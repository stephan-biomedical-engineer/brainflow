# F3: a placa ADS1299_IIO_BOARD na Raspberry Pi

Este ramo (`ads1299-iio-pi-test`) é o `ads1299-iio-board` com esta pasta a mais. Ela não entra no
PR. O plano é o `docs/PLANO_BRAINFLOW.md` do `ads1299-linux`, e o teste é a F3 dele.

A montagem é a mesma da F4: Pi 4B com Raspberry Pi OS (6.18.50), o driver completo
(`ti-ads1299`, dispositivo `ads1299-8`) carregado pelo `dtoverlay=ads1299,drdy=6,pwdn=5`, a chave
SRB1 em GNDA, nada no DB25 e a ponte USB desconectada. O roteiro não mexe no driver. Ele muda
atributos pelo `config_board` e, no fim, devolve cada um ao valor que tinha.

## Na Pi

O BrainFlow é compilado na própria Pi. Leva alguns minutos; `-j2` evita faltar memória.

```sh
sudo apt install -y git cmake g++ python3-venv
git clone -b ads1299-iio-pi-test https://github.com/stephan-biomedical-engineer/brainflow.git
cd brainflow
git log -1 --format='%h %s'                     # anote o hash

cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_BLE=OFF -DBUILD_BLUETOOTH=OFF \
      -DBUILD_SYNCHRONI_SDK=OFF -DBUILD_TESTS=ON 2>&1 | tail -3
cmake --build build -j2 2>&1 | tee build.log | tail -3
grep -c warning: build.log                       # esperado: 0
build/tests/brainflow_tests 2>&1 | tail -1       # esperado: [  PASSED  ] 55 tests.

python3 -m venv build/venv
build/venv/bin/pip install -e python_package
build/venv/bin/python pi-test/teste-f3.py --autoteste   # esperado: tudo certo
```

O emulador também roda na Pi, sem tocar no ADS1299:

```sh
build/venv/bin/python emulator/brainflow_emulator/ads1299_iio_emulator.py --mode ads1299 2>&1 | tail -1
build/venv/bin/python emulator/brainflow_emulator/ads1299_iio_emulator.py --mode ads1298 2>&1 | tail -1
# esperado nos dois: OK: 0 checks failed
```

Agora o teste contra o ADS1299. Ele leva cerca de 1 minuto:

```sh
cat /sys/bus/iio/devices/iio:device*/name        # esperado: ads1299-8
cat /sys/bus/iio/devices/iio:device*/timestamp_source   # esperado: drdy_irq
sudo dmesg | grep -i ads1299 | tail -2           # o autoteste do probe

sudo build/venv/bin/python pi-test/teste-f3.py 2>&1 | tee ~/f3-teste.txt
grep -v board_logger ~/f3-teste.txt | tail -3    # esperado: 0 falhas
```

Mande de volta o `~/f3-teste.txt`, o `build.log` e a saída dos outros comandos.

## O que o roteiro confere

Cada medida é lida pelo BrainFlow, como um programa do usuário leria.

- **G.** A taxa fica em 250 SPS e não muda pelo `config_board`. Um atributo é lido. Uma escrita
  durante o stream é recusada.
- **A.** São 10 s com as entradas normais. Esperado:
  - 2500 linhas ±2 %, com os números de pacote sem buraco;
  - timestamps do driver com passo mediano de 4 ms, nenhum acima de 6 ms e nenhum fora de ordem;
  - `lost_samples` 0, e o timestamp em tempo Unix.
- **B.** O gerador em `1x_dc`, contra as entradas em curto, no ganho 24. O nível de cada canal, em
  µV, tem de ficar a ±2,5 % de 1875 µV (VREF/2400). O erro também tem de ficar a 0,05 ponto do que
  a F4 mediu nesta Pi com leituras diretas, de −0,076 % a −0,414 %.
- **C.** O ruído com as entradas em curto, convertido de volta em códigos, fica abaixo do teto do
  autoteste: 447 códigos, 10 µV no ganho 24.
- **E.** O mesmo gerador no ganho 1, trocado pelo `config_board`. O erro em µV tem de ficar a
  0,1 ponto do que dão as leituras avulsas (`in_voltageN_raw` × `scale`) no mesmo ganho. Isso
  mostra que a placa relê o `scale` a cada stream: se não relesse, o erro seria de 24 vezes. O
  nível não é o mesmo nos dois ganhos. Neste chip, o ganho 1 se afasta do 24 de −1,7 % a +3,5 %
  conforme o canal (ver o diagnóstico abaixo).
- **F.** A onda quadrada `1x_slow` tem período de 256 amostras: fCLK/2²¹ contra fCLK/8192, a mesma
  razão para qualquer relógio. Uma amostra perdida ou repetida muda o período.
- **D.** A entrada positiva de todo canal é sentida, com as entradas abertas. A linha de lead-off
  tem de ser igual ao `lead_off_status` e mostrar alguma entrada aberta. Sem nada sentido, a linha
  fica em 0.

O roteiro foi rodado aqui no PC, inteiro, contra um dispositivo falso que não simula o gerador, o
curto nem o lead-off. G e A passaram. As verificações de B a F falharam, como deviam com dados
falsos, e os atributos voltaram aos valores de antes. Na placa ele nunca rodou.

## Diagnóstico da falha do E (2026-10-22)

Na primeira rodada da F3, só o E falhou, e só no canal 7: o nível do gerador no ganho 1 saiu 3,5 %
acima do ganho 24. Os outros canais ficaram entre −1,7 % e +1,5 %. A caracterização
(`BRINGUP_AFE.md` §5.1) mediu o mesmo gerador nos 7 ganhos com leituras avulsas, e a variação
foi de 0,06 % a 0,72 %. O E usa o buffer contínuo. O `ganho1.py` mede o gerador dos três jeitos,
intercalados, nos ganhos 1 e 24, em três rodadas:

```sh
cd ~/brainflow
git pull                                          # traz o ganho1.py
build/venv/bin/python pi-test/ganho1.py --autoteste      # esperado: tudo certo
sudo build/venv/bin/python pi-test/ganho1.py 2>&1 | tee ~/ganho1.txt   # cerca de 1,5 minuto
```

- Se o `brainflow` e o `buffer` (lido em Python, sem o BrainFlow) baterem a 0,1 ponto, a placa
  está certa. Esse é o critério do roteiro.
- Se os dois discordarem da `avulsa` no ganho 1, a diferença é do modo de aquisição (driver ou
  chip), e vira uma questão do driver.

O offset em códigos aparece ao lado, para ver se é ele que se move entre uma medida e outra.
