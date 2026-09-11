# TC Scheduler (`tc_scheduler`)

**Aqui mora a autonomia da estação.** Este serviço decide o que rastrear e
quando, é o **único escritor do banco**, e serve o plano por HTTP para quem
quiser lê-lo.

## Portas

| Porta | Protocolo | Direção |
|---|---|---|
| 5591 | HTTP (Flask) | **expõe** — API de leitura; quem consome é o GRS Manager |
| 5580 | ZMQ REQ | **consome** — [Station Manager](https://github.com/nanosat-gs/grs-station-manager) |
| 5432 | Postgres | **consome** — o banco do TC Generator |

## Como a autonomia funciona

Pontua as passagens por **prioridade do telecomando à espera → quantidade de
comandos → elevação máxima**, e escolhe as que não se sobrepõem (a estação tem
um rotor só). Manda **uma ordem por passagem** (`track_satellite`) ao Station
Manager, não um setpoint por segundo: planejar é lento e apontar é rápido, e
separá-los faz com que um replanejamento nunca atrase o rotor.

## O único escritor do banco

Essa regra é a razão de a API de leitura existir. Antes, o painel do GRS
Manager abria a própria conexão com o Postgres para mostrar satélites e
passagens — dois serviços de repositórios diferentes escrevendo raw SQL contra
o mesmo schema. Agora quem escreve é quem serve.

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

## Armadilhas

- **Satélite sem telecomando à espera não entra no plano.**
  `fetch_trackable_satellites` usa `JOIN telecommands`, não `LEFT JOIN`: sem
  nada a transmitir não há o que agendar. Ele continua aparecendo no painel com
  posição, porque a posição corrente vem de outra consulta, sem esse filtro.
  Cadastrar um satélite e estranhar que não há passagem planejada costuma ser
  isto.
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
