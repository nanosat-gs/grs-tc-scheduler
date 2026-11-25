# TC Scheduler

Decide o que a estação terrestre do SpaceLab rastreia, e quando.

Fecha o circuito entre os dois lados da estação: o operador cadastra satélites
e telecomandos na interface web, e o Station Manager sabe apontar a antena e
sintonizar os rádios — mas alguém precisa dizer qual passagem e quando. É este
serviço. Ele também é o **único que escreve** no banco da estação, e serve o
plano por HTTP para o painel do operador.

Faz parte da estação terrestre orquestrada em
[nanosat-gs/grs-station](https://github.com/nanosat-gs/grs-station).

## O que faz

1. Busca os satélites ativos com dado orbital. **Toda passagem é candidata**,
   com ou sem telecomando: o satélite transmite telemetria sem esperar uplink.
2. Propaga 24 h à frente e prevê as passagens; descarta as rasantes (< 5° ou
   < 60 s).
3. Pontua por **forçada pelo operador → prioridade do telecomando à espera →
   quantidade de comandos → elevação máxima**, e escolhe as que não se
   sobrepõem (a estação tem um rotor só).
4. No AOS, manda **uma ordem** ao Station Manager (`track_satellite`), com os
   elementos orbitais e os **downlinks ligados** do satélite. O Station Manager
   conduz a passagem sozinho até o LOS: rotor, Doppler e ajuste fino.
5. Durante a passagem, consulta o Station Manager e guarda o **erro do
   oscilador** que o ajuste fino aprendeu, depois que ele converge.
6. Serve o plano e recebe as ações do operador por HTTP em `:5591`.
7. **Com `UPLINK_ENABLED=1`:** fecha a malha de cada telecomando
   (`tc_lifecycle.py`). Durante a passagem, ou numa sessão manual:
   - pede ao `grs-uplink` (:5593) que transmita **um TC por vez**;
   - marca `sent` só com o `ack` do rádio;
   - casa a resposta do satélite, decodificada pelo `grs-telemetry-decoder`, e
     fecha o TC em `confirmed`, `failed`, `response_received` ou `no_response`.

   Ver `docs/schema-contract.md`, "O ciclo de vida com uplink".

O operador pode **pular**, **forçar** ou **desfazer** uma passagem, ligar ou
desligar a recepção por satélite e cadastrar os downlinks (nome e frequência) —
tudo pela aba Previsão e pelo detalhe do satélite no painel do GRS Manager, que
repassa para cá. Uma ação replaneja em segundos, sem esperar o ciclo de 5 min.

## API

Em `:5591`. No compose da estação, publicada só em `127.0.0.1`: ela escreve e
não tem autenticação.

| Método | Caminho | O quê |
|---|---|---|
| `GET` | `/health` | Processo vivo e `database_available` |
| `GET` | `/api/station` | Satélites e próximas passagens (200 mesmo sem banco) |
| `GET` | `/api/satellite/<code>` | Detalhe de um satélite |
| `GET` | `/api/passes` | Todas as passagens previstas, escolhidas ou não, com o motivo e a decisão do operador; e, por satélite, a recepção e os downlinks (rádio que ouve cada um e o erro de oscilador medido) |
| `PUT` | `/api/passes/decision` | `{"satellite_code", "aos", "decision": "skip" \| "force" \| null}` |
| `PUT` | `/api/satellites/<code>/reception` | Liga/desliga o rastreio de passagens para recepção |
| `PUT` | `/api/satellites/<code>/downlinks` | Substitui a lista: `{"downlinks": [{name, frequency_hz, enabled}]}` — a ordem importa: o primeiro ligado é a referência do Doppler |
| `POST` | `/api/tle/refresh` | Revalida os elementos orbitais no CelesTrak |
| `POST` | `/api/uplink/session` | Com uplink: `{"satellite": código ou id, "duration_s"}` abre uma sessão manual de uplink (contato fora de passagem, ou o simulador) |
| `DELETE` | `/api/uplink/session/<id>` | Fecha a sessão |
| `GET` | `/api/uplink` | Sessões abertas e as últimas transmissões, com o status do TC e as respostas casadas |
| `GET` | `/api/simulated-pass` | A passagem simulada em curso, ou a próxima, com o TLE dela; `null` sem nenhuma |

Erros das ações: 400 pedido inválido, 404 satélite inexistente, 503 banco fora.

## Instalar e rodar

```bash
pip install -e ".[dev]"
pytest

cp .env.example .env
python -m tc_scheduler.main            # laço + API
python -m tc_scheduler.main --no-api   # só o laço
python -m tc_scheduler.api             # só a API
```

Observar e exercitar:

```bash
python -m tc_scheduler.monitor --watch
python tools/station_demo.py prepare --code SAT-001 --norad-id 44885
python tools/station_demo.py simulate-pass --code SAT-001
python tools/station_demo.py simulate-pass --code FS-2 --proxima                     # uma passagem real, começando agora
python tools/station_demo.py simulate-pass --code FS-2 --proxima --elevacao-min 60
```

O `simulate-pass` sem `--proxima` abre uma janela com a posição do satélite
naquele instante, que quase sempre está abaixo do horizonte. Com `--proxima`,
a janela é a próxima passagem real, com elevação máxima de pelo menos
`--elevacao-min` (padrão 30°), trazida para agora: ângulos, duração e curva de
Doppler de verdade. A órbita da janela é um TLE deslocado no tempo, e é ele
que vai ao Station Manager. Um satélite sem órbita, como o FS-2 na bancada,
usa a de outro: `--orbita-de <NORAD>`, padrão o FloripaSat-1 (44885). O TLE
fica em `GET /api/simulated-pass`. Ver `src/tc_scheduler/simulated_pass.py`.

As variáveis estão comentadas no [.env.example](.env.example). As `GS_*`
precisam bater com as do Station Manager, e `STATION_RADIOS` aqui só informa ao
painel qual rádio ouve cada downlink — quem roteia de fato é o Station Manager,
com a mesma variável.

## Banco

O schema principal é **propriedade do TC Generator**; o contrato entre os dois
está em [docs/schema-contract.md](docs/schema-contract.md), e `schema_check.py`
o confere no boot.

O que é deste serviço vive no schema **`mission_control`**, criado no boot:
recepção por satélite, downlinks (com o erro de oscilador medido), decisões do
operador e o resultado do último replanejamento. Detalhes no mesmo documento e
no `CLAUDE.md`.
