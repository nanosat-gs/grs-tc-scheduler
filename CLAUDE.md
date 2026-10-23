# TC Scheduler (`tc_scheduler`)

**Aqui mora a autonomia da estação.** Este serviço decide o que rastrear e
quando, é o **único escritor do banco**, e serve o plano por HTTP para quem
quiser lê-lo.

## Portas

| Porta | Protocolo | Direção |
|---|---|---|
| 5591 | HTTP (Flask) | **expõe** — API do plano e das ações do operador; quem consome é o GRS Manager. No compose, publicada só em `127.0.0.1` (a API escreve e não tem autenticação) |
| 5580 | ZMQ REQ | **consome** — [Station Manager](https://github.com/nanosat-gs/grs-station-manager) |
| 5432 | Postgres | **consome** — o banco do TC Generator |

## Como a autonomia funciona

**Toda passagem é candidata**, tenha telecomando ou não: o satélite transmite
telemetria sem esperar uplink, e a correção de Doppler só atua numa passagem
rastreada. Pontua por **forçada pelo operador → prioridade do telecomando à
espera → quantidade de comandos → elevação máxima**, e escolhe as que não se
sobrepõem (a estação tem um rotor só). O peso de telecomando vale só na
**primeira passagem não pulada** de cada satélite, que é a que leva a fila
inteira; as seguintes disputam como recepção (`mark_telecommand_carriers`).

Manda **uma ordem por passagem** (`track_satellite`) ao Station Manager, não um
setpoint por segundo: planejar é lento e apontar é rápido, e separá-los faz com
que um replanejamento nunca atrase o rotor. A ordem leva os downlinks ligados
do satélite, em ordem (`[{name, frequency_hz}]`), e o Station Manager anuncia
portadora e Doppler de cada um ao rádio da sua faixa.

## Controle do operador sobre passagens

O operador age pelo painel do GRS Manager, que repassa para `api.py`
(`operator_control.py`). Três coisas, guardadas no schema **`mission_control`**,
que é deste serviço (criado no boot por `db.ensure_control_schema`):

| Tabela | O quê |
|---|---|
| `satellite_reception` | Por satélite: `track_passes` (padrão ligado; sem linha = padrão). A coluna `downlink_frequency_hz` é obsoleta: migrada no boot para `satellite_downlinks` e zerada |
| `satellite_downlinks` | As portadoras do satélite: nome, frequência, ligado, ordem, e o erro do oscilador medido pelo ajuste fino (`measured_offset_hz`, `measured_at`) — guardado durante a passagem só depois de a malha convergir (`db.afc_converged`), mantido ao editar sem mudar a frequência. `PUT /api/satellites/<code>/downlinks` substitui a lista inteira (a ordem importa: o primeiro ligado é a referência do Doppler) |
| `pass_decisions` | `skip` ou `force` numa passagem. Desfazer = apagar a linha |
| `pass_predictions` | Resultado do último replanejamento, **todas** as candidatas: `planned`, `conflict` (com quem venceu), `skipped`, `reception_off`. É como a API mostra as que perderam sem compartilhar memória com o laço |

**A passagem é identificada por satélite + AOS previsto**, casados com
tolerância de ±10 min (`PASS_MATCH_TOLERANCE`) — nunca pelo id: as linhas
`planned` são apagadas e recriadas a cada replanejamento. Passagens seguidas do
mesmo satélite distam ~90 min, e uma TLE nova desloca o AOS em segundos.

O laço confere a cada `SCHEDULER_CONTROL_POLL_SECONDS` (5 s) se as tabelas de
controle mudaram (`control_version`) e replaneja na hora: a ação aparece na
tela em segundos, sem esperar o ciclo de 5 min.

Com `track_passes` desligado, o satélite volta ao comportamento antigo: só
entra a passagem que leva telecomando, ou a que o operador forçou.

## O único escritor do banco

Essa regra é a razão de a API existir. Antes, o painel do GRS Manager abria a
própria conexão com o Postgres para mostrar satélites e passagens — dois
serviços de repositórios diferentes escrevendo raw SQL contra o mesmo schema.
Agora quem escreve é quem serve, inclusive as ações do operador: o painel as
manda para cá em vez de abrir conexão própria.

`station_data.py` faz a leitura, `api.py` a expõe. Os caminhos são os mesmos
que o painel já servia, para que o cliente do outro lado seja um repasse de URL.

## O laço e a API dividem o processo

A API roda numa **thread daemon** do mesmo processo. Não há estado
compartilhado: `StationData` abre o próprio engine SQLAlchemy, e engines são
thread-safe.

**A ressalva:** o replanejamento é SGP4 em Python puro, CPU-bound, e **segura a
GIL**. Durante um replan a API pode ficar segundos sem responder. É tolerável
porque o painel busca a cada 15 s e degrada em vez de falhar. Se algum dia
incomodar, `api.py` **já é executável sozinho** (`python -m tc_scheduler.api`):
a saída é um serviço a mais no compose, mesma imagem, sem tocar em código.

## O ciclo de vida do TC tem a sua thread

Com `UPLINK_ENABLED`, o `tc_lifecycle` roda em `lifecycle_runner.py`, thread
própria e engine próprio, e não no laço principal. O pedido ao `grs-uplink`
espera a rajada sair e o rádio confirmar (até `UPLINK_REQUEST_TIMEOUT_MS`):
no laço, isso segurava a ativação da próxima passagem. A thread lê do banco
qual passagem está ativa a cada tick; o um-TC-por-vez continua valendo porque
casar, vencer prazos e enviar seguem em sequência nela. Não chame o atributo
de parada de `_stop`: é método interno do `threading.Thread`.

## Armadilhas

- **`STATION_RADIOS` aqui só informa o painel.** Quem roteia downlink para
  rádio é o Station Manager, com a mesma variável. A regra de `radios.annotate`
  tem de continuar igual à de `mgm8.domain.models.route_downlinks`, ou o
  painel promete um rádio e a estação usa outro.

- **Satélite sem passagem no plano: confira a recepção.** Todo satélite
  `active` com dado orbital entra (`fetch_candidate_satellites`), a menos que o
  operador tenha desligado `track_passes` — aí só entra com telecomando. A aba
  Previsão do painel mostra o motivo de cada passagem (`reception_off`,
  `conflict`, `skipped`).
- **`scheduled_passes` só tem as escolhidas.** As que perderam o conflito vivem
  em `mission_control.pass_predictions`. Quem lê só `scheduled_passes` não vê
  por que uma passagem sumiu.
- **`CREATE TABLE IF NOT EXISTS` não adiciona coluna.** Mudar as tabelas de
  `mission_control` exige `ALTER TABLE` à mão num banco existente;
  `schema_check.py` avisa no boot quando falta coluna.
- **`load_orbital_data` lê `satellite["name"]`.** As consultas de ativação
  (`fetch_pass_to_activate`, `fetch_active_pass`) precisam trazer `s.name`, não
  só o alias `satellite_name`: sem ela, todo satélite com TLE manual tinha a
  passagem cancelada com KeyError no AOS (os com NORAD ID passavam por outro
  caminho).
- **O plano ignora passagens rasantes.** `is_worth_tracking` corta abaixo de
  `SCHEDULER_MIN_PASS_ELEVATION_DEG` (5°) ou 60 s. Um cliente com máscara em 0°
  vai listar passagens que a estação decidiu não rastrear — é diferença de
  política, não de cálculo.
- **O `sent` do fim da janela é inferência, não confirmação.** Ver
  [docs/schema-contract.md](docs/schema-contract.md).
- **O schema não é nosso.** É do TC Generator, sem migrations ligando os dois.
  `schema_check.py` confere no boot e **loga**, nunca levanta. Ver o mesmo
  documento.
- **A tag da `spacelab-tracking` precisa bater com a do Station Manager.** O
  payload de `track_satellite` é `OrbitalData.to_json()`, e os dois lados
  desserializam com a versão que instalaram. É o acoplamento mais delicado que
  o split criou.
- **O card do satélite no painel atrasa até ~45 s.** `satellite_tracking_status`
  é escrito a cada 30 s e a página busca a cada 15 s, enquanto o rotor vem ao
  vivo. Durante uma passagem o satélite varre ~0,3°/s — o número do rotor é o
  atual.

## Rodar

```bash
pip install -e ".[dev]"
pytest

# Testes contra Postgres de verdade (criam e apagam um banco descartável):
SCHEDULER_TEST_DATABASE_URL=postgresql+psycopg2://admin:admin@localhost:5432/tc_generator \
TC_GENERATOR_SCHEMA_SQL=../grs-tc-generator/resources/database/schema.sql pytest

python -m tc_scheduler.main            # laço + API
python -m tc_scheduler.main --no-api   # só o laço
python -m tc_scheduler.api             # só a API
```

Observar e exercitar:

```bash
python -m tc_scheduler.monitor --watch
python tools/station_demo.py prepare --code SAT-001 --norad-id 44885
python tools/station_demo.py simulate-pass --code SAT-001
```
