#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
#
# Diagnostico da falha do A da terceira rodada da F3 (2026-10-22): um passo de
# 16,1 ms entre timestamps, com lost_samples 0 e os numeros de pacote sem
# buraco. Sem o BrainFlow: o buffer IIO lido aqui mesmo, em duas fases com o
# mesmo tempo, uma com current_timestamp_clock = monotonic (o NTP nao o ajusta
# aos saltos) e outra com realtime (o relogio que a placa do BrainFlow usa).
#
#   sudo build/venv/bin/python pi-test/lacunas.py [--seconds 180] 2>&1 | tee ~/lacunas.txt
#   build/venv/bin/python pi-test/lacunas.py --autoteste
#
# Para cada fase:
#   amostras    - entregues pelo buffer
#   faltam      - pelos timestamps: soma de (passo / 4 ms arredondado - 1) nos
#                 passos acima de 6 ms
#   lost_samples - o que o driver contou
#   irq         - interrupcoes do DRDY no /proc/interrupts durante a fase
#
# Leitura:
#   - lacunas nas duas fases, faltam > lost_samples: o driver perde conversoes
#     sem contar. Se irq = amostras, as bordas do DRDY se juntaram (a
#     interrupcao atrasou e as seguintes nao foram vistas).
#   - lacunas so na fase realtime: o relogio foi ajustado; nada se perdeu.
#   - nenhuma lacuna: o evento nao se repetiu nesse tempo.
#
# Deixa o dispositivo como encontrou.

import argparse
import glob
import os
import select
import struct
import sys
import time

PERIOD_NS = 4000000
GAP_NS = 6000000


def missing_from_steps(steps_ns):
    """Conversoes que faltam, pelos passos acima de 1,5 periodo."""
    return sum(max(0, round(s / PERIOD_NS) - 1) for s in steps_ns if s > GAP_NS)


def decode(data, n):
    """n tensoes le:s24/32 e o timestamp le:s64, alinhado a 8: o timestamp de cada scan."""
    size = 4 * n
    size += (-size) % 8
    ts_off = size
    size += 8
    return [struct.unpack_from('<q', data, off + ts_off)[0]
            for off in range(0, len(data) - size + 1, size)]


def autoteste():
    ok = True

    def eq(got, want, what):
        nonlocal ok
        print('%s %s: %r (esperado %r)' % ('ok  ' if got == want else 'FALHA', what, got, want))
        ok = ok and got == want

    eq(missing_from_steps([4000000] * 10), 0, 'sem lacuna')
    eq(missing_from_steps([4000000, 16108000, 4000000]), 3, 'passo de 16,1 ms')
    eq(missing_from_steps([5900000]), 0, 'jitter abaixo de 1,5 periodo')
    scans = b''.join(b'\0' * 32 + struct.pack('<q', t) for t in (10, 20))
    eq(decode(scans, 8), [10, 20], 'timestamps de 8 canais')
    scans = b''.join(b'\0' * 16 + struct.pack('<q', t) for t in (7,))
    eq(decode(scans, 4), [7], 'timestamp de 4 canais')
    print('tudo certo' if ok else 'ALGO ERRADO')
    return 0 if ok else 1


def irq_count():
    total = 0
    with open('/proc/interrupts') as f:
        for line in f:
            if 'ads1299' in line:
                fields = line.split()[1:]
                for x in fields:
                    if not x.isdigit():
                        break
                    total += int(x)
    return total


class Dev:
    def __init__(self, path):
        self.dir = path
        self.saved = {}

    def read(self, name):
        with open(os.path.join(self.dir, name)) as f:
            return f.read().strip()

    def write(self, name, value):
        if name not in self.saved:
            self.saved[name] = self.read(name)
        with open(os.path.join(self.dir, name), 'w') as f:
            f.write(value)

    def restore(self):
        for name, value in self.saved.items():
            try:
                with open(os.path.join(self.dir, name), 'w') as f:
                    f.write(value)
            except OSError as e:
                print('  nao consegui devolver %s=%s: %s' % (name, value, e))


def phase(dev, n, clock, seconds, chardev):
    dev.write('current_timestamp_clock', clock)
    for f in sorted(os.listdir(os.path.join(dev.dir, 'scan_elements'))):
        if f.endswith('_en'):
            base = f[:-3]
            want = base == 'in_timestamp' or (base.startswith('in_voltage') and base[len('in_voltage'):].isdigit())
            dev.write('scan_elements/' + f, '1' if want else '0')
    dev.write('buffer/length', '1024')

    irq0 = irq_count()
    dev.write('buffer/enable', '1')
    data = bytearray()
    fd = os.open(chardev, os.O_RDONLY | os.O_NONBLOCK)
    try:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            r, _, _ = select.select([fd], [], [], 0.5)
            if r:
                try:
                    data += os.read(fd, 65536)
                except BlockingIOError:
                    pass
    finally:
        os.close(fd)
        dev.write('buffer/enable', '0')
    irq = irq_count() - irq0
    lost = dev.read('lost_samples')

    ts = decode(bytes(data), n)
    steps = [b - a for a, b in zip(ts, ts[1:])]
    gaps = [(i, s) for i, s in enumerate(steps) if s > GAP_NS]
    print('\n== %s, %d s' % (clock, seconds))
    print('  amostras %d, irq %d, lost_samples %s, faltam pelos timestamps %d'
          % (len(ts), irq, lost, missing_from_steps(steps)))
    print('  passo: menor %.3f ms, maior %.3f ms, fora de ordem %d'
          % (min(steps) / 1e6, max(steps) / 1e6, sum(1 for s in steps if s <= 0)))
    for i, s in gaps[:20]:
        around = ' '.join('%.3f' % (x / 1e6) for x in steps[max(0, i - 2):i + 3])
        print('  lacuna na amostra %d: %.3f ms (passos em volta, ms: %s)' % (i + 1, s / 1e6, around))
    if len(gaps) > 20:
        print('  e mais %d lacunas' % (len(gaps) - 20))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--autoteste', action='store_true')
    parser.add_argument('--seconds', type=int, default=180)
    parser.add_argument('--dev', help='diretorio sysfs (so para ensaio sem a placa)')
    parser.add_argument('--file', default='', help='dispositivo de caracteres (so para ensaio sem a placa)')
    args = parser.parse_args()
    if args.autoteste:
        return autoteste()

    found = [os.path.dirname(p) for p in glob.glob('/sys/bus/iio/devices/iio:device*/name')
             if open(p).read().startswith('ads1299')]
    if args.dev:
        found = [args.dev]
    if len(found) != 1:
        print('nenhum ou mais de um ads1299: %s' % found)
        return 2
    dev = Dev(found[0])
    chardev = args.file or '/dev/' + os.path.basename(found[0])
    n = sum(1 for p in glob.glob(os.path.join(found[0], 'scan_elements', 'in_voltage*_en'))
            if os.path.basename(p)[len('in_voltage'):-3].isdigit())
    print('%s: %s, %d canais, %s, sampling_frequency %s'
          % (found[0], dev.read('name'), n, dev.read('timestamp_source'), dev.read('sampling_frequency')))
    if dev.read('sampling_frequency') != '250':
        print('esperado 250 SPS')
        return 2
    print('relogio do sistema:')
    os.system('timedatectl show -p NTP -p NTPSynchronized -p TimeUSec 2>/dev/null | sed "s/^/  /"')
    try:
        phase(dev, n, 'monotonic', args.seconds, chardev)
        phase(dev, n, 'realtime', args.seconds, chardev)
    finally:
        dev.restore()
    return 0


if __name__ == '__main__':
    sys.exit(main())
