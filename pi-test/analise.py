#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
#
# Analise de uma sessao gravada pelo sessao.py, so com o BrainFlow
# (DataFilter): filtros, PSD, potencia por banda, efeito Berger e piscadas.
#
#   build/venv/bin/python pi-test/analise.py sessao-AAAAMMDD-HHMM.csv [--nomes ...]
#   build/venv/bin/python pi-test/analise.py --autoteste
#       gera duas sessoes sinteticas, uma com alfa e piscadas e outra sem, e
#       confere que a primeira passa e a segunda falha
#
# Cada canal: tirada a media, 60 Hz removido, passa-banda 1-40 Hz (Butterworth
# de fase zero, ordem 4). Segmentos pelos marcadores: 1 olhos abertos, 2
# fechados, 3 piscada, 9 fim; os 2 primeiros segundos de cada segmento ficam
# fora. PSD de Welch (512 pontos, metade sobreposta, Hanning).
#
# Criterios:
#   Berger  - em pelo menos um canal occipital (nome comecando com O), a
#             potencia de alfa (8-13 Hz) com os olhos fechados >= 1,5 vez a
#             de olhos abertos, e o pico do PSD de 7 a 14 Hz entre 8 e 13 Hz
#   piscada - em pelo menos 8 das 10 deixas, um pico em Fp1 ou Fp2 (filtro
#             1-10 Hz) acima de max(40 uV, 6 x o desvio mediano absoluto do
#             repouso de olhos abertos) nos 1,5 s depois do bipe
#
# Grava as figuras ao lado do arquivo: <nome>-psd.png e <nome>-piscadas.png.

import argparse
import os
import statistics
import sys
import tempfile

RATE = 250
M_ABERTOS, M_FECHADOS, M_PISCADA, M_FIM = 1.0, 2.0, 3.0, 9.0
NFFT = 512
SKIP = 2 * RATE
RAZAO_BERGER = 1.5
PISCADAS_MIN = 8


def descritor():
    from brainflow.board_shim import BoardShim, BoardIds
    return BoardShim.get_board_descr(BoardIds.ADS1299_IIO_BOARD.value)


def filtrar(x, lo=1.0, hi=40.0):
    from brainflow.data_filter import DataFilter, DetrendOperations, FilterTypes, NoiseTypes
    x = x.copy()
    DataFilter.detrend(x, DetrendOperations.CONSTANT.value)
    DataFilter.remove_environmental_noise(x, RATE, NoiseTypes.SIXTY.value)
    DataFilter.perform_bandpass(x, RATE, lo, hi, 4, FilterTypes.BUTTERWORTH_ZERO_PHASE.value, 0)
    return x


def segmentos(markers):
    """[(rotulo, inicio, fim)] em amostras, de cada marcador ate o proximo."""
    idx = [(i, m) for i, m in enumerate(markers) if m]
    out = []
    for k, (i, m) in enumerate(idx):
        fim = idx[k + 1][0] if k + 1 < len(idx) else len(markers)
        out.append((m, i, fim))
    return out


def psd(x):
    from brainflow.data_filter import DataFilter, WindowOperations
    return DataFilter.get_psd_welch(x.copy(), NFFT, NFFT // 2, RATE, WindowOperations.HANNING.value)


def analisar(arquivo, nomes, figuras=True):
    import numpy as np
    from brainflow.data_filter import DataFilter
    d = descritor()
    data = DataFilter.read_file(arquivo)
    eeg = d['eeg_channels']
    markers = data[d['marker_channel']]
    segs = segmentos(markers)
    filt = [filtrar(data[r]) for r in eeg]
    print('%s: %d amostras (%.1f s), %d marcadores' % (os.path.basename(arquivo), data.shape[1],
                                                      data.shape[1] / RATE, sum(1 for m in markers if m)))
    resultado = {}

    # Berger
    print('\n== alfa (8-13 Hz), olhos fechados contra abertos')
    psds = {}
    berger_ok = False
    for ch, nome in enumerate(nomes):
        pot = {M_ABERTOS: [], M_FECHADOS: []}
        acum = {M_ABERTOS: None, M_FECHADOS: None}
        for m, a, b in segs:
            if m in pot and b - a - SKIP >= NFFT:
                p = psd(filt[ch][a + SKIP:b])
                pot[m].append(DataFilter.get_band_power(p, 8.0, 13.0))
                acum[m] = np.array(p[0]) if acum[m] is None else acum[m] + np.array(p[0])
                freqs = np.array(p[1])
        if not pot[M_ABERTOS] or not pot[M_FECHADOS]:
            print('  %-4s sem segmentos dos dois tipos' % nome)
            continue
        ab, fe = statistics.mean(pot[M_ABERTOS]), statistics.mean(pot[M_FECHADOS])
        psds[nome] = (freqs, acum[M_ABERTOS] / len(pot[M_ABERTOS]), acum[M_FECHADOS] / len(pot[M_FECHADOS]))
        faixa = (freqs >= 7) & (freqs <= 14)
        pico = freqs[faixa][np.argmax(psds[nome][2][faixa])]
        razao = fe / ab if ab > 0 else float('inf')
        occ = nome.upper().startswith('O')
        ok = occ and razao >= RAZAO_BERGER and 8 <= pico <= 13
        berger_ok = berger_ok or ok
        print('  %-4s abertos %8.2f  fechados %8.2f uV^2  razao %5.2f  pico %4.1f Hz%s'
              % (nome, ab, fe, razao, pico, '  <- occipital' + (', ok' if ok else '') if occ else ''))
    resultado['berger'] = berger_ok

    # bandas, media dos canais, por condicao
    print('\n== potencia relativa por banda, media dos canais (DataFilter.get_avg_band_powers)')
    print('  condicao   delta   teta   alfa   beta  gama')
    for m, rot in ((M_ABERTOS, 'abertos'), (M_FECHADOS, 'fechados')):
        cols = [(a + SKIP, b) for mm, a, b in segs if mm == m and b - a - SKIP >= NFFT]
        if not cols:
            continue
        sub = np.concatenate([data[:, a:b] for a, b in cols], axis=1)
        bands = DataFilter.get_avg_band_powers(sub, eeg, RATE, True)[0]
        print('  %-9s ' % rot + ' '.join('%6.2f' % v for v in bands))

    # piscadas
    print('\n== piscadas')
    fps = [ch for ch, nome in enumerate(nomes) if nome.upper().startswith('FP')]
    deixas = [a for m, a, b in segs if m == M_PISCADA]
    lenta = {ch: filtrar(data[eeg[ch]], 1.0, 10.0) for ch in fps}
    vistas = 0
    if fps and deixas:
        repouso = [(a + SKIP, b) for m, a, b in segs if m == M_ABERTOS]
        limiar = {}
        for ch in fps:
            base = np.concatenate([lenta[ch][a:b] for a, b in repouso]) if repouso else lenta[ch]
            mad = float(np.median(np.abs(base - np.median(base))))
            limiar[ch] = max(40.0, 6 * mad)
        for i in deixas:
            janela = slice(i, i + int(1.5 * RATE))
            if any(np.max(np.abs(lenta[ch][janela])) > limiar[ch] for ch in fps):
                vistas += 1
        print('  %d de %d deixas com piscada (limiar: %s)' % (vistas, len(deixas), ', '.join(
            '%s %.0f uV' % (nomes[ch], limiar[ch]) for ch in fps)))
    else:
        print('  sem canal Fp ou sem deixas de piscada')
    resultado['piscadas'] = vistas >= PISCADAS_MIN

    if figuras:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        base = os.path.splitext(arquivo)[0]
        fig, axs = plt.subplots(2, 4, figsize=(14, 6), sharex=True)
        for ax, nome in zip(axs.flat, nomes):
            if nome in psds:
                f, ab, fe = psds[nome]
                faixa = (f >= 1) & (f <= 40)
                ax.semilogy(f[faixa], ab[faixa], label='abertos')
                ax.semilogy(f[faixa], fe[faixa], label='fechados')
                ax.axvspan(8, 13, alpha=0.15)
            ax.set_title(nome)
            ax.set_xlim(1, 40)
        if psds:
            axs.flat[0].legend()
        fig.supxlabel('Hz')
        fig.supylabel('uV^2/Hz')
        fig.tight_layout()
        fig.savefig(base + '-psd.png', dpi=100)
        if fps and deixas:
            fig, ax = plt.subplots(figsize=(12, 4))
            a, b = deixas[0] - RATE, min(deixas[-1] + 3 * RATE, data.shape[1])
            t = [i / RATE for i in range(b - a)]
            for ch in fps:
                ax.plot(t, lenta[ch][a:b], label=nomes[ch], linewidth=0.8)
            for i in deixas:
                ax.axvline((i - a) / RATE, color='k', linestyle='--', linewidth=0.6)
            ax.set_xlabel('s')
            ax.set_ylabel('uV (1-10 Hz)')
            ax.legend()
            fig.tight_layout()
            fig.savefig(base + '-piscadas.png', dpi=100)
        print('\nfiguras: %s-psd.png%s' % (base, ', %s-piscadas.png' % base if fps and deixas else ''))

    print('\n%s efeito Berger (alfa occipital >= %.1f x com olhos fechados, pico em 8-13 Hz)'
          % ('ok   ' if resultado['berger'] else 'FALHA', RAZAO_BERGER))
    print('%s piscadas (>= %d de 10)' % ('ok   ' if resultado['piscadas'] else 'FALHA', PISCADAS_MIN))
    return resultado


def gerar(arquivo, com_eeg=True, seed=1):
    """Uma sessao sintetica no formato da placa 69, com o protocolo do sessao.py."""
    import numpy as np
    from brainflow.data_filter import DataFilter
    rng = np.random.default_rng(seed)
    d = descritor()
    plano = [(M_ABERTOS, 30), (M_FECHADOS, 30)] * 3 + [(M_PISCADA, 3)] * 10 + [(M_FIM, 2)]
    n = sum(s for _, s in plano) * RATE + 2 * RATE
    data = np.zeros((d['num_rows'], n))
    t = np.arange(n) / RATE
    data[d['package_num_channel']] = np.arange(n)
    data[d['timestamp_channel']] = 1.79e9 + t
    alfa_amp = np.zeros(n)
    pos = 2 * RATE
    for m, s in plano:
        data[d['marker_channel']][pos] = m
        if m == M_FECHADOS:
            alfa_amp[pos:pos + s * RATE] = 1.0
        if m == M_PISCADA and com_eeg:
            k = pos + int(0.35 * RATE)
            w = int(0.3 * RATE)
            for ch in (0, 1):
                data[d['eeg_channels'][ch]][k:k + w] += 200 * np.sin(np.pi * np.arange(w) / w)
        pos += s * RATE
    for ch, row in enumerate(d['eeg_channels']):
        ruido = np.cumsum(rng.normal(0, 1, n))
        ruido -= np.convolve(ruido, np.ones(250) / 250, mode='same')
        occ = ch >= 6
        alfa = (20.0 if occ else 6.0) * alfa_amp + 4.0 if com_eeg else 4.0
        data[row] += 3 * ruido + alfa * np.sin(2 * np.pi * 10.0 * t + ch) \
            + 30 * np.sin(2 * np.pi * 60.0 * t) + 500
    DataFilter.write_file(data, arquivo, 'w')


def autoteste():
    nomes = 'Fp1,Fp2,C3,C4,P7,P8,O1,O2'.split(',')
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        for com, esperado in ((True, True), (False, False)):
            arq = os.path.join(tmp, 'sintetica-%s.csv' % ('eeg' if com else 'sem'))
            gerar(arq, com)
            print('\n######## %s' % ('com alfa e piscadas: deve passar' if com else 'sem alfa nem piscadas: deve falhar'))
            r = analisar(arq, nomes, figuras=True)
            for k in ('berger', 'piscadas'):
                if r[k] != esperado:
                    print('AUTOTESTE: %s deu %s, esperado %s' % (k, r[k], esperado))
                    ok = False
    print('\n' + ('tudo certo' if ok else 'ALGO ERRADO'))
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('arquivo', nargs='?')
    parser.add_argument('--nomes', default='Fp1,Fp2,C3,C4,P7,P8,O1,O2')
    parser.add_argument('--autoteste', action='store_true')
    parser.add_argument('--gerar', help='so grava uma sessao sintetica neste arquivo')
    args = parser.parse_args()
    if args.autoteste:
        return autoteste()
    if args.gerar:
        gerar(args.gerar)
        return 0
    if not args.arquivo:
        parser.error('falta o arquivo')
    r = analisar(args.arquivo, args.nomes.split(','))
    return 0 if all(r.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
