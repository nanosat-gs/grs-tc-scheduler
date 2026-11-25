"""Passagem simulada: a próxima passagem real, começando agora (simulated_pass.py).

O que mais paga é o primeiro teste. A passagem simulada só vale se o TLE
deslocado for, visto da estação, a mesma passagem: a estação e o rádio da
bancada vão calcular o Doppler a partir dele, cada um com a sua geometria, e um
erro aqui pareceria erro de um dos dois.

Os testes de banco (replanejamento, ativação, ajuste fino) rodam contra
Postgres de verdade, como os de recepção, e são pulados sem as duas variáveis.
"""

from __future__ import annotations

import os
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sgp4.api import Satrec
from spacelab_tracking import from_tle_lines, get_tracking_info, predict_passes

from tc_scheduler.api import create_app
from tc_scheduler.simulated_pass import NoSuitablePass, next_pass_now

# Um TLE real da ISS, com a época perto do "agora" dos testes: nada depende da rede.
ISS_L1 = "1 25544U 98067A   19343.69339541  .00001764  00000-0  38792-4 0  9991"
ISS_L2 = "2 25544  51.6439 211.2001 0007417  17.6667  85.6398 15.50103472202482"
NOW = datetime(2019, 12, 10, 3, 0, tzinfo=timezone.utc)
STATION = {"name": "teste", "latitude_deg": -27.6, "longitude_deg": -48.5, "altitude_m": 20.0}
VHF_HZ = 145.9e6


def iss():
    return from_tle_lines(ISS_L1, ISS_L2, "ISS")


def test_tle_deslocado_e_a_mesma_passagem_vista_da_estacao():
    simulated = next_pass_now(iss(), "ISS", STATION, NOW, min_peak_deg=30)
    original = Satrec.twoline2rv(ISS_L1, ISS_L2)
    shifted = Satrec.twoline2rv(simulated.tle_line1, simulated.tle_line2)
    shift = timedelta(seconds=simulated.shift_s)
    p = simulated.satellite_pass

    t = p.aos_time
    while t <= p.los_time:
        a = get_tracking_info(original, "ISS", when=t + shift, station=STATION)
        b = get_tracking_info(shifted, "ISS", when=t, station=STATION)
        assert abs(a.topocentric.azimuth_deg - b.topocentric.azimuth_deg) < 0.01
        assert abs(a.topocentric.elevation_deg - b.topocentric.elevation_deg) < 0.01
        assert abs(a.doppler_shift_hz(VHF_HZ) - b.doppler_shift_hz(VHF_HZ)) < 1.0
        t += timedelta(seconds=30)


def test_a_passagem_comeca_agora_e_tem_a_forma_de_uma_passagem_real():
    simulated = next_pass_now(iss(), "ISS", STATION, NOW, min_peak_deg=30, lead_s=15)
    p = simulated.satellite_pass
    assert abs((p.aos_time - (NOW + timedelta(seconds=15))).total_seconds()) < 1.0
    assert p.max_elevation_deg >= 30
    assert simulated.original_aos > NOW
    assert simulated.shift_s == pytest.approx((simulated.original_aos - p.aos_time).total_seconds(),
                                              abs=1.0)
    # A curva de Doppler de uma passagem: positivo no AOS, negativo no LOS.
    shifted = Satrec.twoline2rv(simulated.tle_line1, simulated.tle_line2)
    aos = get_tracking_info(shifted, "ISS", when=p.aos_time, station=STATION)
    los = get_tracking_info(shifted, "ISS", when=p.los_time, station=STATION)
    assert aos.doppler_shift_hz(VHF_HZ) > 2000
    assert los.doppler_shift_hz(VHF_HZ) < -2000


def test_elevacao_minima_escolhe_uma_passagem_mais_alta():
    baixa = next_pass_now(iss(), "ISS", STATION, NOW, min_peak_deg=0)
    alta = next_pass_now(iss(), "ISS", STATION, NOW, min_peak_deg=60)
    assert alta.satellite_pass.max_elevation_deg >= 60
    assert alta.original_aos >= baixa.original_aos


def test_o_tle_deslocado_e_um_tle_valido():
    simulated = next_pass_now(iss(), "ISS", STATION, NOW)
    data = from_tle_lines(simulated.tle_line1, simulated.tle_line2, "ISS")  # confere checksum
    assert data.tle_line1[2:7] == "25544"
    found = predict_passes(Satrec.twoline2rv(data.tle_line1, data.tle_line2), "ISS",
                           station=STATION, start=NOW, search_window_hours=0.5)
    assert found


def test_sem_passagem_com_a_elevacao_pedida_explica():
    with pytest.raises(NoSuitablePass, match="89"):
        next_pass_now(iss(), "ISS", STATION, NOW, min_peak_deg=89.9, search_hours=6)


def test_ativacao_usa_a_orbita_da_janela_simulada():
    from tc_scheduler.main import load_orbital_data

    simulated = next_pass_now(iss(), "ISS", STATION, NOW)
    data = load_orbital_data({"name": "FS-2", "norad_id": 25544,
                              "tle_line1": ISS_L1, "tle_line2": ISS_L2,
                              "simulated_tle_line1": simulated.tle_line1,
                              "simulated_tle_line2": simulated.tle_line2})
    assert (data.tle_line1, data.tle_line2) == (simulated.tle_line1, simulated.tle_line2)
    assert data.satellite_name == "FS-2"


# --- API --------------------------------------------------------------------

class FakeStationData:
    def __init__(self, simulated=None, fail=False):
        self._simulated = simulated
        self._fail = fail

    def simulated_pass(self):
        if self._fail:
            raise RuntimeError("banco fora")
        return self._simulated


def client(station_data):
    app = create_app(station_data)
    app.config.update(TESTING=True)
    return app.test_client()


def test_api_entrega_o_tle_da_passagem_simulada():
    simulated = {"pass_id": 7, "satellite_code": "FS-2", "tle": [ISS_L1, ISS_L2]}
    response = client(FakeStationData(simulated)).get("/api/simulated-pass")
    assert response.status_code == 200
    assert response.get_json() == {"simulated_pass": simulated}


def test_api_sem_passagem_simulada_devolve_null():
    response = client(FakeStationData(None)).get("/api/simulated-pass")
    assert response.status_code == 200
    assert response.get_json() == {"simulated_pass": None}


def test_api_com_banco_fora_responde_503():
    response = client(FakeStationData(fail=True)).get("/api/simulated-pass")
    assert response.status_code == 503


# --- Banco ------------------------------------------------------------------

DATABASE_URL = os.environ.get("SCHEDULER_TEST_DATABASE_URL")
SCHEMA_SQL = os.environ.get("TC_GENERATOR_SCHEMA_SQL")
needs_db = pytest.mark.skipif(
    not (DATABASE_URL and SCHEMA_SQL and pathlib.Path(SCHEMA_SQL or "").exists()),
    reason="SCHEDULER_TEST_DATABASE_URL e TC_GENERATOR_SCHEMA_SQL não definidos",
)


def _checksum(line: str) -> str:
    return str(sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line) % 10)


def iss_like_today():
    """Órbita tipo ISS com época de agora: a passagem simulada é de hoje."""
    now = datetime.now(timezone.utc)
    day = now.timetuple().tm_yday + (now.hour + now.minute / 60) / 24
    line1 = f"1 25544U 98067A   {now:%y}{day:012.8f}  .00000000  00000-0  00000-0 0  999"
    line2 = "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.49815645 4320"
    return from_tle_lines(line1 + _checksum(line1), line2 + _checksum(line2), "ISS")


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
    with test_engine.begin() as conn:
        conn.execute(text("UPDATE satellites SET status = 'inactive'"))
    db.ensure_control_schema(test_engine)
    yield test_engine
    test_engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
    admin.dispose()


class FakeStation:
    def __init__(self):
        self.tracked = []
        self.tracking = None

    def track_satellite(self, orbital_data, until_iso, satellite_name=None,
                        downlink_frequency_hz=None, downlinks=None):
        self.tracked.append({"orbital_data": orbital_data, "name": satellite_name,
                             "downlinks": downlinks})
        return {}

    def get_tracking(self):
        return self.tracking


def simulated_window(engine, with_telecommand=True):
    """O FS-2 da bancada: sem órbita, com dois downlinks e um TC; a janela emprestada da ISS."""
    from sqlalchemy import text

    from tc_scheduler import config, db

    with engine.begin() as conn:
        # O schema do TC Generator já traz o FS-2; na bancada ele não tem órbita.
        sat = conn.execute(text("""
            UPDATE satellites SET status = 'active', norad_id = NULL,
                                  tle_line1 = NULL, tle_line2 = NULL
            WHERE code = 'FS-2' RETURNING id
        """)).scalar_one()
        conn.execute(text(f"""
            INSERT INTO {db.CONTROL_SCHEMA}.satellite_downlinks
                (satellite_id, name, frequency_hz, position)
            VALUES (:s, 'beacon', 145900000, 0), (:s, 'dados', 468400000, 1)
        """), {"s": sat})
    simulated = next_pass_now(iss_like_today(), "ISS", config.GROUND_STATION,
                              datetime.now(timezone.utc), min_peak_deg=10)
    with engine.begin() as conn:
        pass_id = db.insert_simulated_pass(conn, sat, simulated, "tle", "teste")
        tc = None
        if with_telecommand:
            tc = conn.execute(text("""
                INSERT INTO telecommands (satellite_id, command_type, priority, status,
                                          scheduled_pass_id)
                VALUES (:s, 'PING', 5, 'queued', :p) RETURNING id
            """), {"s": sat, "p": pass_id}).scalar_one()
    return sat, pass_id, tc, simulated


@needs_db
def test_replanejamento_nao_apaga_a_janela_simulada_nem_solta_os_tcs(engine):
    from sqlalchemy import text

    from tc_scheduler import db

    _, pass_id, tc, _ = simulated_window(engine)
    with engine.begin() as conn:
        db.clear_planned_passes(conn)
        status = conn.execute(text("SELECT status FROM scheduled_passes WHERE id = :p"),
                              {"p": pass_id}).scalar_one()
        tc_row = conn.execute(text("""
            SELECT status, scheduled_pass_id FROM telecommands WHERE id = :t
        """), {"t": tc}).one()
    assert status == "planned"
    assert tuple(tc_row) == ("queued", pass_id)


@needs_db
def test_no_aos_a_estacao_recebe_o_tle_simulado_e_os_downlinks(engine, monkeypatch):
    from tc_scheduler import main

    _, pass_id, _, simulated = simulated_window(engine)
    after_aos = simulated.satellite_pass.aos_time + timedelta(seconds=1)
    monkeypatch.setattr(main, "now_utc", lambda: after_aos)
    station = FakeStation()
    main.Scheduler(engine, station).drive_passes()

    assert len(station.tracked) == 1
    order = station.tracked[0]
    assert order["orbital_data"]["tle_line1"] == simulated.tle_line1
    assert order["orbital_data"]["tle_line2"] == simulated.tle_line2
    assert [d["name"] for d in order["downlinks"]] == ["beacon", "dados"]


@needs_db
def test_janela_simulada_nao_grava_o_erro_do_oscilador(engine, monkeypatch):
    from sqlalchemy import text

    from tc_scheduler import db, main

    sat, _, _, simulated = simulated_window(engine, with_telecommand=False)
    monkeypatch.setattr(main, "now_utc",
                        lambda: simulated.satellite_pass.aos_time + timedelta(seconds=1))
    station = FakeStation()
    scheduler = main.Scheduler(engine, station)
    scheduler.drive_passes()          # assume a janela
    # A malha "convergiu" num desvio que é o Doppler que o FS-2 parado não tem.
    station.tracking = {"downlinks": [{"name": "beacon", "offset_hz": -2500.0,
                                       "afc_updates": 10, "afc_last_residual_hz": 3.0}]}
    scheduler.drive_passes()          # confere o rastreio, como a cada tick
    with engine.begin() as conn:
        assert db.fetch_active_pass(conn)["simulated"] is True
        measured = conn.execute(text(f"""
            SELECT measured_offset_hz FROM {db.CONTROL_SCHEMA}.satellite_downlinks
            WHERE satellite_id = :s AND name = 'beacon'
        """), {"s": sat}).scalar_one()
    assert measured is None
