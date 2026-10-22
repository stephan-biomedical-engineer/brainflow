#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
#
# Diagnostico da falha do E da F3 (canal 7, ganho 1, +3,5 %): o mesmo gerador
# 1x_dc contra as entradas em curto, medido de tres jeitos, intercalados:
#
#   avulsa   - in_voltageN_raw, leituras single-shot (o metodo da F4 e do
#              BRINGUP_AFE 5.1): 10 leituras depois de 2 descartadas, 50 ms
#   buffer   - o buffer IIO lido aqui mesmo em Python, sem o BrainFlow: 2 s,
#              os 25 primeiros descartados
#   brainflow - a placa ADS1299_IIO_BOARD: 2 s, os 25 primeiros descartados
#
# nos ganhos 1 e 24, tres rodadas. Mostra o erro de cada canal contra 1875 uV
# (VREF / 2400) e o offset em codigos, que nasce depois do PGA.
#
#   sudo build/venv/bin/python pi-test/ganho1.py 2>&1 | tee ~/ganho1.txt
#   build/venv/bin/python pi-test/ganho1.py --autoteste
#
# Criterio (o que decide se a placa esta certa): em cada ganho e rodada, o
# erro do brainflow a 0,1 ponto do erro do buffer lido em Python. O que a
# avulsa diz e informativo: se ela discorda dos dois, a diferenca e do modo de
# aquisicao (driver ou chip), nao do BrainFlow.
#
# Deixa o dispositivo como encontrou.

import argparse
import importlib.util
import os
import select
import statistics
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('f3', os.path.join(HERE, 'teste-f3.py'))
f3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(f3)

GAINS = [1, 24]
ROUNDS = 3
SECONDS = 2
DISCARD = 25
READINGS, SKIP, INTERVAL = 10, 2, 0.050
TOL = 0.1   # pontos percentuais entre brainflow e buffer


def decode_scans(data, n):
    """n canais le:s24/32, nada mais habilitado: um inteiro com sinal por canal."""
    size = 4 * n
    out = [[] for _ in range(n)]
    for off in range(0, len(data) - size + 1, size):
        for ch in range(n):
            v = struct.unpack_from('<I', data, off + 4 * ch)[0] & 0xFFFFFF
            out[ch].append(v - (1 << 24) if v & 0x800000 else v)
    return out


def autoteste():
    ok = True
    raw = b''.join(struct.pack('<I', v & 0xFFFFFF) for v in [-8388608, 8388607, -1, 5])
    got = decode_scans(raw * 2, 4)
    want = [[-8388608] * 2, [8388607] * 2, [-1] * 2, [5] * 2]
    print('%s decodificacao do buffer' % ('ok  ' if got == want else 'FALHA'))
    ok = ok and got == want
    print('tudo certo' if ok else 'ALGO ERRADO')
    return 0 if ok else 1


class Diag:
    def __init__(self, board, dev, n, chardev):
        self.chardev = chardev
        self.board = board
        self.dev = dev
        self.n = n
        from brainflow.board_shim import BoardShim
        self.eeg = BoardShim.get_board_descr(board.board_id)['eeg_channels'][:n]

    def set_all(self, fmt, value):
        for ch in range(self.n):
            name = fmt % ch
            self.dev.save(name)
            if self.dev.read(name) != value:
                self.dev.write(name, value)

    def uv_per_code(self):
        return [float(self.dev.read('in_voltage%d_scale' % ch)) * 1000 for ch in range(self.n)]

    def avulsa(self):
        vals = [[] for _ in range(self.n)]
        for r in range(SKIP + READINGS):
            row = [int(self.dev.read('in_voltage%d_raw' % ch)) for ch in range(self.n)]
            if r >= SKIP:
                for ch in range(self.n):
                    vals[ch].append(row[ch])
            time.sleep(INTERVAL)
        return [statistics.mean(v) for v in vals]

    def buffer(self):
        se = os.path.join(self.dev.dir, 'scan_elements')
        for f in os.listdir(se):
            if f.endswith('_en'):
                base = f[:-3]
                want = '1' if base[len('in_voltage'):].isdigit() and base.startswith('in_voltage') else '0'
                self.dev.write(os.path.join('scan_elements', f), want)
        self.dev.write('buffer/length', '1024')
        self.dev.write('buffer/enable', '1')
        data = b''
        fd = os.open(self.chardev, os.O_RDONLY | os.O_NONBLOCK)
        try:
            end = time.monotonic() + SECONDS
            while time.monotonic() < end:
                r, _, _ = select.select([fd], [], [], 0.5)
                if r:
                    try:
                        data += os.read(fd, 4096)
                    except BlockingIOError:
                        pass
        finally:
            os.close(fd)
            self.dev.write('buffer/enable', '0')
        scans = decode_scans(data, self.n)
        return [statistics.mean(v[DISCARD:]) for v in scans]

    def brainflow(self, uv):
        self.board.start_stream()
        time.sleep(SECONDS)
        data = self.board.get_board_data()
        self.board.stop_stream()
        # de volta a codigos, para comparar com os outros dois
        return [statistics.mean(data[r][DISCARD:]) / uv[ch] for ch, r in enumerate(self.eeg)]

    def run(self):
        failed = []
        for rnd in range(1, ROUNDS + 1):
            for gain in GAINS:
                self.set_all('in_voltage%d_input_mux', 'shorted')
                self.set_all('in_voltage%d_hardwaregain', str(gain))
                uv = self.uv_per_code()
                m = {}
                for state in ('test_signal', 'shorted'):
                    self.set_all('in_voltage%d_input_mux', state)
                    m[state] = {'avulsa': self.avulsa(), 'buffer': self.buffer(),
                                'brainflow': self.brainflow(uv)}
                print('\n== rodada %d, ganho %d (erro contra 1875 uV; offset em codigos)' % (rnd, gain))
                print('  canal   avulsa   buffer  brainflow | offset: avulsa  buffer  brainflow')
                for ch in range(self.n):
                    err = {}
                    for k in ('avulsa', 'buffer', 'brainflow'):
                        err[k] = f3.gen_err(m['test_signal'][k][ch] * uv[ch], m['shorted'][k][ch] * uv[ch])
                    print('  %5d  %+6.3f%%  %+6.3f%%  %+6.3f%%  |        %7.1f %7.1f  %7.1f'
                          % (ch + 1, err['avulsa'], err['buffer'], err['brainflow'],
                             m['shorted']['avulsa'][ch], m['shorted']['buffer'][ch],
                             m['shorted']['brainflow'][ch]))
                    if abs(err['brainflow'] - err['buffer']) > TOL:
                        failed.append('rodada %d, ganho %d, canal %d: brainflow %+.3f %%, buffer %+.3f %%'
                                      % (rnd, gain, ch + 1, err['brainflow'], err['buffer']))
        return failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--autoteste', action='store_true')
    parser.add_argument('--dev', help='diretorio sysfs (so para ensaio sem a placa)')
    parser.add_argument('--file', default='', help='dispositivo de caracteres (so para ensaio sem a placa)')
    args = parser.parse_args()
    if args.autoteste:
        return autoteste()

    from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds
    import glob
    found = [os.path.dirname(p) for p in glob.glob('/sys/bus/iio/devices/iio:device*/name')
             if open(p).read().startswith('ads1299')]
    if args.dev:
        found = [args.dev]
    if len(found) != 1:
        print('nenhum ou mais de um ads1299: %s' % found)
        return 2
    dev = f3.Device(found[0])
    chardev = args.file or '/dev/' + os.path.basename(found[0])
    n = 8
    for ch in range(n):
        dev.save('in_voltage%d_input_mux' % ch)
        dev.save('in_voltage%d_hardwaregain' % ch)
    dev.save('test_signal')
    dev.save('sampling_frequency')
    dev.save('buffer/length')
    for f in sorted(os.listdir(os.path.join(found[0], 'scan_elements'))):
        if f.endswith('_en'):
            dev.save('scan_elements/' + f)

    params = BrainFlowInputParams()
    params.serial_port = found[0]
    params.file = args.file
    board = BoardShim(BoardIds.ADS1299_IIO_BOARD, params)
    board.prepare_session()
    try:
        dev.write('test_signal', '1x_dc')
        failed = Diag(board, dev, n, chardev).run()
    finally:
        board.release_session()
        dev.restore()

    print('\n%d falhas (brainflow contra buffer, tolerancia %.1f ponto)' % (len(failed), TOL))
    for f in failed:
        print('  ' + f)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
