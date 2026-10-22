#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
#
# Formas de onda sem eletrodos: o gerador interno do ADS1299 pelo BrainFlow,
# transmitido ao PC como numa sessao de verdade (streaming_board://225.1.1.1:6677).
#
#   sudo build/venv/bin/python pi-test/demo_sinais.py [--segundos 120]
#   no PC: build/venv/bin/python pi-test/ao_vivo_pc.py --bruto --escala 5000
#
#   CH1-CH6: gerador, alternando a cada 20 s entre 1x_slow (quadrada de
#            fCLK/2^21, ~1 Hz, 3,75 mV pico a pico) e 2x_fast (fCLK/2^20,
#            ~2 Hz, 7,5 mV). Trocar o gerador pede o stream parado, entao ha
#            um corte de alguns centesimos de segundo a cada troca.
#   CH7-CH8: entradas em curto, o piso de ruido (~1 uV).
#
# Nada vai numa pessoa. Deixa o dispositivo como encontrou.

import argparse
import glob
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('f3', os.path.join(HERE, 'teste-f3.py'))
f3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(f3)

STREAM = 'streaming_board://225.1.1.1:6677'
TROCA_S = 20


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--segundos', type=int, default=120)
    parser.add_argument('--dev', help='diretorio sysfs (so para ensaio sem a placa)')
    parser.add_argument('--file', default='', help='dispositivo de caracteres (so para ensaio sem a placa)')
    args = parser.parse_args()

    from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds

    found = [os.path.dirname(p) for p in glob.glob('/sys/bus/iio/devices/iio:device*/name')
             if open(p).read().startswith('ads1299')]
    if args.dev:
        found = [args.dev]
    if len(found) != 1:
        print('nenhum ou mais de um ads1299: %s' % found)
        return 2
    dev = f3.Device(found[0])
    params = BrainFlowInputParams()
    params.serial_port = found[0]
    params.file = args.file
    board = BoardShim(BoardIds.ADS1299_IIO_BOARD, params)
    dev.save('sampling_frequency')
    board.prepare_session()

    def config(attr, value):
        dev.save(attr)
        if dev.read(attr) != value:
            board.config_board('%s=%s' % (attr, value))

    try:
        for ch in range(8):
            config('in_voltage%d_hardwaregain' % ch, '24')
            config('in_voltage%d_input_mux' % ch, 'shorted')
        sinais = ['1x_slow', '2x_fast']
        fim = time.monotonic() + args.segundos
        k = 0
        print('transmitindo em %s por %d s; no PC: ao_vivo_pc.py --bruto --escala 5000' % (STREAM, args.segundos))
        while time.monotonic() < fim:
            sinal = sinais[k % 2]
            for ch in range(6):
                config('in_voltage%d_input_mux' % ch, 'shorted')   # o MUX sai do gerador antes da troca
            config('test_signal', sinal)
            for ch in range(6):
                config('in_voltage%d_input_mux' % ch, 'test_signal')
            board.start_stream()
            if k == 0:
                board.add_streamer(STREAM)   # os streamers duram entre um stream e outro
            print('  CH1-CH6 em %s, CH7-CH8 em curto' % sinal)
            time.sleep(min(TROCA_S, max(0, fim - time.monotonic())))
            board.stop_stream()
            k += 1
    except KeyboardInterrupt:
        print('  interrompido')
    finally:
        board.release_session()
        dev.restore()
    return 0


if __name__ == '__main__':
    sys.exit(main())
