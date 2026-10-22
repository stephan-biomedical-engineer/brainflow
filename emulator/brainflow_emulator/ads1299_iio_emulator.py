"""Emulates a Linux IIO ADS1299 for ADS1299_IIO_BOARD and checks what BrainFlow reads from it.

The device is a directory laid out like /sys/bus/iio/devices/iio:deviceN, and its character
device is a FIFO. Once the board enables the buffer, scans are written to the FIFO at 250 SPS,
in the layout of either driver:

  ads1299: the out-of-tree ti-ads1299 (voltages, lead-off word, timestamp)
  ads1298: mainline ti-ads1298 (voltages only)

Every value is a function of the scan number, so each row BrainFlow returns can be checked.
Exits non-zero if any check fails.
"""

import argparse
import logging
import os
import shutil
import statistics
import struct
import sys
import tempfile
import threading
import time

from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds
from brainflow.exit_codes import BrainFlowError

GAINS = [1, 2, 4, 6, 8, 12, 24, 24]
RATE = 250


def scale_text(gain):
    # as ti-ads1299 prints it: 4.5 V / (gain x 2^23) in mV, IIO_VAL_INT_PLUS_NANO, truncated
    return '0.%09d' % (4500000 * 1000000 // (gain << 23))


def scale_mv(gain):
    # what the board must multiply by: the attribute, not the exact value
    return float(scale_text(gain))


def code(k, ch):
    # first scans hit the extremes, then a spread that crosses zero
    if k == 0:
        return -(1 << 23)
    if k == 1:
        return (1 << 23) - 1
    return ((k * 7919 + ch * 104729) % (1 << 24)) - (1 << 23)


def lead_off(k):
    return (k * 3) & 0xFFFF


def write(path, value):
    with open(path, 'w') as f:
        f.write(value + '\n')


def read(path):
    with open(path) as f:
        return f.read().strip()


class FakeDevice:
    def __init__(self, root, mode, channels):
        self.mode = mode
        self.channels = channels
        self.dir = os.path.join(root, 'iio:device0')
        self.chardev = os.path.join(root, 'dev-iio:device0')
        self.base_ns = 0
        self.written = 0
        self.error = None
        se = os.path.join(self.dir, 'scan_elements')
        os.makedirs(se)
        os.makedirs(os.path.join(self.dir, 'buffer'))
        os.mkfifo(self.chardev)

        write(os.path.join(self.dir, 'name'), 'ads1299-%d' % channels if mode == 'ads1299' else 'ads1298')
        write(os.path.join(self.dir, 'sampling_frequency'), '500')  # the board must set 250
        write(os.path.join(self.dir, 'buffer', 'enable'), '0')
        write(os.path.join(self.dir, 'buffer', 'length'), '2')
        for ch in range(channels):
            write(os.path.join(self.dir, 'in_voltage%d_scale' % ch), scale_text(GAINS[ch]))
            self.element(se, 'in_voltage%d' % ch, ch, 'le:s24/32>>0')
        # an element this board does not read, which it must disable
        self.element(se, 'in_accel_x', 20, 'le:s16/16>>0', enabled='1')
        if mode == 'ads1299':
            for ch in range(channels):
                write(os.path.join(self.dir, 'in_voltage%d_hardwaregain' % ch), str(GAINS[ch]))
            write(os.path.join(self.dir, 'current_timestamp_clock'), 'monotonic')  # must become realtime
            write(os.path.join(self.dir, 'lost_samples'), '0')
            self.element(se, 'in_voltage%d_lead_off' % channels, channels, 'le:u16/32>>0')
            self.element(se, 'in_timestamp', channels + 1, 'le:s64/64>>0')

    @staticmethod
    def element(se, base, index, type_, enabled='0'):
        write(os.path.join(se, base + '_en'), enabled)
        write(os.path.join(se, base + '_index'), str(index))
        write(os.path.join(se, base + '_type'), type_)

    def scan(self, k):
        out = b''.join(struct.pack('<I', code(k, ch) & 0xFFFFFF) for ch in range(self.channels))
        if self.mode == 'ads1299':
            out += struct.pack('<I', lead_off(k))
            out += b'\0' * (-len(out) % 8)
            out += struct.pack('<q', self.ts_ns(k))
        return out

    def ts_ns(self, k):
        return self.base_ns + k * (1000000000 // RATE)

    def run(self):
        try:
            enable = os.path.join(self.dir, 'buffer', 'enable')
            while read(enable) != '1':
                time.sleep(0.005)
            with open(self.chardev, 'wb', buffering=0) as fifo:
                self.base_ns = time.time_ns()
                start = time.monotonic()
                k = 0
                while read(enable) == '1':
                    due = int((time.monotonic() - start) * RATE) + 1
                    if due > k:
                        fifo.write(b''.join(self.scan(i) for i in range(k, due)))
                        k = due
                        self.written = k
                    time.sleep(0.02)
        except BrokenPipeError:
            pass
        except Exception as e:  # reported by the main thread
            self.error = e


class Checker:
    def __init__(self):
        self.failed = 0

    def check(self, ok, what):
        logging.info('%s: %s', 'PASS' if ok else 'FAIL', what)
        if not ok:
            self.failed += 1


def expect_error(fn):
    try:
        fn()
    except BrainFlowError:
        return True
    return False


def run(mode, channels, seconds):
    c = Checker()
    root = tempfile.mkdtemp(prefix='ads1299_iio_')
    try:
        dev = FakeDevice(root, mode, channels)
        gen = threading.Thread(target=dev.run, daemon=True)
        gen.start()

        board_id = BoardIds.ADS1299_IIO_BOARD.value
        descr = BoardShim.get_board_descr(board_id)
        eeg = descr['eeg_channels']
        lo_row = descr['other_channels'][0]
        ts_row = descr['timestamp_channel']
        num_row = descr['package_num_channel']
        marker_row = descr['marker_channel']

        params = BrainFlowInputParams()
        params.serial_port = dev.dir
        params.file = dev.chardev
        board = BoardShim(board_id, params)
        board.prepare_session()

        se = os.path.join(dev.dir, 'scan_elements')
        c.check(read(os.path.join(dev.dir, 'sampling_frequency')) == '250', 'prepare_session sets 250 SPS')
        if mode == 'ads1299':
            c.check(read(os.path.join(dev.dir, 'current_timestamp_clock')) == 'realtime',
                    'prepare_session sets the realtime clock')
            gain_attr = 'in_voltage%d_hardwaregain' % (channels - 1)
            gain = str(GAINS[channels - 1])
            c.check(board.config_board(gain_attr) == gain, 'config_board reads an attribute')
            board.config_board(gain_attr + '=12')
            c.check(read(os.path.join(dev.dir, gain_attr)) == '12', 'config_board writes an attribute')
            board.config_board(gain_attr + '=' + gain)
        c.check(expect_error(lambda: board.config_board('sampling_frequency=500')),
                'config_board refuses sampling_frequency')
        c.check(expect_error(lambda: board.config_board('../name')), 'config_board refuses a path')
        c.check(expect_error(lambda: board.config_board('no_such_attribute')),
                'config_board refuses a missing attribute')

        board.start_stream()
        c.check(read(os.path.join(se, 'in_accel_x_en')) == '0', 'start_stream disables unused elements')
        c.check(read(os.path.join(dev.dir, 'buffer', 'length')) == '1024', 'start_stream sets buffer/length')
        time.sleep(seconds / 2)
        board.insert_marker(42.0)
        if mode == 'ads1299':
            c.check(expect_error(lambda: board.config_board(gain_attr + '=12')),
                    'config_board refuses a write while streaming')
        time.sleep(seconds / 2)
        data = board.get_board_data()
        board.stop_stream()
        c.check(read(os.path.join(dev.dir, 'buffer', 'enable')) == '0', 'stop_stream disables the buffer')
        board.release_session()
        gen.join(timeout=2)
        c.check(dev.error is None, 'the emulator ran without errors (%s)' % dev.error)

        n = data.shape[1]
        logging.info('%d scans written, %d rows read', dev.written, n)
        c.check(n >= seconds * RATE * 0.9, 'at least 90 %% of %d scans read' % (seconds * RATE))
        nums = [int(x) for x in data[num_row]]
        c.check(nums == list(range(n)), 'package numbers run from 0 without gaps')

        bad = 0
        for col in range(n):
            k = nums[col]
            for ch, row in enumerate(eeg):
                want = code(k, ch) * scale_mv(GAINS[ch]) * 1000.0 if ch < channels else 0.0
                if abs(data[row][col] - want) > 1e-9 * max(1.0, abs(want)):
                    bad += 1
        c.check(bad == 0, 'every EEG value equals code x scale in uV (%d wrong)' % bad)
        c.check(abs(data[eeg[0]][0] + 4500000.0 / GAINS[0]) < 0.01 * 4500000.0 / GAINS[0],
                'the most negative code reads as -%.0f uV (%.3f)' % (4500000.0 / GAINS[0], data[eeg[0]][0]))

        if mode == 'ads1299':
            c.check(all(int(data[lo_row][col]) == lead_off(nums[col]) for col in range(n)),
                    'the lead-off row carries the lead-off word')
            err = max(abs(data[ts_row][col] - dev.ts_ns(nums[col]) / 1e9) for col in range(n))
            c.check(err < 1e-6, 'timestamps are the IIO timestamps (worst %.3g s off)' % err)
        else:
            c.check(all(data[lo_row][col] == -1.0 for col in range(n)), 'the lead-off row is -1 without one')
            diffs = [data[ts_row][i + 1] - data[ts_row][i] for i in range(n - 1)]
            med = statistics.median(diffs)
            c.check(abs(med - 1.0 / RATE) < 0.0005, 'host timestamps step by %.4f s (median)' % med)
            c.check(abs(data[ts_row][-1] - time.time()) < 5, 'host timestamps are Unix time')

        markers = [x for x in data[marker_row] if x != 0]
        c.check(markers == [42.0], 'the marker appears once (%s)' % markers)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return c.failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['ads1299', 'ads1298'], default='ads1299')
    parser.add_argument('--channels', type=int, choices=[4, 6, 8], default=8)
    parser.add_argument('--seconds', type=float, default=2.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    BoardShim.enable_dev_board_logger()
    failed = run(args.mode, args.channels, args.seconds)
    logging.info('%s: %d checks failed', 'FAILED' if failed else 'OK', failed)
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
