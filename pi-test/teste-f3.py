#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
#
# F3 do PLANO_BRAINFLOW.md: a placa ADS1299_IIO_BOARD do BrainFlow contra o
# ADS1299 de verdade, na Raspberry Pi, com o driver completo (ti-ads1299,
# dispositivo "ads1299-8") carregado.
#
#   sudo build/venv/bin/python pi-test/teste-f3.py      contra o dispositivo
#   build/venv/bin/python pi-test/teste-f3.py --autoteste   so as contas
#
# Tudo e lido pelo BrainFlow; o sysfs so e lido para comparar, e escrito so
# para devolver o dispositivo como estava. As mudancas de configuracao passam
# pelo config_board.
#
# Criterios:
#   A. 10 s a 250 SPS: linhas = 2500 +-2 %, numeros de pacote sem buraco,
#      passo dos timestamps IIO com mediana de 4 ms, nenhum acima de 6 ms e
#      nenhum fora de ordem, lost_samples 0, timestamp em tempo Unix
#   B. gerador 1x_dc contra entradas em curto, ganho 24: |nivel - offset| em uV
#      a +-2,5 % de 1875 uV (VREF / 2400), e o erro de cada canal a 0,05
#      ponto do que a F4 mediu nesta Pi sem o BrainFlow
#   C. entradas em curto: ruido pico a pico, em codigos, abaixo do teto do
#      autoteste (447 = 10 uV no ganho 24)
#   D. lead-off com a entrada positiva de todo canal sentida, as entradas
#      abertas e a corrente de 6 uA (com a corrente padrao, uma entrada aberta
#      perto do limiar faz o comparador oscilar: 2026-10-22, canal 3): a linha
#      de lead-off com um valor so, igual ao lead_off_status; sem nada
#      sentido, a linha em 0
#   E. ganho 1 em vez de 24 pelo config_board: o erro do gerador em uV a 0,1
#      ponto do que leituras avulsas (in_voltageN_raw x scale, sem o BrainFlow)
#      dao no mesmo ganho. O nivel NAO e o mesmo nos dois ganhos: neste chip o
#      ganho 1 se afasta do 24 de -1,7 a +3,5 % conforme o canal (ganho1.py,
#      2026-10-22), igual nos tres metodos de leitura
#   F. onda quadrada 1x_slow: periodo de 256 amostras (fCLK / 2^21 contra
#      fCLK / 8192), independente do relogio
#   G. config_board: recusa sampling_frequency e escrita durante o stream;
#      get_board_sampling_rate = 250
#
# No fim, devolve cada atributo que mudou ao valor que tinha.

import argparse
import glob
import os
import statistics
import sys
import time

RATE = 250
VREF_UV = 4500000
EXPECTED_UV = VREF_UV / 2400             # 1875 uV
NOISE_CEILING = 10 * 24 * 2 ** 23 / VREF_UV   # 447 codigos no ganho 24
SQUARE_PERIOD = 2 ** 21 // 8192          # 256 amostras a 250 SPS
DISCARD = 25                             # 0,1 s depois do START

# F4, Raspberry Pi 4B, 6.18.50+rpt-rpi-v8, 100 rodadas, leituras diretas
# (ads1299-v1/pi-test/teste-v1.py)
F4_ERR = [-0.340, -0.241, -0.239, -0.350, -0.414, -0.175, -0.076, -0.311]
TOL_F4 = 0.05            # pontos percentuais
TOL_DATASHEET = 2.5      # %


# ---------------------------------------------------------------- contas

def gen_err(level_uv, offset_uv):
    """Erro do gerador em %, contra VREF / 2400."""
    return (abs(level_uv - offset_uv) / EXPECTED_UV - 1) * 100


def lead_off_word(status):
    """'ff 00' (lead_off_status) -> 0xff00, como a linha de lead-off."""
    pp, nn = status.split()
    return (int(pp, 16) << 8) | int(nn, 16)


def rising_edges(x):
    """Indices onde o sinal cruza a mediana subindo."""
    mid = statistics.median(x)
    return [i for i in range(1, len(x)) if x[i - 1] < mid <= x[i]]


def square_period(x):
    e = rising_edges(x)
    if len(e) < 2:
        return None
    return (e[-1] - e[0]) / (len(e) - 1)


def mode(values):
    return statistics.mode(values)


def autoteste():
    ok = True

    def eq(got, want, what):
        nonlocal ok
        good = abs(got - want) < 1e-9 if isinstance(want, float) else got == want
        print('%s %s: %r (esperado %r)' % ('ok  ' if good else 'FALHA', what, got, want))
        ok = ok and good

    eq(gen_err(1875.0 + 10.0, 10.0), 0.0, 'erro zero')
    eq(round(gen_err(-1875.0 * 0.99, 0.0), 6), round(-1.0, 6), 'erro de -1 % com nivel negativo')
    eq(lead_off_word('ff 00'), 0xff00, 'palavra de lead-off')
    eq(lead_off_word('01 80'), 0x0180, 'palavra de lead-off, os dois lados')
    sq = [(1 if (i // 128) % 2 == 0 else -1) * 1875.0 + 3.0 for i in range(256 * 6 + 40)]
    eq(square_period(sq), 256.0, 'periodo da onda quadrada')
    eq(square_period([0.0] * 100), None, 'periodo de um sinal constante')
    eq(mode([3, 3, 5]), 3, 'moda')
    eq(round(NOISE_CEILING), 447, 'teto do ruido em codigos')
    print('tudo certo' if ok else 'ALGO ERRADO')
    return 0 if ok else 1


# ---------------------------------------------------------------- dispositivo

class Device:
    """O diretorio sysfs, para comparar e para devolver o que mudou."""

    def __init__(self, path):
        self.dir = path
        self.saved = {}

    def read(self, name):
        with open(os.path.join(self.dir, name)) as f:
            return f.read().strip()

    def write(self, name, value):
        with open(os.path.join(self.dir, name), 'w') as f:
            f.write(value)

    def save(self, name):
        if name not in self.saved:
            self.saved[name] = self.read(name)

    def restore(self):
        # o MUX sai do gerador antes de desligar o gerador
        order = sorted(self.saved, key=lambda n: (n == 'test_signal', n))
        for name in order:
            try:
                if self.read(name) != self.saved[name]:
                    self.write(name, self.saved[name])
            except OSError as e:
                print('  nao consegui devolver %s=%s: %s' % (name, self.saved[name], e))


class Test:
    def __init__(self, board, dev, n):
        from brainflow.board_shim import BoardShim
        self.board = board
        self.dev = dev
        self.n = n
        descr = BoardShim.get_board_descr(board.board_id)
        self.eeg = descr['eeg_channels'][:n]
        self.lo = descr['other_channels'][0]
        self.ts = descr['timestamp_channel']
        self.num = descr['package_num_channel']
        self.failed = []

    def check(self, ok, what):
        print('  %s %s' % ('ok   ' if ok else 'FALHA', what))
        if not ok:
            self.failed.append(what)

    def config(self, attr, value):
        self.dev.save(attr)
        self.board.config_board('%s=%s' % (attr, value))

    def set_all(self, fmt, value):
        for ch in range(self.n):
            self.config(fmt % ch, value)

    def stream(self, seconds):
        self.board.start_stream()
        time.sleep(seconds)
        data = self.board.get_board_data()
        self.board.stop_stream()
        return data

    def means(self, data):
        return [statistics.mean(data[r][DISCARD:]) for r in self.eeg]

    def uv_per_code(self, ch):
        return float(self.dev.read('in_voltage%d_scale' % ch)) * 1000

    # A
    def counting(self):
        print('\n== A. 10 s a 250 SPS, entradas normais')
        self.set_all('in_voltage%d_input_mux', 'normal')
        data = self.stream(10)
        n = data.shape[1]
        nums = [int(x) for x in data[self.num]]
        steps = [data[self.ts][i + 1] - data[self.ts][i] for i in range(n - 1)]
        lost = self.dev.read('lost_samples')
        print('  %d linhas, passo mediano %.4f ms, maior %.4f ms, lost_samples %s'
              % (n, statistics.median(steps) * 1000, max(steps) * 1000, lost))
        self.check(abs(n - 10 * RATE) <= 0.02 * 10 * RATE, 'linhas a 2 %% de 2500 (%d)' % n)
        self.check(nums == list(range(n)), 'numeros de pacote sem buraco')
        self.check(abs(statistics.median(steps) - 1 / RATE) < 0.00004, 'passo mediano de 4 ms')
        self.check(max(steps) < 0.006, 'nenhum passo acima de 6 ms (1,5 periodo)')
        self.check(min(steps) > 0, 'nenhum timestamp fora de ordem')
        self.check(lost == '0', 'lost_samples 0')
        self.check(abs(data[self.ts][-1] - time.time()) < 2, 'timestamp em tempo Unix')

    # B, C, E
    def generator(self):
        print('\n== B. gerador 1x_dc contra entradas em curto, ganho 24')
        self.set_all('in_voltage%d_hardwaregain', '24')
        self.config('test_signal', '1x_dc')
        self.set_all('in_voltage%d_input_mux', 'test_signal')
        level = self.means(self.stream(2))
        scale24 = [self.uv_per_code(ch) for ch in range(self.n)]
        self.set_all('in_voltage%d_input_mux', 'shorted')
        short = self.stream(2)
        offset = self.means(short)
        for ch in range(self.n):
            err = gen_err(level[ch], offset[ch])
            print('  canal %d: nivel %+.2f uV, offset %+.2f uV, erro %+.3f %% (F4 %+.3f %%)'
                  % (ch + 1, level[ch], offset[ch], err, F4_ERR[ch]))
            self.check(abs(err) <= TOL_DATASHEET, 'canal %d a 2,5 %% de 1875 uV' % (ch + 1))
            self.check(abs(err - F4_ERR[ch]) <= TOL_F4, 'canal %d a 0,05 ponto da F4' % (ch + 1))

        print('\n== C. ruido com entradas em curto, ganho 24')
        for ch in range(self.n):
            x = short[self.eeg[ch]][DISCARD:]
            pp = (max(x) - min(x)) / scale24[ch]
            print('  canal %d: %.0f codigos pico a pico (%.2f uV)' % (ch + 1, pp, max(x) - min(x)))
            self.check(pp < NOISE_CEILING, 'canal %d abaixo de 447 codigos' % (ch + 1))

        print('\n== E. o mesmo gerador no ganho 1, pelo config_board')
        self.set_all('in_voltage%d_hardwaregain', '1')
        self.set_all('in_voltage%d_input_mux', 'test_signal')
        level1 = self.means(self.stream(2))
        scale1 = [self.uv_per_code(ch) for ch in range(self.n)]
        direct_level = self.direct()
        self.set_all('in_voltage%d_input_mux', 'shorted')
        offset1 = self.means(self.stream(2))
        direct_offset = self.direct()
        for ch in range(self.n):
            err1 = gen_err(level1[ch], offset1[ch])
            err_direct = gen_err(direct_level[ch] * scale1[ch], direct_offset[ch] * scale1[ch])
            err24 = gen_err(level[ch], offset[ch])
            print('  canal %d: erro %+.3f %% pelo BrainFlow, %+.3f %% pelas avulsas (ganho 24: %+.3f %%)'
                  % (ch + 1, err1, err_direct, err24))
            self.check(abs(err1 - err_direct) <= 0.1, 'canal %d: a 0,1 ponto das avulsas' % (ch + 1))
        self.set_all('in_voltage%d_hardwaregain', '24')

    def direct(self):
        """Media de 10 leituras avulsas por canal, depois de 2 descartadas, 50 ms entre elas."""
        vals = [[] for _ in range(self.n)]
        for r in range(12):
            row = [int(self.dev.read('in_voltage%d_raw' % ch)) for ch in range(self.n)]
            if r >= 2:
                for ch in range(self.n):
                    vals[ch].append(row[ch])
            time.sleep(0.050)
        return [statistics.mean(v) for v in vals]

    # F
    def square(self):
        print('\n== F. onda quadrada 1x_slow')
        self.set_all('in_voltage%d_input_mux', 'shorted')  # o MUX sai do gerador antes de trocar o sinal
        self.config('test_signal', '1x_slow')
        self.set_all('in_voltage%d_input_mux', 'test_signal')
        data = self.stream(6)
        for ch in range(self.n):
            x = list(data[self.eeg[ch]][DISCARD:])
            period = square_period(x)
            print('  canal %d: periodo %s amostras, pico a pico %.1f uV'
                  % (ch + 1, '%.2f' % period if period else '-', max(x) - min(x)))
            self.check(period is not None and abs(period - SQUARE_PERIOD) <= 1,
                       'canal %d: periodo de 256 amostras' % (ch + 1))
        self.set_all('in_voltage%d_input_mux', 'shorted')

    # D
    def lead_off(self):
        print('\n== D. lead-off, entradas abertas')
        self.set_all('in_voltage%d_input_mux', 'normal')
        self.config('lead_off_frequency', 'dc')
        self.dev.save('lead_off_current_nanoamp')
        print('  corrente de lead-off: %s nA, passa a 6000' % self.dev.read('lead_off_current_nanoamp'))
        self.config('lead_off_current_nanoamp', '6000')
        self.set_all('in_voltage%d_lead_off_sense_p', '1')
        data = self.stream(2)
        status = self.dev.read('lead_off_status')
        words = [int(x) for x in data[self.lo][DISCARD:]]
        common = sorted(set(words), key=words.count, reverse=True)
        counts = ', '.join('0x%04x x%d' % (w, words.count(w)) for w in common[:8])
        if len(common) > 8:
            counts += ' e mais %d valores' % (len(common) - 8)
        print('  lead_off_status "%s"; linha de lead-off: %s' % (status, counts))
        self.check(status not in ('disabled', 'ac') and set(words) == {lead_off_word(status)},
                   'a linha de lead-off tem um valor so, o do lead_off_status')
        self.check((mode(words) >> 8) != 0, 'alguma entrada positiva aberta aparece')

        self.set_all('in_voltage%d_lead_off_sense_p', '0')
        data = self.stream(2)
        words = [int(x) for x in data[self.lo][DISCARD:]]
        self.check(set(words) == {0}, 'sem nada sentido, a linha fica em 0')

    # G
    def config_board(self):
        from brainflow.exit_codes import BrainFlowError
        print('\n== G. config_board e taxa')

        def refused(fn):
            try:
                fn()
            except BrainFlowError:
                return True
            return False

        self.check(self.dev.read('sampling_frequency') == '250', 'sampling_frequency 250 depois do prepare')
        self.check(self.board.get_board_sampling_rate() == RATE, 'get_board_sampling_rate 250')
        self.check(refused(lambda: self.board.config_board('sampling_frequency=500')),
                   'sampling_frequency recusado')
        self.check(self.board.config_board('timestamp_source') == self.dev.read('timestamp_source'),
                   'leitura de atributo (timestamp_source = %s)' % self.dev.read('timestamp_source'))
        self.board.start_stream()
        self.check(refused(lambda: self.board.config_board('in_voltage0_hardwaregain=12')),
                   'escrita recusada durante o stream')
        self.board.stop_stream()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--autoteste', action='store_true')
    parser.add_argument('--dev', help='diretorio sysfs; sem ele, a placa procura sozinha')
    parser.add_argument('--file', default='', help='dispositivo de caracteres (so para ensaio sem a placa)')
    args = parser.parse_args()
    if args.autoteste:
        return autoteste()

    from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds

    found = [os.path.dirname(p) for p in glob.glob('/sys/bus/iio/devices/iio:device*/name')
             if open(p).read().startswith('ads129')]
    path = args.dev or (found[0] if len(found) == 1 else None)
    if path is None:
        print('nenhum ou mais de um ads129x: %s; use --dev' % found)
        return 2
    dev = Device(path)
    n = sum(1 for p in glob.glob(os.path.join(path, 'scan_elements', 'in_voltage*_en'))
            if os.path.basename(p)[len('in_voltage'):-3].isdigit())
    print('%s: %s, %d canais, timestamp_source %s'
          % (path, dev.read('name'), n, dev.read('timestamp_source')))

    params = BrainFlowInputParams()
    params.serial_port = args.dev or ''   # vazio: a placa procura sozinha
    params.file = args.file
    board = BoardShim(BoardIds.ADS1299_IIO_BOARD, params)
    dev.save('sampling_frequency')
    board.prepare_session()
    t = Test(board, dev, n)
    try:
        t.config_board()
        t.counting()
        t.generator()
        t.square()
        t.lead_off()
    finally:
        board.release_session()
        dev.restore()

    print('\n%d falhas' % len(t.failed))
    for what in t.failed:
        print('  ' + what)
    return 1 if t.failed else 0


if __name__ == '__main__':
    sys.exit(main())
