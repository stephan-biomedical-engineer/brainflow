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

## D na segunda rodada (2026-10-22)

Na rodada sobre `b4eda311`, só o D falhou. O `lead_off_status` era `ff 00`, mas a linha de
lead-off teve 3 valores distintos, com moda `0xfb00`: o canal 3 aparecia conectado na maior parte
do tempo. Na primeira rodada, a linha tinha sido `0xff00` do começo ao fim. Uma entrada aberta,
com a corrente padrão de lead-off, fica perto do limiar, e o comparador oscila. O D agora usa
6 µA, que levam a entrada aberta firme ao trilho. Sem nada ligado no DB25, isso é inofensivo. O
critério também ficou mais estrito: um valor só, igual ao `lead_off_status`. A contagem de cada
valor aparece na saída.

## A na terceira rodada (2026-10-22): uma lacuna de 16,1 ms

Na rodada sobre `de9d7d8c`, só o A falhou: um passo de 16,1 ms entre timestamps, com
`lost_samples` 0 e os números de pacote sem buraco. Os timestamps são do driver, e o BrainFlow só
os decodifica. Há duas explicações possíveis:

- O driver perdeu ~3 conversões sem contar. Uma interrupção do DRDY atrasada junta as bordas
  seguintes.
- O relógio `realtime` foi ajustado no meio da gravação.

O `lacunas.py` lê o buffer sem o BrainFlow, 3 minutos com o relógio `monotonic` e 3 com o
`realtime`. Ele compara as lacunas com o `lost_samples` e com as interrupções do DRDY:

```sh
cd ~/brainflow && git pull
build/venv/bin/python pi-test/lacunas.py --autoteste                 # esperado: tudo certo
sudo build/venv/bin/python pi-test/lacunas.py 2>&1 | tee ~/lacunas.txt  # cerca de 6 minutos
```

A leitura do resultado está no começo do roteiro.

Resultado (2026-10-22): 3 min em `monotonic` e 3 min em `realtime`, sem o BrainFlow, sem nenhuma
lacuna. As interrupções do DRDY bateram com as amostras (44984 e 44985), e `lost_samples` ficou
em 0. O evento não se repetiu. Agora o roteiro tem uma fase pelo BrainFlow. Se as lacunas só
aparecem com ele, é a carga que ele põe no sistema (uma leitura por amostra) que atrasa a
interrupção:

```sh
cd ~/brainflow && git pull
sudo build/venv/bin/python pi-test/lacunas.py --so-brainflow --seconds 600 2>&1 | tee ~/lacunas-bf.txt
```

# Usar o BrainFlow numa pessoa, como na PiEEG

Até aqui, a placa foi validada contra leituras sem o BrainFlow. Esta parte usa o framework como
um usuário da PiEEG usaria. Uma touca de 8 canais, referência no SRB1 e BIAS vão numa pessoa. O
EEG aparece ao vivo no PC e é gravado com marcadores. A análise usa só o `DataFilter` do
BrainFlow. Dois sinais fisiológicos mostram que a cadeia inteira extrai EEG:

- **Efeito Berger:** o alfa (8 a 13 Hz) nos eletrodos occipitais cresce com os olhos fechados.
- **Piscadas:** picos de 100 a 400 µV nos eletrodos da testa.

## Segurança

A placa não é um equipamento médico e não tem isolação. Com os eletrodos na pessoa, a Pi fica
**só na bateria** (power bank), sem nenhum cabo para algo ligado na tomada: nem fonte, nem HDMI,
nem USB para o PC. A ligação com o PC é só pelo Wi-Fi. O PC pode ficar na tomada. É o mesmo
aviso da PiEEG. O `sessao.py` pergunta isso antes de começar.

## Montagem

- A touca nas posições do `--nomes`, na ordem CH1 a CH8. O padrão é o da PiEEG:
  `Fp1,Fp2,C3,C4,P7,P8,O1,O2`. A análise procura os canais occipitais pelo nome (começa com `O`)
  e os da testa também (começa com `Fp`).
- A chave SRB1 na posição do eletrodo de referência (um lóbulo da orelha), não em GNDA.
- O eletrodo de BIAS no outro lóbulo.

O `sessao.py` configura o resto pelo `config_board`:
- `montage referential` (o SRB1 nas entradas negativas);
- `bias_drive on`, com o BIAS derivado das entradas P e N dos 8 canais, como no padrão da OpenBCI;
- ganho 24, MUX normal e gerador desligado.

No fim, ele devolve tudo como estava.

## No PC (este, onde o BrainFlow do fork está compilado)

O PC precisa receber o multicast UDP de 225.1.1.1:6677. **Neste PC, o multicast não chegou nem
de si para si mesmo** (2026-10-22). Nem o exemplo da placa sintética do BrainFlow funcionou. O
suspeito é o firewall:

```sh
sudo ufw status                     # se "active":
sudo ufw allow 6677/udp
```

Depois, com a Pi ligada e o Wi-Fi em comum:

```sh
cd ~/Documents/TCC/brainflow        # ramo ads1299-iio-pi-test
build/venv/bin/python pi-test/ao_vivo_pc.py
```

A janela fica esperando até a Pi começar a gravar. Se nada chegar, a rede pode estar isolando
os clientes. Nesse caso, grave assim mesmo, e depois reveja a sessão gravada com
`ao_vivo_pc.py --arquivo sessao-....csv`.

## Na Pi

```sh
cd ~/brainflow && git pull
sudo build/venv/bin/python pi-test/sessao.py 2>&1 | tee ~/sessao.txt
```

O `sessao.py` faz quatro coisas, em ordem:

1. **Contato:** lead-off DC de 6 nA por 3 s, um eletrodo por linha, mais REF e BIAS. Os que
   saírem `solto` ou `instavel` precisam de ajuste (gel, cabelo afastado); rode de novo.
2. **Qualidade:** 5 s, com o RMS em 1 a 40 Hz e o 60 Hz de cada canal.
3. **Gravação** (cerca de 4 min), depois da confirmação:
   - 3 ciclos de 30 s de olhos abertos e 30 s de olhos fechados. Um bipe no terminal do PC (pelo
     ssh) avisa a hora de abrir;
   - depois, 10 piscadas firmes, uma a cada bipe.
4. **No fim,** mostra o comando para copiar o arquivo.

## Análise, no PC

```sh
scp stephan@stephan-rpi:~/brainflow/sessao-AAAAMMDD-HHMM.csv .
build/venv/bin/python pi-test/analise.py sessao-AAAAMMDD-HHMM.csv
```

A análise faz o seguinte, tudo com o `DataFilter`:
- filtros: média tirada, 60 Hz removido e passa-banda de 1 a 40 Hz;
- PSD de Welch por condição;
- razão de alfa entre olhos fechados e abertos, por canal;
- potência relativa por banda (`get_avg_band_powers`);
- piscadas detectadas nas deixas.

Ela grava `-psd.png` e `-piscadas.png` ao lado do arquivo. Os critérios estão no começo do
roteiro. O `--autoteste` gera duas sessões sintéticas, uma com alfa e piscadas e outra sem, e
confere que a primeira passa e a segunda falha.

Ensaio no PC (2026-10-22), com o dispositivo falso e durações divididas por 10: o `sessao.py`
rodou até o fim, gravou os 17 marcadores na ordem e devolveu os atributos. O visualizador
desenhou uma sessão sintética pela `PLAYBACK_FILE_BOARD`. O transporte por multicast não pôde
ser visto aqui. Nada disso rodou numa pessoa.
