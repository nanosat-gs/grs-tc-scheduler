"""Passagens de recepção contra Postgres DE VERDADE, com SGP4 de verdade.

Um banco descartável é criado por teste (CREATE DATABASE), carregado com o
`schema.sql` do TC Generator — o schema real, não uma cópia — e apagado no
fim. O replanejamento roda inteiro: SQL, propagação SGP4 a partir de uma TLE
fixa (sem CelesTrak), seleção e gravação. Só o Station Manager é dublê.

Pulado sem as duas variáveis. No compose:

    docker compose run --rm --no-deps \\
        -v "$PWD/repos/grs-tc-scheduler/tests:/app/tests" \\
        -v "$PWD/repos/grs-tc-generator/resources/database/schema.sql:/schema.sql" \\
        tc-scheduler sh -c 'pip install -q "pytest>=8,<9"; \\
        SCHEDULER_TEST_DATABASE_URL=$PG_DATABASE_URL TC_GENERATOR_SCHEMA_SQL=/schema.sql \\
        python -m pytest -q tests'
"""

from __future__ import annotations

import os
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DATABASE_URL = os.environ.get("SCHEDULER_TEST_DATABASE_URL")
SCHEMA_SQL = os.environ.get("TC_GENERATOR_SCHEMA_SQL")

pytestmark = pytest.mark.skipif(
    not (DATABASE_URL and SCHEMA_SQL and pathlib.Path(SCHEMA_SQL or "").exists()),
    reason="SCHEDULER_TEST_DATABASE_URL e TC_GENERATOR_SCHEMA_SQL não definidos",
)


# --- TLE sintética: época de hoje, sem arrasto, checksum calculado -------------

def _checksum(line: str) -> str:
    return str(sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line) % 10)


def iss_like_tle(norad: int) -> tuple[str, str]:
    """Órbita tipo ISS (51,6°, ~15,5 rev/dia), passando sobre São Paulo várias
    vezes por dia. Época de hoje e sem arrasto: nada decai entre a época e o
    teste, e nada depende da rede."""
    now = datetime.now(timezone.utc)
    day = now.timetuple().tm_yday + (now.hour + now.minute / 60) / 24
    line1 = f"1 {norad:05d}U 98067A   {now:%y}{day:012.8f}  .00000000  00000-0  00000-0 0  999"
    line2 = f"2 {norad:05d}  51.6416 247.4627 0006703 130.5360 325.0288 15.49815645 4320"
    return line1 + _checksum(line1), line2 + _checksum(line2)


# --- Banco descartável ---------------------------------------------------------

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
    # Cursor do driver direto, sem parâmetros: o schema.sql tem '%' literal,
    # que o psycopg2 tentaria interpretar como marcador se houvesse parâmetros.
    raw = test_engine.raw_connection()
    try:
        with raw.cursor() as cursor:
            cursor.execute(pathlib.Path(SCHEMA_SQL).read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()
    with test_engine.begin() as conn:
        # Os satélites de exemplo do schema têm NORAD ID: o replan iria ao
        # CelesTrak por eles. Fora do teste.
        conn.execute(text("UPDATE satellites SET status = 'inactive'"))
    db.ensure_control_schema(test_engine)

    yield test_engine

    test_engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
    admin.dispose()


def add_satellite(engine, code: str, norad: int) -> int:
    from sqlalchemy import text

    line1, line2 = iss_like_tle(norad)
    with engine.begin() as conn:
        return conn.execute(text("""
            INSERT INTO satellites (name, code, status, tle_line1, tle_line2)
            VALUES (:code, :code, 'active', :l1, :l2) RETURNING id
        """), {"code": code, "l1": line1, "l2": line2}).scalar_one()


def add_telecommand(engine, satellite_id: int, priority: int = 5) -> int:
    from sqlalchemy import text

    with engine.begin() as conn:
        return conn.execute(text("""
            INSERT INTO telecommands (satellite_id, command_type, priority)
            VALUES (:sid, 'PING', :priority) RETURNING id
        """), {"sid": satellite_id, "priority": priority}).scalar_one()


class FakeStation:
    def __init__(self):
        self.tracked = []

    def track_satellite(self, orbital_data, until_iso, satellite_name=None,
                        downlink_frequency_hz=None):
        self.tracked.append({"name": satellite_name, "until": until_iso,
                             "downlink_frequency_hz": downlink_frequency_hz})
        return {}

    def get_tracking(self):
        return None


def scheduler_for(engine, station=None):
    from tc_scheduler.main import Scheduler

    return Scheduler(engine, station or FakeStation())


def planned(engine) -> list[dict]:
    from sqlalchemy import text

    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text("""
            SELECT p.id, s.code, p.aos_time, p.los_time,
                   (SELECT COUNT(*) FROM telecommands t WHERE t.scheduled_pass_id = p.id) AS tcs
            FROM scheduled_passes p JOIN satellites s ON s.id = p.satellite_id
            WHERE p.status = 'planned' ORDER BY p.aos_time
        """)).mappings()]


def control(engine):
    from tc_scheduler.operator_control import OperatorControl

    return OperatorControl(engine)


def as_datetime(value) -> datetime:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def has_pass_near(passes: list[dict], aos, code: str | None = None) -> bool:
    """Existe passagem com AOS a menos de 1 min deste? Comparar o AOS exato
    seria frágil: ele é recalculado a cada replanejamento, e um "não está mais
    no plano" passaria sempre por diferença de milissegundos."""
    aos = as_datetime(aos)
    return any(
        abs(as_datetime(p["aos_time"]) - aos) < timedelta(minutes=1)
        and (code is None or p.get("code", p.get("satellite_code")) == code)
        for p in passes
    )


# --- Passagens sem telecomando ---------------------------------------------------

def test_satelite_sem_telecomando_tem_passagens_planejadas(engine):
    """O defeito que motivou a mudança: sem comando na fila não havia passagem,
    e sem passagem a antena não apontava para receber telemetria."""
    add_satellite(engine, "RX-1", 90001)

    scheduler_for(engine).replan()

    plan = planned(engine)
    assert len(plan) >= 2, "uma órbita tipo ISS passa várias vezes por dia"
    assert all(p["tcs"] == 0 for p in plan)


def test_telecomando_vai_so_na_primeira_passagem(engine):
    sat = add_satellite(engine, "RX-1", 90001)
    add_telecommand(engine, sat)
    add_telecommand(engine, sat)

    scheduler_for(engine).replan()

    plan = planned(engine)
    assert plan[0]["tcs"] == 2
    assert all(p["tcs"] == 0 for p in plan[1:])
    assert len(plan) >= 2, "as outras passagens seguem planejadas, para recepção"


def test_recepcao_desligada_volta_ao_comportamento_antigo(engine):
    sat = add_satellite(engine, "RX-1", 90001)
    control(engine).set_reception("RX-1", {"track_passes": False})

    scheduler_for(engine).replan()
    assert planned(engine) == []

    add_telecommand(engine, sat)
    scheduler_for(engine).replan()
    plan = planned(engine)
    assert len(plan) == 1 and plan[0]["tcs"] == 1


def test_previsao_mostra_as_passagens_de_quem_desligou(engine):
    add_satellite(engine, "RX-1", 90001)
    ctl = control(engine)
    ctl.set_reception("RX-1", {"track_passes": False})

    scheduler_for(engine).replan()

    outcomes = {p["outcome"] for p in ctl.predictions()["passes"]}
    assert outcomes == {"reception_off"}


# --- Decisões do operador ---------------------------------------------------------

def test_pular_tira_a_passagem_do_plano(engine):
    add_satellite(engine, "RX-1", 90001)
    scheduler = scheduler_for(engine)
    scheduler.replan()
    first = planned(engine)[0]

    control(engine).set_decision("RX-1", first["aos_time"].isoformat(), "skip")
    scheduler.replan()

    assert not has_pass_near(planned(engine), first["aos_time"])
    assert len(planned(engine)) >= 1, "as outras passagens continuam"
    skipped = [p for p in control(engine).predictions()["passes"] if p["outcome"] == "skipped"]
    assert len(skipped) == 1 and skipped[0]["decision"] == "skip"


def test_decisao_casa_com_o_aos_mesmo_deslocado(engine):
    """O plano é recriado a cada ciclo e uma TLE nova desloca o AOS em
    segundos: a decisão tem de continuar valendo."""
    add_satellite(engine, "RX-1", 90001)
    scheduler = scheduler_for(engine)
    scheduler.replan()
    first = planned(engine)[0]

    shifted = first["aos_time"] + timedelta(minutes=4)
    control(engine).set_decision("RX-1", shifted.isoformat(), "skip")
    scheduler.replan()

    assert not has_pass_near(planned(engine), first["aos_time"])
    assert len(planned(engine)) >= 1


def test_desfazer_devolve_a_passagem(engine):
    add_satellite(engine, "RX-1", 90001)
    scheduler = scheduler_for(engine)
    scheduler.replan()
    first = planned(engine)[0]
    ctl = control(engine)

    ctl.set_decision("RX-1", first["aos_time"].isoformat(), "skip")
    scheduler.replan()
    assert not has_pass_near(planned(engine), first["aos_time"])

    ctl.set_decision("RX-1", first["aos_time"].isoformat(), None)
    scheduler.replan()

    assert has_pass_near(planned(engine), first["aos_time"])


def test_forcar_vence_o_conflito(engine):
    """Dois satélites na mesma órbita, 1 minuto defasados: toda passagem de um
    colide com a do outro. Forçar a do perdedor inverte o resultado."""
    add_satellite(engine, "A", 90001)
    add_satellite(engine, "B", 90002)
    # Mesmo TLE, NORAD diferente: passagens praticamente simultâneas.
    scheduler = scheduler_for(engine)
    scheduler.replan()
    ctl = control(engine)

    losers = [p for p in ctl.predictions()["passes"] if p["outcome"] == "conflict"]
    assert losers, "passagens simultâneas precisam colidir"
    loser = losers[0]

    ctl.set_decision(loser["satellite_code"], loser["aos_time"], "force")
    scheduler.replan()

    forced = [
        p for p in ctl.predictions()["passes"]
        if p["satellite_code"] == loser["satellite_code"] and has_pass_near([p], loser["aos_time"])
    ]
    assert len(forced) == 1
    assert forced[0]["outcome"] == "planned" and forced[0]["forced"] is True
    assert has_pass_near(planned(engine), loser["aos_time"], code=loser["satellite_code"])


def test_acao_do_operador_dispara_replanejamento(engine):
    add_satellite(engine, "RX-1", 90001)
    scheduler = scheduler_for(engine)
    scheduler.replan()
    scheduler.replan_if_operator_changed()   # primeira leitura: só memoriza
    first = planned(engine)[0]

    control(engine).set_decision("RX-1", first["aos_time"].isoformat(), "skip")
    scheduler.replan_if_operator_changed()

    assert not has_pass_near(planned(engine), first["aos_time"])


# --- Recepção e frequência --------------------------------------------------------

def test_frequencia_vai_no_track_satellite(engine):
    from sqlalchemy import text

    add_satellite(engine, "RX-1", 90001)
    control(engine).set_reception("RX-1", {"downlink_frequency_hz": 145_900_000})
    scheduler = scheduler_for(engine)
    scheduler.replan()

    # Traz a primeira passagem para "agora", para o drive_passes assumi-la.
    first = planned(engine)[0]
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE scheduled_passes SET aos_time = :aos, los_time = :los WHERE id = :id
        """), {"aos": now - timedelta(minutes=1), "los": now + timedelta(minutes=9),
               "id": first["id"]})

    scheduler.drive_passes()

    assert scheduler._station.tracked[0]["downlink_frequency_hz"] == 145_900_000


@pytest.mark.parametrize("changes, message", [
    ({"track_passes": "sim"}, "true ou false"),
    ({"downlink_frequency_hz": 145.9}, "Hz, não em MHz"),
    ({"downlink_frequency_hz": "145900000"}, "número"),
    ({"canal": 1}, "desconhecido"),
    ({}, "envie"),
])
def test_recepcao_invalida_e_recusada(engine, changes, message):
    from tc_scheduler.operator_control import ControlError

    add_satellite(engine, "RX-1", 90001)
    with pytest.raises(ControlError, match=message):
        control(engine).set_reception("RX-1", changes)


def test_recepcao_parcial_preserva_o_outro_campo(engine):
    add_satellite(engine, "RX-1", 90001)
    ctl = control(engine)

    ctl.set_reception("RX-1", {"downlink_frequency_hz": 145_900_000})
    result = ctl.set_reception("RX-1", {"track_passes": False})

    assert result["downlink_frequency_hz"] == 145_900_000
    assert result["track_passes"] is False


def test_satelite_inexistente(engine):
    from tc_scheduler.operator_control import NotFound

    with pytest.raises(NotFound):
        control(engine).set_decision("NAO-EXISTE", "2026-09-29T14:03:00Z", "skip")


def test_schema_check_confere_as_tabelas_de_controle(engine):
    from tc_scheduler import schema_check

    assert schema_check.check(engine) is True
