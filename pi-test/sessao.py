#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
#
# Uma sessao de EEG pelo BrainFlow, como um usuario da PiEEG faria, com a placa
# ADS1299_IIO_BOARD e uma touca de 8 canais: referencia no SRB1, BIAS ligado.
#
#   sudo build/venv/bin/python pi-test/sessao.py [--nomes Fp1,Fp2,C3,C4,P7,P8,O1,O2]
#
# 1. Configura a montagem pelo config_board: montage referential (SRB1 nas
#    entradas negativas), BIAS derivado das entradas P e N de todos os canais,
#    ganho 24, MUX normal, gerador desligado.
# 2. Confere o contato: lead-off DC de 6 nA, 3 s, com a entrada positiva de
#    cada canal, a negativa do canal 1 (que, com o SRB1 fechado, e o eletrodo
#    de referencia) e o eletrodo de BIAS. Depois desliga o lead-off.
# 3. Mostra 5 s de qualidade por canal (60 Hz, RMS em 1-40 Hz) e pergunta se
#    segue.
# 4. Grava o protocolo, com marcadores, num arquivo do BrainFlow e transmite ao
#    vivo por streaming_board://225.1.1.1:6677 (o ao_vivo_pc.py mostra):
#      3 x (30 s de olhos abertos [marcador 1], 30 s fechados [marcador 2]);
#      10 piscadas, uma a cada 3 s, ao bipe [marcador 3]; fim [marcador 9].
#    Com os olhos fechados nao se le a tela: um bipe avisa a hora de abrir.
#
# No fim, devolve cada atributo ao valor que tinha.
#
# SEGURANCA: a placa nao e um equipamento medico e nao tem isolacao. Com os
# eletrodos na pessoa, a Pi so na bateria (power bank), sem cabo para nada
# ligado na tomada: nem fonte, nem HDMI, nem USB para um PC. So Wi-Fi.

import argparse
import importlib.util
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('f3', os.path.join(HERE, 'teste-f3.py'))
f3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(f3)

RATE = 250
STREAM = 'streaming_board://225.1.1.1:6677'
CICLOS, ABERTOS_S, FECHADOS_S = 3, 30, 30
PISCADAS, PISCADA_S = 10, 3
M_ABERTOS, M_FECHADOS, M_PISCADA, M_FIM = 1.0, 2.0, 3.0, 9.0


def bipe(n=1):
    for _ in range(n):
        sys.stdout.write('\a')
        sys.stdout.flush()
        time.sleep(0.25)


def contagem(segundos, texto):
    for s in range(segundos, 0, -1):
        sys.stdout.write('\r  %s: %2d s ' % (texto, s))
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write('\r' + ' ' * 60 + '\r')


class Sessao:
    def __init__(self, board, dev, n, nomes):
        from brainflow.board_shim import BoardShim
        self.board = board
        self.dev = dev
        self.n = n
        self.nomes = nomes
        descr = BoardShim.get_board_descr(board.board_id)
        self.eeg = descr['eeg_channels'][:n]
        self.lo = descr['other_channels'][0]

    def config(self, attr, value):
        self.dev.save(attr)
        if self.dev.read(attr) != value:
            self.board.config_board('%s=%s' % (attr, value))

    def montar(self):
        print('== montagem')
        for ch in range(self.n):
            self.config('in_voltage%d_input_mux' % ch, 'normal')
        self.config('test_signal', 'off')
        for ch in range(self.n):
            self.config('in_voltage%d_hardwaregain' % ch, '24')
            self.config('in_voltage%d_bias_sense_p' % ch, '1')
            self.config('in_voltage%d_bias_sense_n' % ch, '1')
            self.config('in_voltage%d_srb2' % ch, '0')
        self.config('montage', 'referential')
        self.config('bias_drive', 'on')
        print('  montage %s, bias_drive %s, ganho 24, BIAS de P e N dos %d canais'
              % (self.dev.read('montage'), self.dev.read('bias_drive'), self.n))

    def contato(self):
        print('\n== contato (lead-off DC, 6 nA, 3 s)')
        self.config('lead_off_frequency', 'dc')
        self.config('lead_off_current_nanoamp', '6')
        for ch in range(self.n):
            self.config('in_voltage%d_lead_off_sense_p' % ch, '1')
            self.config('in_voltage%d_lead_off_sense_n' % ch, '1' if ch == 0 else '0')
        self.config('bias_lead_off', 'on')
        self.board.start_stream()
        time.sleep(3)
        data = self.board.get_board_data()
        self.board.stop_stream()
        bias = self.dev.read('bias_lead_off_status')
        words = [int(x) for x in data[self.lo][f3.DISCARD:]]
        ruins = []
        for ch in range(self.n):
            frac = sum(1 for w in words if (w >> 8) & (1 << ch)) / len(words)
            estado = 'solto' if frac > 0.5 else ('instavel' if frac > 0 else 'ok')
            print('  %-4s %-8s (%3.0f %% das amostras com a entrada aberta)' % (self.nomes[ch], estado, frac * 100))
            if estado != 'ok':
                ruins.append(self.nomes[ch])
        frac = sum(1 for w in words if w & 1) / len(words)
        estado = 'solto' if frac > 0.5 else ('instavel' if frac > 0 else 'ok')
        print('  %-4s %-8s (%3.0f %%)' % ('REF', estado, frac * 100))
        if estado != 'ok':
            ruins.append('REF')
        print('  %-4s %s' % ('BIAS', bias))
        if bias != 'connected':
            ruins.append('BIAS')
        for ch in range(self.n):
            self.config('in_voltage%d_lead_off_sense_p' % ch, '0')
            self.config('in_voltage%d_lead_off_sense_n' % ch, '0')
        self.config('bias_lead_off', 'off')
        return ruins

    def qualidade(self):
        from brainflow.data_filter import DataFilter, DetrendOperations, FilterTypes
        print('\n== qualidade, 5 s (uV)')
        self.board.start_stream()
        time.sleep(5)
        data = self.board.get_board_data()
        self.board.stop_stream()
        print('  canal  RMS 1-40 Hz   60 Hz (pico)   faixa bruta')
        for ch, row in enumerate(self.eeg):
            x = data[row][f3.DISCARD:].copy()
            bruto = max(x) - min(x)
            DataFilter.detrend(x, DetrendOperations.CONSTANT.value)
            y = x.copy()
            DataFilter.perform_bandpass(y, RATE, 1.0, 40.0, 4, FilterTypes.BUTTERWORTH_ZERO_PHASE.value, 0)
            z = x.copy()
            DataFilter.perform_bandpass(z, RATE, 58.0, 62.0, 4, FilterTypes.BUTTERWORTH_ZERO_PHASE.value, 0)
            rms = statistics.pstdev(y[RATE:-RATE])
            rede = statistics.pstdev(z[RATE:-RATE]) * 2 ** 0.5
            print('  %-5s %9.1f   %12.1f   %11.0f' % (self.nomes[ch], rms, rede, bruto))
        print('  EEG em repouso costuma dar algumas a poucas dezenas de uV RMS. Centenas de uV de 60 Hz')
        print('  pedem eletrodo melhor (gel, pele limpa) ou cabos mais curtos e juntos.')

    def gravar(self, arquivo):
        print('\n== gravacao: %s' % arquivo)
        print('  ao vivo em %s (rode o ao_vivo_pc.py no PC)' % STREAM)
        self.board.start_stream(450000, 'file://%s:w' % arquivo)
        self.board.add_streamer(STREAM)
        time.sleep(2)
        for c in range(1, CICLOS + 1):
            print('  ciclo %d/%d: olhos ABERTOS, olhe para um ponto fixo, sem piscar de proposito' % (c, CICLOS))
            self.board.insert_marker(M_ABERTOS)
            contagem(ABERTOS_S, 'olhos abertos')
            print('  ciclo %d/%d: FECHE os olhos e relaxe; abra no bipe' % (c, CICLOS))
            bipe(1)
            self.board.insert_marker(M_FECHADOS)
            time.sleep(FECHADOS_S)
            bipe(2)
        print('  agora %d piscadas firmes, uma a cada bipe' % PISCADAS)
        time.sleep(3)
        for i in range(PISCADAS):
            bipe(1)
            self.board.insert_marker(M_PISCADA)
            time.sleep(PISCADA_S - 0.25)
        self.board.insert_marker(M_FIM)
        time.sleep(2)
        self.board.stop_stream()
        print('  pronto')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--nomes', default='Fp1,Fp2,C3,C4,P7,P8,O1,O2',
                        help='posicao de cada canal, na ordem CH1..CH8')
    parser.add_argument('--arquivo', default=time.strftime('sessao-%Y%m%d-%H%M.csv'))
    parser.add_argument('--dev', help='diretorio sysfs (so para ensaio sem a placa)')
    parser.add_argument('--file', default='', help='dispositivo de caracteres (so para ensaio sem a placa)')
    parser.add_argument('--rapido', action='store_true', help='duracoes / 10 (so para ensaio)')
    args = parser.parse_args()
    if args.rapido:
        global ABERTOS_S, FECHADOS_S, PISCADA_S
        ABERTOS_S, FECHADOS_S, PISCADA_S = ABERTOS_S // 10, FECHADOS_S // 10, 1

    from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds

    import glob
    found = [os.path.dirname(p) for p in glob.glob('/sys/bus/iio/devices/iio:device*/name')
             if open(p).read().startswith('ads1299')]
    if args.dev:
        found = [args.dev]
    params = BrainFlowInputParams()
    params.serial_port = found[0] if len(found) == 1 else ''
    params.file = args.file
    board = BoardShim(BoardIds.ADS1299_IIO_BOARD, params)
    if len(found) != 1:
        print('nenhum ou mais de um ads1299: %s' % found)
        return 2
    dev = f3.Device(found[0])
    n = 8
    nomes = args.nomes.split(',')
    if len(nomes) != n:
        print('--nomes precisa de %d posicoes' % n)
        return 2

    print('A Pi esta so na bateria, sem nenhum cabo para algo ligado na tomada? [s/N] ', end='')
    if input().strip().lower() != 's':
        print('nada feito')
        return 2

    dev.save('sampling_frequency')
    board.prepare_session()
    s = Sessao(board, dev, n, nomes)
    try:
        s.montar()
        ruins = s.contato()
        if ruins:
            print('  ajuste %s antes de gravar.' % ', '.join(ruins))
        s.qualidade()
        print('\nGravar agora (cerca de %d min)? [s/N] ' % ((CICLOS * (ABERTOS_S + FECHADOS_S) + PISCADAS * PISCADA_S) // 60 + 1), end='')
        if input().strip().lower() == 's':
            s.gravar(os.path.abspath(args.arquivo))
            uid, gid = os.environ.get('SUDO_UID'), os.environ.get('SUDO_GID')
            if uid and gid:
                os.chown(args.arquivo, int(uid), int(gid))
            print('\nNo PC: scp <usuario>@<pi>:%s . && build/venv/bin/python pi-test/analise.py %s'
                  % (os.path.abspath(args.arquivo), os.path.basename(args.arquivo)))
    finally:
        board.release_session()
        dev.restore()
    return 0


if __name__ == '__main__':
    sys.exit(main())
