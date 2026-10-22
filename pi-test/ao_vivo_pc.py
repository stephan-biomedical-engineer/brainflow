#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
#
# O EEG ao vivo no PC, recebido da Pi pelo streaming do proprio BrainFlow: a Pi
# (sessao.py) transmite com add_streamer("streaming_board://225.1.1.1:6677"), e
# aqui a STREAMING_BOARD recebe, com master_board = ADS1299_IIO_BOARD, que diz
# o formato. Precisa deste BrainFlow (o do fork), que conhece a placa 69.
#
#   build/venv/bin/python pi-test/ao_vivo_pc.py [--nomes ...] [--janela 5]
#   build/venv/bin/python pi-test/ao_vivo_pc.py --arquivo sessao-....csv
#       revê uma sessao gravada, no ritmo em que foi gravada (PLAYBACK_FILE_BOARD)
#   build/venv/bin/python pi-test/ao_vivo_pc.py --png figura.png --segundos 10
#       sem tela: espera, desenha uma vez, grava e sai
#
# Cada canal: tirada a media, 60 Hz removido, passa-banda 1-40 Hz. Ao lado, a
# potencia relativa de alfa (8-13 Hz) na janela. Linhas verticais marcam os
# marcadores do protocolo (1 abertos, 2 fechados, 3 piscada).
#
# Pi e PC na mesma rede Wi-Fi; algumas redes bloqueiam multicast (isolamento
# de clientes). O PC pode ficar na tomada: nao ha cabo ate a Pi.

import argparse
import os
import sys
import time

RATE = 250


def filtrar(x):
    from brainflow.data_filter import DataFilter, DetrendOperations, FilterTypes, NoiseTypes
    x = x.copy()
    DataFilter.detrend(x, DetrendOperations.CONSTANT.value)
    DataFilter.remove_environmental_noise(x, RATE, NoiseTypes.SIXTY.value)
    DataFilter.perform_bandpass(x, RATE, 1.0, 40.0, 4, FilterTypes.BUTTERWORTH_ZERO_PHASE.value, 0)
    return x


def alfa_relativa(x):
    from brainflow.data_filter import DataFilter, WindowOperations
    nfft = 256
    if len(x) < nfft:
        return float('nan')
    psd = DataFilter.get_psd_welch(x, nfft, nfft // 2, RATE, WindowOperations.HANNING.value)
    total = DataFilter.get_band_power(psd, 1.0, 40.0)
    return DataFilter.get_band_power(psd, 8.0, 13.0) / total if total > 0 else float('nan')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--nomes', default='Fp1,Fp2,C3,C4,P7,P8,O1,O2')
    parser.add_argument('--janela', type=float, default=5.0, help='segundos na tela')
    parser.add_argument('--endereco', default='225.1.1.1')
    parser.add_argument('--porta', type=int, default=6677)
    parser.add_argument('--arquivo', help='rever uma sessao gravada, em vez de receber da Pi')
    parser.add_argument('--bruto', action='store_true', help='so tira a media, sem filtros (para o gerador)')
    parser.add_argument('--escala', type=float, default=100.0, help='uV entre canais')
    parser.add_argument('--png', help='sem tela: grava uma figura e sai')
    parser.add_argument('--segundos', type=float, default=10.0, help='com --png, quanto esperar')
    args = parser.parse_args()

    import matplotlib
    if args.png:
        matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds

    master = BoardIds.ADS1299_IIO_BOARD.value
    descr = BoardShim.get_board_descr(master)
    eeg = descr['eeg_channels']
    marker_row = descr['marker_channel']
    nomes = args.nomes.split(',')

    params = BrainFlowInputParams()
    params.master_board = master
    if args.arquivo:
        params.file = args.arquivo
        board = BoardShim(BoardIds.PLAYBACK_FILE_BOARD, params)
        origem = os.path.basename(args.arquivo)
    else:
        params.ip_address = args.endereco
        params.ip_port = args.porta
        board = BoardShim(BoardIds.STREAMING_BOARD, params)
        origem = '%s:%d' % (args.endereco, args.porta)
    board.prepare_session()
    board.start_stream()
    janela = int(args.janela * RATE)

    fig, ax = plt.subplots(figsize=(12, 8))
    try:
        fig.canvas.manager.set_window_title('ADS1299 pelo BrainFlow')
    except AttributeError:
        pass

    def desenhar():
        data = board.get_current_board_data(janela)
        ax.clear()
        n = data.shape[1]
        if n < RATE:
            ax.set_title('esperando dados de %s (%d amostras)' % (origem, n))
            return n
        t = [i / RATE for i in range(n)]
        passo = args.escala
        for i, row in enumerate(eeg):
            y = data[row] - data[row].mean() if args.bruto else filtrar(data[row])
            ax.plot(t, y - i * passo, linewidth=0.7)
            ax.text(t[-1] + 0.05, -i * passo, '%s  alfa %.0f %%' % (nomes[i], 100 * alfa_relativa(y)),
                    va='center', fontsize=9)
        for i in range(n):
            m = data[marker_row][i]
            if m:
                ax.axvline(t[i], color='k', linestyle='--', linewidth=0.8)
                ax.text(t[i], passo * 0.6, '%g' % m, fontsize=8)
        ax.set_yticks([-i * passo for i in range(len(eeg))])
        ax.set_yticklabels(nomes)
        ax.set_xlabel('s (ultimos %.0f s); %.0f uV entre canais%s'
                      % (args.janela, passo, '; sem filtro' if args.bruto else '; 1-40 Hz, sem 60 Hz'))
        ax.set_xlim(0, args.janela + 1.2)
        ax.set_title('ADS1299_IIO_BOARD, de %s' % origem)
        return n

    try:
        if args.png:
            time.sleep(args.segundos)
            n = desenhar()
            fig.savefig(args.png, dpi=100)
            print('%d amostras na janela; figura em %s' % (n, args.png))
            return 0 if n >= RATE else 1
        plt.ion()
        plt.show()
        visto = time.monotonic()
        while plt.fignum_exists(fig.number):
            desenhar()
            plt.pause(0.2)
            if time.monotonic() - visto >= 2:
                visto = time.monotonic()
                total = board.get_board_data_count()
                print('%s  %d amostras recebidas ate agora' % (time.strftime('%H:%M:%S'), total), flush=True)
    finally:
        board.stop_stream()
        board.release_session()
    return 0


if __name__ == '__main__':
    sys.exit(main())
