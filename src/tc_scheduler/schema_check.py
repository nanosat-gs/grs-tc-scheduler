"""Confere, no boot, se o banco tem as colunas que este serviço usa.

O schema não é nosso: ele é criado pelo TC Generator
(`resources/database/schema.sql` naquele repositório), montado pelo compose em
`/docker-entrypoint-initdb.d/`. Depois do split em repositórios, essa é uma
dependência entre dois repos sem nada que a declare — nem migrations, nem
modelo compartilhado, só SQL cru dos dois lados.

O que este módulo resolve não é a divergência em si, é o **sinal**. Sem ele,
uma coluna renomeada no TC Generator aparece como um ProgrammingError no meio
de um ciclo de replanejamento, capturado pelo `except Exception` do laço e
logado como "Falha no ciclo do scheduler" — a mensagem mais genérica possível,
num processo que continua rodando como se nada fosse. Com ele, aparece como
uma linha explícita no boot, dizendo qual coluna sumiu.

Loga e segue, nunca levanta: um schema divergente pode muito bem ser
compatível com o que de fato usamos, e derrubar o Scheduler por causa de uma
coluna que talvez nem seja lida trocaria um problema pequeno por um grande.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# O contrato, extraído das consultas de `db.py` e `station_data.py`.
# Ver docs/schema-contract.md para o texto que explica cada uma.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "satellites": (
        "id", "name", "code", "status", "norad_id", "tle_line1", "tle_line2",
    ),
    "telecommands": (
        "id", "satellite_id", "scheduled_pass_id", "command_type", "status", "priority",
    ),
    "scheduled_passes": (
        "id", "satellite_id", "aos_time", "los_time", "culmination_time",
        "max_elevation_deg", "status", "status_message",
    ),
    "satellite_tracking_status": (
        "satellite_id", "azimuth_deg", "elevation_deg", "range_km",
        "is_visible", "status_message", "checked_at",
    ),
    "execution_logs": (
        "telecommand_id", "status", "message",
    ),
}

_COLUMNS_SQL = """
    SELECT table_name, column_name
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = ANY(:tables)
"""


def check(engine) -> bool:
    """Compara o banco com REQUIRED_COLUMNS. True se está tudo lá.

    Uma falha ao consultar o próprio information_schema não é tratada como
    divergência: nesse caso não sabemos nada, e afirmar que falta coluna seria
    pior do que não dizer nada.
    """
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(_COLUMNS_SQL), {"tables": list(REQUIRED_COLUMNS)}
            ).fetchall()
    except Exception as error:
        logger.warning("Não deu para conferir o schema do banco: %s", error)
        return False

    found: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        found.setdefault(table_name, set()).add(column_name)

    ok = True
    for table, columns in REQUIRED_COLUMNS.items():
        if table not in found:
            logger.warning(
                "Tabela '%s' não existe no banco. Ela é criada pelo TC Generator; "
                "o volume do Postgres pode ter sido inicializado sem o schema dele.",
                table,
            )
            ok = False
            continue
        missing = sorted(set(columns) - found[table])
        if missing:
            logger.warning(
                "Tabela '%s' sem as colunas %s. O schema é propriedade do TC Generator "
                "(nanosat-gs/grs-tc-generator); ver docs/schema-contract.md.",
                table, ", ".join(missing),
            )
            ok = False

    ok = _check_control_tables(engine) and ok

    if ok:
        logger.info("Schema do banco confere com o que o Scheduler usa.")
    return ok


def _check_control_tables(engine) -> bool:
    """As tabelas do próprio Scheduler (`mission_control`).

    Criadas por `db.ensure_control_schema`, então a divergência aqui só vem de
    uma versão mais nova do Scheduler subindo contra tabelas criadas por uma
    antiga: `CREATE TABLE IF NOT EXISTS` não adiciona coluna.
    """
    from sqlalchemy import text

    from tc_scheduler.db import CONTROL_COLUMNS, CONTROL_SCHEMA

    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT table_name, column_name FROM information_schema.columns
                WHERE table_schema = :schema AND table_name = ANY(:tables)
            """), {"schema": CONTROL_SCHEMA, "tables": list(CONTROL_COLUMNS)}).fetchall()
    except Exception as error:
        logger.warning("Não deu para conferir as tabelas de controle: %s", error)
        return False

    found: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        found.setdefault(table_name, set()).add(column_name)

    ok = True
    for table, columns in CONTROL_COLUMNS.items():
        missing = sorted(set(columns) - found.get(table, set()))
        if missing:
            logger.warning(
                "%s.%s sem as colunas %s. Tabela criada por uma versão anterior do "
                "Scheduler: CREATE TABLE IF NOT EXISTS não adiciona coluna — é "
                "preciso ALTER TABLE à mão.", CONTROL_SCHEMA, table, ", ".join(missing),
            )
            ok = False
    return ok


# O que o ciclo de vida do TC (tc_lifecycle.py) lê de OUTROS serviços, em
# `mission_control`. Só conferido com UPLINK_ENABLED. Divergência aqui é
# aviso, não erro: o grs-uplink e o decoder podem subir depois do Scheduler,
# e as tabelas deles aparecem quando eles sobem.
UPLINK_READ_COLUMNS: dict[str, tuple[str, ...]] = {
    # grs-uplink, docs/schema-contract.md
    "uplink_transmissions": (
        "id", "telecommand_id", "status", "ended_at", "tc_pkt_id",
        "expected_response_pkt_id", "station_callsign",
    ),
    # grs-telemetry-decoder, docs/schema-contract.md
    "decoded_frames": (
        "id", "raw_packet_id", "received_at", "radio", "status", "ngham_payload",
        "pkt_id", "detail",
    ),
    "fs2_tc_feedback": ("decoded_frame_id", "error_code"),
}


def check_uplink(engine) -> bool:
    from sqlalchemy import text

    from tc_scheduler.db import CONTROL_SCHEMA

    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT table_name, column_name FROM information_schema.columns
                WHERE table_schema = :schema AND table_name = ANY(:tables)
            """), {"schema": CONTROL_SCHEMA, "tables": list(UPLINK_READ_COLUMNS)}).fetchall()
    except Exception as error:
        logger.warning("Não deu para conferir as tabelas do uplink: %s", error)
        return False

    found: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        found.setdefault(table_name, set()).add(column_name)

    ok = True
    for table, columns in UPLINK_READ_COLUMNS.items():
        if table not in found:
            logger.warning("Uplink: %s.%s ainda não existe (o serviço dono ainda não subiu?). "
                           "O ciclo de vida do TC falha até ela aparecer.", CONTROL_SCHEMA, table)
            ok = False
            continue
        missing = sorted(set(columns) - found[table])
        if missing:
            logger.warning("Uplink: %s.%s sem as colunas %s.", CONTROL_SCHEMA, table, ", ".join(missing))
            ok = False
    if ok:
        logger.info("Tabelas do uplink (grs-uplink, decoder) conferem.")
    return ok
