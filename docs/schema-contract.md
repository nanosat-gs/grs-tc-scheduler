# Contrato de schema com o TC Generator

O TC Scheduler é o único serviço que **escreve** no banco da estação. Mas o
banco não é dele: o schema é criado pelo
[TC Generator](https://github.com/edsoncepedi/grs-tc-generator), em
`resources/database/schema.sql`, montado pelo compose do orquestrador em
`/docker-entrypoint-initdb.d/`.

**Estas colunas são propriedade do TC Generator. Mudá-las lá quebra este
serviço.** Não há migrations, não há modelo compartilhado, não há nada que
declare essa dependência além deste documento e do
[`schema_check.py`](../src/tc_scheduler/schema_check.py) que o verifica no boot.

Isso foi uma escolha, não um esquecimento. As alternativas eram piores:

- **Copiar a DDL para cá ou para o orquestrador** criaria duas fontes de
  verdade. E como `docker-entrypoint-initdb.d` **só roda em volume vazio**, a
  divergência ficaria silenciosa por meses, aparecendo só num `down -v` — quando
  o banco já teria sido recriado com a versão errada.
- **Um repositório de migrations** seria um terceiro dono para cinco tabelas que
  uma aplicação já possui.

A propriedade está no lugar certo: quem cria satélites e telecomandos é o TC
Generator. O que faltava era o contrato ser **visível** e **verificado**.

## O que este serviço usa

### `satellites` — só leitura

| Coluna | Uso |
|---|---|
| `id` | Chave para as demais tabelas |
| `name` | Nome exibido e passado ao Station Manager |
| `code` | Identificador que o operador digita (`/api/satellite/<code>`) |
| `status` | Só `active` entra no plano |
| `norad_id` | Busca no CelesTrak. **NULL é válido** — o satélite só não é rastreável |
| `tle_line1`, `tle_line2` | TLE manual, que tem precedência sobre o catálogo |

### `telecommands` — leitura e escrita

| Coluna | Uso |
|---|---|
| `id` | Chave |
| `satellite_id` | A quem o comando se destina |
| `scheduled_pass_id` | **Escrito**: a passagem em que o comando sai, ou NULL |
| `command_type` | Exibido no painel |
| `status` | **Escrito**: `pending` → `queued` → `sent` |
| `priority` | Primeiro critério de pontuação da passagem |

### `scheduled_passes` — leitura e escrita (o Scheduler é quem cria)

| Coluna | Uso |
|---|---|
| `id`, `satellite_id` | Chaves |
| `aos_time`, `los_time`, `culmination_time` | A janela |
| `max_elevation_deg` | Terceiro critério de pontuação |
| `status` | `planned` → `active` → `completed` / `missed` |
| `status_message` | Por que a passagem terminou assim |

### `satellite_tracking_status` — escrita (upsert a cada 30 s)

| Coluna | Uso |
|---|---|
| `satellite_id` | Chave |
| `azimuth_deg`, `elevation_deg`, `range_km`, `is_visible` | Posição corrente |
| `status_message`, `checked_at` | Estado e quando foi calculado |

O painel também lê `latitude_deg`, `longitude_deg` e `altitude_km` desta tabela
em versões futuras; hoje ele recalcula o ponto subsatélite em vez de lê-lo,
porque propagar custa microssegundos e alargar a tabela para uma única tela não
se paga.

### `execution_logs` — só escrita

| Coluna | Uso |
|---|---|
| `telecommand_id` | A qual comando o registro se refere |
| `status` | O estado que motivou o registro |
| `message` | **Onde mora a honestidade do sistema** — ver abaixo |

## Uma nota sobre o `sent`

O fim de uma janela `completed` marca os telecomandos daquela passagem como
`sent`. Isso afirma mais do que a estação observa: ela sabe que **rastreou** a
passagem, não que o rádio transmitiu — não há encoder nem modulador.

Foi decisão do operador, ciente do limite, porque sem isso um comando ficava em
`queued` para sempre e a fila de pendentes nunca esvaziava. Cada marcação grava
um `execution_logs` dizendo que veio do fim da janela e não de confirmação
(`db.SENT_BY_PASS_COMPLETION`). Quando os fluxos de dados existirem, um `sent`
com evidência será distinguível destes pela mensagem.

Passagem `missed` **devolve** os comandos à fila: ali a antena nem chegou a
apontar.
