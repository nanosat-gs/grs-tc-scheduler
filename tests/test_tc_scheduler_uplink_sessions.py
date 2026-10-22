"""Sessões de uplink contra Postgres DE VERDADE: a liberação sem Doppler.

Mesmo banco descartável de test_tc_scheduler_reception.py (o `schema.sql`
real do TC Generator), pulado sem as duas variáveis.
"""

from __future__ import annotations

import os
import pathlib
import uuid
from datetime import datetime, timezone

import pytest

DATABASE_URL = os.environ.get("SCHEDULER_TEST_DATABASE_URL")
SCHEMA_SQL = os.environ.get("TC_GENERATOR_SCHEMA_SQL")

pytestmark = pytest.mark.skipif(
    not (DATABASE_URL and SCHEMA_SQL and pathlib.Path(SCHEMA_SQL or "").exists()),
    reason="SCHEDULER_TEST_DATABASE_URL e TC_GENERATOR_SCHEMA_SQL não definidos",
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine():
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from tc_scheduler import db

    admin = create_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    name = f"sched_test_{uuid.uuid4().hex[:8]}"
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))

    test_engine = create_engine(make_url(DATABASE_URL).set(database=name))
    raw = test_engine.raw_connection()
    try:
        with raw.cursor() as cursor:
            cursor.execute(pathlib.Path(SCHEMA_SQL).read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()
    db.ensure_control_schema(test_engine)

    yield test_engine

    test_engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
    admin.dispose()


def satellite_codes(engine, n: int) -> list[str]:
    from sqlalchemy import text

    with engine.connect() as conn:
        return list(conn.execute(text("SELECT code FROM satellites ORDER BY id LIMIT :n"),
                                 {"n": n}).scalars())


def sessions_by_code(engine) -> dict[str, bool]:
    from sqlalchemy import text

    from tc_scheduler.lifecycle_store import PostgresLifecycleStore

    open_sessions = PostgresLifecycleStore(engine).open_sessions(NOW)
    with engine.connect() as conn:
        codes = dict(conn.execute(text("SELECT id, code FROM satellites")).all())
    return {codes[satellite_id]: allow for satellite_id, allow in open_sessions.items()}


def test_sessao_sem_liberacao_exige_doppler(engine):
    from tc_scheduler.uplink_control import UplinkControl

    [code] = satellite_codes(engine, 1)
    session = UplinkControl(engine, 3600).open_session({"satellite": code, "duration_s": 600}, NOW)

    assert session["session"]["allow_no_doppler"] is False
    assert sessions_by_code(engine) == {code: False}


def test_liberacao_fica_na_sessao_e_na_visao_geral(engine):
    from sqlalchemy import text

    from tc_scheduler.uplink_control import UplinkControl

    # A visão geral também lista as transmissões, cuja tabela é do grs-uplink
    # (criada no boot dele). Aqui, só as colunas que ela lê.
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE mission_control.uplink_transmissions (
                id BIGSERIAL PRIMARY KEY, telecommand_id INTEGER, attempt INTEGER,
                command_type TEXT, status TEXT, tx_freq_hz DOUBLE PRECISION,
                doppler_hz DOUBLE PRECISION, ended_at TIMESTAMPTZ, detail TEXT)
        """))
    first, second = satellite_codes(engine, 2)
    control = UplinkControl(engine, 3600)
    control.open_session({"satellite": first, "duration_s": 600}, NOW)
    control.open_session({"satellite": second, "duration_s": 600, "allow_no_doppler": True}, NOW)

    assert sessions_by_code(engine) == {first: False, second: True}
    overview = {s["satellite"]: s["allow_no_doppler"] for s in control.overview(NOW)["sessions"]}
    assert overview == {first: False, second: True}


def test_duas_sessoes_do_mesmo_satelite_basta_uma_liberar(engine):
    from tc_scheduler.uplink_control import UplinkControl

    [code] = satellite_codes(engine, 1)
    control = UplinkControl(engine, 3600)
    control.open_session({"satellite": code, "duration_s": 600}, NOW)
    control.open_session({"satellite": code, "duration_s": 600, "allow_no_doppler": True}, NOW)

    assert sessions_by_code(engine) == {code: True}


def test_liberacao_precisa_ser_booleana(engine):
    from tc_scheduler.operator_control import ControlError
    from tc_scheduler.uplink_control import UplinkControl

    [code] = satellite_codes(engine, 1)
    with pytest.raises(ControlError, match="allow_no_doppler"):
        UplinkControl(engine, 3600).open_session({"satellite": code, "allow_no_doppler": "sim"}, NOW)


def test_banco_antigo_ganha_a_coluna_no_boot(engine):
    """CREATE TABLE IF NOT EXISTS não acrescenta coluna: um banco criado antes
    da liberação precisa do ALTER, e ele tem de ser idempotente."""
    from sqlalchemy import text

    from tc_scheduler import db

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE mission_control.uplink_sessions DROP COLUMN allow_no_doppler"))
    db.ensure_control_schema(engine)
    db.ensure_control_schema(engine)

    with engine.connect() as conn:
        column = conn.execute(text("""
            SELECT column_default, is_nullable FROM information_schema.columns
            WHERE table_schema = 'mission_control' AND table_name = 'uplink_sessions'
              AND column_name = 'allow_no_doppler'
        """)).one()
    assert column.column_default == "false" and column.is_nullable == "NO"
