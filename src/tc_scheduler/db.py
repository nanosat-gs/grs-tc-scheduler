"""Acesso ao banco do TC Generator, em SQL puro.

Não importa os modelos ORM do TC Generator de propósito: eles vivem noutro
repositório, com o seu próprio ciclo de vida, e arrastá-los para cá acoplaria
duas imagens Docker por código Python além de trazer o Flask para um processo
que não é web. O contrato entre os dois é o schema do banco, e é só isso que
este módulo assume.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CandidateSatellite:
    """Satélite ativo com órbita conhecida, e o que se quer dele.

    `track_passes` é a escolha do operador: rastrear toda passagem para
    receber telemetria (padrão), ou só as que levam telecomando.
    """

    id: int
    name: str
    code: str
    norad_id: Optional[int]
    tle_line1: Optional[str]
    tle_line2: Optional[str]
    pending_telecommands: int
    max_priority: int
    track_passes: bool

    @property
    def has_manual_tle(self) -> bool:
        return bool(self.tle_line1 and self.tle_line2)


# --- Tabelas do próprio Scheduler --------------------------------------------
#
# O schema `public` é do TC Generator (fork de terceiros, sem migrations). O
# que o operador decide sobre passagens e recepção é do Scheduler — é ele quem
# planeja — e mora no schema `mission_control`, criado aqui no boot com
# `IF NOT EXISTS`, sem precisar de migração no repositório do outro.

CONTROL_SCHEMA = "mission_control"

# Tolerância para reconhecer "a mesma passagem" entre dois replanejamentos. O
# plano é apagado e recriado a cada ciclo, então o id da linha não serve de
# identidade: vale satélite + AOS previsto. Uma TLE nova desloca o AOS em
# segundos; duas passagens do mesmo satélite distam ~90 min.
PASS_MATCH_TOLERANCE = timedelta(minutes=10)

_CONTROL_DDL = (
    f"CREATE SCHEMA IF NOT EXISTS {CONTROL_SCHEMA}",
    f"""
    CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.satellite_reception (
        satellite_id          INTEGER PRIMARY KEY REFERENCES satellites(id) ON DELETE CASCADE,
        -- Sem linha para o satélite vale o padrão: rastrear toda passagem.
        track_passes          BOOLEAN NOT NULL DEFAULT TRUE,
        -- OBSOLETA: a frequência agora é por downlink (satellite_downlinks).
        -- Mantida para não quebrar uma versão anterior do Scheduler subindo
        -- contra este banco; ensure_control_schema migra e zera o valor.
        downlink_frequency_hz DOUBLE PRECISION
            CHECK (downlink_frequency_hz IS NULL OR downlink_frequency_hz > 0),
        updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.satellite_downlinks (
        id            BIGSERIAL PRIMARY KEY,
        satellite_id  INTEGER NOT NULL REFERENCES satellites(id) ON DELETE CASCADE,
        -- "beacon", "dados"... É o que o operador vê e o que o Station
        -- Manager loga ao rotear cada portadora para um rádio.
        name          TEXT NOT NULL,
        frequency_hz  DOUBLE PRECISION NOT NULL CHECK (frequency_hz > 0),
        enabled       BOOLEAN NOT NULL DEFAULT TRUE,
        -- Ordem do cadastro: o primeiro downlink ligado é a referência do
        -- Doppler, e numa faixa com dois downlinks fica o primeiro.
        position      INTEGER NOT NULL DEFAULT 0,
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (satellite_id, name)
    )
    """,
    # O erro do oscilador medido pelo ajuste fino (bloco FFT) na última
    # passagem: o painel sugere corrigir a frequência com ele. Colunas
    # adicionadas depois da tabela, daí o ALTER (CREATE TABLE IF NOT EXISTS
    # não adiciona coluna em banco existente).
    f"ALTER TABLE {CONTROL_SCHEMA}.satellite_downlinks "
    f"ADD COLUMN IF NOT EXISTS measured_offset_hz DOUBLE PRECISION",
    f"ALTER TABLE {CONTROL_SCHEMA}.satellite_downlinks "
    f"ADD COLUMN IF NOT EXISTS measured_at TIMESTAMPTZ",
    f"""
    CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.pass_decisions (
        id            BIGSERIAL PRIMARY KEY,
        satellite_id  INTEGER NOT NULL REFERENCES satellites(id) ON DELETE CASCADE,
        predicted_aos TIMESTAMPTZ NOT NULL,
        decision      TEXT NOT NULL CHECK (decision IN ('skip', 'force')),
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"CREATE INDEX IF NOT EXISTS pass_decisions_satellite_aos_idx "
    f"ON {CONTROL_SCHEMA}.pass_decisions (satellite_id, predicted_aos)",
    # --- Ciclo de vida do telecomando (tc_lifecycle.py) ----------------------
    # Cada resposta do satélite casada com uma transmissão. A chave é o quadro
    # decodificado: o mesmo raw packet reprocessado por outra versão do
    # decoder vira outra linha, e pode avançar o status (response_received ->
    # confirmed). Sem FK para uplink_transmissions e decoded_frames: são
    # tabelas de outros serviços, que podem subir depois deste.
    f"""
    CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.tc_responses (
        id                 BIGSERIAL PRIMARY KEY,
        telecommand_id     INTEGER NOT NULL REFERENCES telecommands(id) ON DELETE CASCADE,
        transmission_id    BIGINT NOT NULL,
        decoded_frame_id   BIGINT NOT NULL UNIQUE,
        raw_packet_id      BIGINT NOT NULL,
        pkt_id             SMALLINT NOT NULL,
        radio              TEXT,
        received_at        TIMESTAMPTZ NOT NULL,
        frame_status       TEXT NOT NULL,
        requester_callsign TEXT NOT NULL,
        echoed_tc_id       SMALLINT,
        error_code         SMALLINT,
        match_rule         TEXT NOT NULL,
        decision           TEXT NOT NULL,
        matched_at         TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"CREATE INDEX IF NOT EXISTS tc_responses_telecommand_idx "
    f"ON {CONTROL_SCHEMA}.tc_responses (telecommand_id, received_at)",
    # Sessão de uplink aberta pelo operador fora de uma passagem planejada
    # (contato manual, ou o simulador sem esperar uma passagem real). A API
    # escreve, o laço lê: as duas threads não compartilham objeto nenhum.
    f"""
    CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.uplink_sessions (
        id            BIGSERIAL PRIMARY KEY,
        satellite_id  INTEGER NOT NULL REFERENCES satellites(id) ON DELETE CASCADE,
        opened_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        until_time    TIMESTAMPTZ NOT NULL,
        closed_at     TIMESTAMPTZ,
        opened_by     TEXT
    )
    """,
    # Até onde o ciclo de vida já leu decoded_frames.
    f"""
    CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.tc_lifecycle_state (
        key    TEXT PRIMARY KEY,
        value  BIGINT NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.pass_predictions (
        id                   BIGSERIAL PRIMARY KEY,
        satellite_id         INTEGER NOT NULL REFERENCES satellites(id) ON DELETE CASCADE,
        aos_time             TIMESTAMPTZ NOT NULL,
        los_time             TIMESTAMPTZ NOT NULL,
        culmination_time     TIMESTAMPTZ,
        max_elevation_deg    DOUBLE PRECISION NOT NULL,
        outcome              TEXT NOT NULL
            CHECK (outcome IN ('planned', 'conflict', 'skipped', 'reception_off')),
        lost_to_satellite_id INTEGER,
        lost_to_aos_time     TIMESTAMPTZ,
        forced               BOOLEAN NOT NULL DEFAULT FALSE,
        carries_telecommands BOOLEAN NOT NULL DEFAULT FALSE,
        telecommand_count    INTEGER NOT NULL DEFAULT 0,
        computed_at          TIMESTAMPTZ NOT NULL
    )
    """,
)

# Colunas das tabelas próprias, conferidas no boot pelo schema_check.
CONTROL_COLUMNS: dict[str, tuple[str, ...]] = {
    "satellite_reception": (
        "satellite_id", "track_passes", "downlink_frequency_hz", "updated_at",
    ),
    "satellite_downlinks": (
        "id", "satellite_id", "name", "frequency_hz", "enabled", "position", "updated_at",
        "measured_offset_hz", "measured_at",
    ),
    "pass_decisions": ("id", "satellite_id", "predicted_aos", "decision", "created_at"),
    "tc_responses": (
        "id", "telecommand_id", "transmission_id", "decoded_frame_id", "raw_packet_id", "pkt_id",
        "radio", "received_at", "frame_status", "requester_callsign", "echoed_tc_id",
        "error_code", "match_rule", "decision", "matched_at",
    ),
    "uplink_sessions": ("id", "satellite_id", "opened_at", "until_time", "closed_at", "opened_by"),
    "tc_lifecycle_state": ("key", "value"),
    "pass_predictions": (
        "satellite_id", "aos_time", "los_time", "culmination_time", "max_elevation_deg",
        "outcome", "lost_to_satellite_id", "lost_to_aos_time", "forced",
        "carries_telecommands", "telecommand_count", "computed_at",
    ),
}


def ensure_control_schema(engine: Engine) -> None:
    """Cria as tabelas do Scheduler se faltarem. Idempotente: roda a cada boot."""
    with engine.begin() as conn:
        for statement in _CONTROL_DDL:
            conn.execute(text(statement))
        migrated = _migrate_single_frequency(conn)
    if migrated:
        logger.info("%d frequência(s) de downlink migradas para satellite_downlinks.", migrated)
    logger.info("Tabelas de controle de passagens prontas em %s.", CONTROL_SCHEMA)


def _migrate_single_frequency(conn: Connection) -> int:
    """A frequência única de `satellite_reception` vira o downlink "principal".

    Só para satélite que ainda não tem downlink nenhum, e o valor antigo é
    zerado na mesma transação: sem isso, um operador que apagasse todos os
    downlinks veria o "principal" renascer no próximo boot.
    """
    migrated = conn.execute(text(f"""
        INSERT INTO {CONTROL_SCHEMA}.satellite_downlinks (satellite_id, name, frequency_hz, position)
        SELECT r.satellite_id, 'principal', r.downlink_frequency_hz, 0
        FROM {CONTROL_SCHEMA}.satellite_reception r
        WHERE r.downlink_frequency_hz IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM {CONTROL_SCHEMA}.satellite_downlinks d
                          WHERE d.satellite_id = r.satellite_id)
    """)).rowcount
    conn.execute(text(f"""
        UPDATE {CONTROL_SCHEMA}.satellite_reception
        SET downlink_frequency_hz = NULL
        WHERE downlink_frequency_hz IS NOT NULL
    """))
    return migrated


def build_engine(database_url: str) -> Engine:
    # pool_pre_ping porque este processo fica horas ocioso entre ciclos e o
    # Postgres pode ter derrubado a conexão nesse meio tempo.
    return create_engine(database_url, pool_pre_ping=True, pool_size=2, max_overflow=2)


# --- Leitura ----------------------------------------------------------------

# Um telecomando ainda precisa de passagem quando está solto na fila, ou quando
# a passagem a que foi vinculado não vai mais acontecer. Um comando vinculado a
# uma passagem já concluída NÃO conta: sem os encoders não há como saber se foi
# de fato transmitido, mas reagendá-lo a cada ciclo faria a estação repetir a
# mesma passagem para sempre.
_NEEDS_A_PASS = """
    t.status IN ('pending', 'queued')
    AND (
        t.scheduled_pass_id IS NULL
        OR EXISTS (
            SELECT 1 FROM scheduled_passes sp
            WHERE sp.id = t.scheduled_pass_id
              AND sp.status IN ('planned', 'cancelled', 'missed')
        )
    )
"""


def fetch_candidate_satellites(conn: Connection) -> list[CandidateSatellite]:
    """Satélites ativos com dado orbital, com a fila e a escolha de recepção.

    Todos entram, tenham telecomando ou não: o satélite transmite telemetria
    em toda passagem, sem esperar uplink. Antes o JOIN com telecomandos tirava
    do plano quem não tinha comando na fila — e com ele a recepção.

    Os de recepção desligada e sem comando também vêm: o planejamento não os
    rastreia, mas mostra as passagens deles ao operador como "recepção
    desligada", que é onde ele decide religar.
    """
    rows = conn.execute(text(f"""
        SELECT s.id, s.name, s.code, s.norad_id, s.tle_line1, s.tle_line2,
               COALESCE(tc.pending, 0) AS pending_telecommands,
               COALESCE(tc.max_priority, 0) AS max_priority,
               COALESCE(r.track_passes, TRUE) AS track_passes
        FROM satellites s
        LEFT JOIN (
            SELECT t.satellite_id, COUNT(*) AS pending, MAX(t.priority) AS max_priority
            FROM telecommands t
            WHERE {_NEEDS_A_PASS}
            GROUP BY t.satellite_id
        ) tc ON tc.satellite_id = s.id
        LEFT JOIN {CONTROL_SCHEMA}.satellite_reception r ON r.satellite_id = s.id
        WHERE s.status = 'active'
          AND (s.norad_id IS NOT NULL
               OR (s.tle_line1 IS NOT NULL AND s.tle_line2 IS NOT NULL))
        ORDER BY s.id
    """)).mappings().all()
    return [CandidateSatellite(**row) for row in rows]


def fetch_pass_decisions(conn: Connection, since: datetime) -> list[dict[str, Any]]:
    """Decisões do operador (pular/forçar) para passagens ainda por vir."""
    return [dict(row) for row in conn.execute(text(f"""
        SELECT satellite_id, predicted_aos, decision
        FROM {CONTROL_SCHEMA}.pass_decisions
        WHERE predicted_aos >= :since
        ORDER BY predicted_aos
    """), {"since": since}).mappings().all()]


def prune_pass_decisions(conn: Connection, before: datetime) -> int:
    """Esquece decisões de passagens que já passaram — elas não decidem mais nada."""
    return conn.execute(text(f"""
        DELETE FROM {CONTROL_SCHEMA}.pass_decisions WHERE predicted_aos < :before
    """), {"before": before}).rowcount


def replace_pass_predictions(conn: Connection, rows: list[dict[str, Any]],
                             computed_at: datetime) -> None:
    """Troca a previsão inteira pela do replanejamento corrente.

    Estado corrente, não histórico: a previsão anterior foi feita com outra
    TLE e outra fila, e mantê-la ao lado confundiria o operador.
    """
    conn.execute(text(f"DELETE FROM {CONTROL_SCHEMA}.pass_predictions"))
    if not rows:
        return
    conn.execute(text(f"""
        INSERT INTO {CONTROL_SCHEMA}.pass_predictions (
            satellite_id, aos_time, los_time, culmination_time, max_elevation_deg,
            outcome, lost_to_satellite_id, lost_to_aos_time, forced,
            carries_telecommands, telecommand_count, computed_at
        ) VALUES (
            :satellite_id, :aos_time, :los_time, :culmination_time, :max_elevation_deg,
            :outcome, :lost_to_satellite_id, :lost_to_aos_time, :forced,
            :carries_telecommands, :telecommand_count, :computed_at
        )
    """), [{**row, "computed_at": computed_at} for row in rows])


def control_version(conn: Connection) -> tuple:
    """Algo que muda sempre que o operador muda uma decisão ou a recepção.

    Contagem e instante mais recente das duas tabelas: um INSERT muda o
    instante, um DELETE muda a contagem. O laço compara isto a cada poucos
    segundos para replanejar logo depois de uma ação do operador, sem esperar
    o ciclo de 5 minutos e sem compartilhar memória com a thread da API.
    """
    row = conn.execute(text(f"""
        SELECT (SELECT COUNT(*) FROM {CONTROL_SCHEMA}.pass_decisions),
               (SELECT MAX(created_at) FROM {CONTROL_SCHEMA}.pass_decisions),
               (SELECT COUNT(*) FROM {CONTROL_SCHEMA}.satellite_reception),
               (SELECT MAX(updated_at) FROM {CONTROL_SCHEMA}.satellite_reception)
    """)).one()
    return tuple(row)


def fetch_satellites_with_orbital_data(conn: Connection) -> list[dict[str, Any]]:
    """Todos os satélites rastreáveis, tenham trabalho pendente ou não.

    Usado para manter a posição corrente no banco: o painel mostra onde cada
    satélite está, e isso não depende de haver telecomando na fila.
    """
    return [dict(row) for row in conn.execute(text("""
        SELECT id, name, code, norad_id, tle_line1, tle_line2
        FROM satellites
        WHERE status = 'active'
          AND (norad_id IS NOT NULL
               OR (tle_line1 IS NOT NULL AND tle_line2 IS NOT NULL))
        ORDER BY id
    """)).mappings().all()]


def fetch_station_overview(conn: Connection) -> dict[str, Any]:
    """Tudo o que o monitor precisa, numa transação só.

    Ler em partes daria um retrato inconsistente: o plano de um instante com as
    posições de outro, justamente quando algo está mudando.
    """
    satellites = [dict(row) for row in conn.execute(text("""
        SELECT s.id, s.name, s.code, s.norad_id, s.status,
               (s.tle_line1 IS NOT NULL AND s.tle_line2 IS NOT NULL) AS has_manual_tle,
               ts.latitude_deg, ts.longitude_deg, ts.altitude_km,
               ts.azimuth_deg, ts.elevation_deg, ts.range_km,
               ts.is_visible, ts.data_source, ts.status_message, ts.checked_at
        FROM satellites s
        LEFT JOIN satellite_tracking_status ts ON ts.satellite_id = s.id
        ORDER BY s.id
    """)).mappings().all()]

    plan = [dict(row) for row in conn.execute(text("""
        SELECT p.id, p.satellite_id, s.name AS satellite_name,
               p.aos_time, p.los_time, p.max_elevation_deg, p.status,
               p.status_message, p.data_source,
               (SELECT COUNT(*) FROM telecommands t WHERE t.scheduled_pass_id = p.id)
                   AS telecommand_count
        FROM scheduled_passes p
        JOIN satellites s ON s.id = p.satellite_id
        WHERE p.status IN ('planned', 'active')
           OR p.los_time > now() - interval '1 hour'
        ORDER BY p.aos_time
        LIMIT 10
    """)).mappings().all()]

    counts = {row["status"]: row["total"] for row in conn.execute(text("""
        SELECT status, COUNT(*) AS total FROM telecommands GROUP BY status
    """)).mappings().all()}

    return {"satellites": satellites, "plan": plan, "telecommand_counts": counts}


def fetch_pass_to_activate(conn: Connection, now: datetime) -> Optional[dict[str, Any]]:
    """Passagem planejada cuja janela já começou e ainda não terminou.

    Traz os downlinks ligados do satélite, em ordem: vão no `track_satellite`
    para o Station Manager anunciar a sintonia de cada rádio.

    `name` além de `satellite_name`: `load_orbital_data` lê `name` para montar
    os dados de um TLE manual. Sem ele, todo satélite de TLE manual falhava
    ao ser assumido e a passagem virava 'cancelled'.
    """
    row = conn.execute(text(f"""
        SELECT p.id, p.satellite_id, p.aos_time, p.los_time, p.max_elevation_deg,
               s.name, s.name AS satellite_name, s.norad_id, s.tle_line1, s.tle_line2,
               (SELECT COALESCE(json_agg(json_build_object('name', d.name,
                                                          'frequency_hz', d.frequency_hz)
                                        ORDER BY d.position, d.id), '[]'::json)
                FROM {CONTROL_SCHEMA}.satellite_downlinks d
                WHERE d.satellite_id = s.id AND d.enabled) AS downlinks
        FROM scheduled_passes p
        JOIN satellites s ON s.id = p.satellite_id
        WHERE p.status = 'planned'
          AND p.aos_time <= :now
          AND p.los_time > :now
        ORDER BY p.aos_time
        LIMIT 1
    """), {"now": now}).mappings().first()
    return dict(row) if row else None


def fetch_active_pass(conn: Connection) -> Optional[dict[str, Any]]:
    row = conn.execute(text(f"""
        SELECT p.id, p.satellite_id, p.aos_time, p.los_time,
               s.name, s.name AS satellite_name, s.norad_id, s.tle_line1, s.tle_line2,
               (SELECT COALESCE(json_agg(json_build_object('name', d.name,
                                                          'frequency_hz', d.frequency_hz)
                                        ORDER BY d.position, d.id), '[]'::json)
                FROM {CONTROL_SCHEMA}.satellite_downlinks d
                WHERE d.satellite_id = s.id AND d.enabled) AS downlinks
        FROM scheduled_passes p
        JOIN satellites s ON s.id = p.satellite_id
        WHERE p.status = 'active'
        ORDER BY p.aos_time
        LIMIT 1
    """)).mappings().first()
    return dict(row) if row else None


# --- Escrita ----------------------------------------------------------------

# Quando o desvio do ajuste fino vale como medida do oscilador: a malha já
# deu alguns passos E o último resíduo é pequeno. Antes disso ela ainda está
# convergindo, e o valor é um ponto no meio do caminho — foi o que aconteceu
# quando o Station Manager reiniciou no meio de uma passagem: o painel sugeriu
# +898 Hz para um oscilador de +1200.
CONVERGED_MIN_UPDATES = 3
CONVERGED_MAX_RESIDUAL_HZ = 50.0


def afc_converged(downlink: dict) -> bool:
    residual = downlink.get("afc_last_residual_hz")
    return (int(downlink.get("afc_updates") or 0) >= CONVERGED_MIN_UPDATES
            and residual is not None and abs(float(residual)) <= CONVERGED_MAX_RESIDUAL_HZ)


def record_measured_offsets(conn: Connection, satellite_id: int, downlinks: list[dict]) -> int:
    """Guarda o desvio que o ajuste fino aprendeu em cada downlink.

    Só quando a malha convergiu (ver afc_converged): um desvio zero porque
    nenhuma rajada foi medida, ou um desvio no meio da convergência, não é
    medida do oscilador.
    """
    updated = 0
    for downlink in downlinks or []:
        if not afc_converged(downlink):
            continue
        updated += conn.execute(text(f"""
            UPDATE {CONTROL_SCHEMA}.satellite_downlinks
            SET measured_offset_hz = :offset, measured_at = now()
            WHERE satellite_id = :satellite_id AND name = :name
        """), {"offset": float(downlink["offset_hz"]), "satellite_id": satellite_id,
               "name": downlink["name"]}).rowcount
    return updated


def clear_planned_passes(conn: Connection) -> int:
    """Descarta o plano ainda não iniciado, devolvendo os telecomandos à fila.

    Só apaga passagens 'planned': uma passagem 'active' está acontecendo agora,
    com o rotor em movimento, e replanejar não pode interrompê-la.

    Devolve à fila também os telecomandos de passagens 'cancelled' e 'missed'.
    Elas não são apagadas (o histórico de por que uma janela não aconteceu tem
    valor), mas os comandos presos nelas precisam voltar a circular — do
    contrário ficariam para sempre em 'queued', apontando para uma passagem que
    não vai acontecer, e nenhum replanejamento os veria de novo.

    O status volta a 'pending' explicitamente: o ON DELETE SET NULL da FK zera
    o vínculo, mas deixaria o status em 'queued'.
    """
    conn.execute(text("""
        UPDATE telecommands
        SET status = 'pending', scheduled_pass_id = NULL
        WHERE status = 'queued'
          AND scheduled_pass_id IN (
              SELECT id FROM scheduled_passes
              WHERE status IN ('planned', 'cancelled', 'missed')
          )
    """))
    return conn.execute(text("DELETE FROM scheduled_passes WHERE status = 'planned'")).rowcount


def insert_planned_pass(conn: Connection, satellite_id: int, satellite_pass, tle_epoch,
                        data_source: str) -> int:
    return conn.execute(text("""
        INSERT INTO scheduled_passes (
            satellite_id, aos_time, los_time, culmination_time,
            max_elevation_deg, aos_azimuth_deg, los_azimuth_deg,
            tle_epoch, data_source, status
        ) VALUES (
            :satellite_id, :aos_time, :los_time, :culmination_time,
            :max_elevation_deg, :aos_azimuth_deg, :los_azimuth_deg,
            :tle_epoch, :data_source, 'planned'
        ) RETURNING id
    """), {
        "satellite_id": satellite_id,
        "aos_time": satellite_pass.aos_time,
        "los_time": satellite_pass.los_time,
        "culmination_time": satellite_pass.culmination_time,
        "max_elevation_deg": satellite_pass.max_elevation_deg,
        "aos_azimuth_deg": satellite_pass.aos_azimuth_deg,
        "los_azimuth_deg": satellite_pass.los_azimuth_deg,
        "tle_epoch": tle_epoch,
        "data_source": data_source,
    }).scalar_one()


def assign_telecommands_to_pass(conn: Connection, pass_id: int, satellite_id: int) -> int:
    """Compromete os telecomandos pendentes do satélite com esta passagem.

    Não marca 'sent': transmitir de fato é trabalho dos encoders/moduladores,
    que ainda não existem. 'queued' diz o que é verdade — o comando tem hora
    marcada para sair.
    """
    return conn.execute(text("""
        UPDATE telecommands
        SET status = 'queued', scheduled_pass_id = :pass_id
        WHERE satellite_id = :satellite_id
          AND status = 'pending'
          AND scheduled_pass_id IS NULL
    """), {"pass_id": pass_id, "satellite_id": satellite_id}).rowcount


def delete_pass(conn: Connection, pass_id: int) -> None:
    conn.execute(text("DELETE FROM scheduled_passes WHERE id = :pass_id"),
                 {"pass_id": pass_id})


def release_telecommands(conn: Connection, pass_ids: Iterable[int]) -> int:
    """Devolve à fila os telecomandos de passagens que não vão mais acontecer.

    Sem isso, um comando vinculado a uma passagem perdida ficaria preso em
    'queued' para sempre: o replanejamento não o veria como pendente e ele
    nunca seria reagendado.
    """
    pass_ids = list(pass_ids)
    if not pass_ids:
        return 0
    return conn.execute(text("""
        UPDATE telecommands
        SET status = 'pending', scheduled_pass_id = NULL
        WHERE scheduled_pass_id = ANY(:pass_ids)
          AND status = 'queued'
    """), {"pass_ids": pass_ids}).rowcount


def mark_pass(conn: Connection, pass_id: int, status: str,
              message: Optional[str] = None) -> None:
    conn.execute(text("""
        UPDATE scheduled_passes
        SET status = :status, status_message = :message
        WHERE id = :pass_id
    """), {"pass_id": pass_id, "status": status, "message": message})


def expire_missed_passes(conn: Connection, now: datetime) -> list[int]:
    """Marca como 'missed' as janelas que passaram sem a estação assumir.

    Distingue "não rastreamos" de "rastreamos e falhou" — sem isso, uma
    passagem perdida ficaria indistinguível de uma que nunca foi planejada.
    """
    rows = conn.execute(text("""
        UPDATE scheduled_passes
        SET status = 'missed',
            status_message = 'janela encerrada sem a estação assumir a passagem'
        WHERE status = 'planned' AND los_time <= :now
        RETURNING id
    """), {"now": now}).scalars().all()
    return list(rows)


def close_finished_passes(conn: Connection, now: datetime) -> list[int]:
    rows = conn.execute(text("""
        UPDATE scheduled_passes
        SET status = 'completed'
        WHERE status = 'active' AND los_time <= :now
        RETURNING id
    """), {"now": now}).scalars().all()
    return list(rows)


# Motivo registrado em execution_logs a cada marcação por fim de janela. A
# estação não tem encoder nem modulador: ela sabe que rastreou a passagem, não
# que o rádio transmitiu. Guardar isso aqui mantém a distinção entre um 'sent'
# inferido e um confirmado — quando os fluxos de dados existirem, o confirmado
# virá com evidência, e estes continuarão identificáveis por esta mensagem.
SENT_BY_PASS_COMPLETION = (
    "marcado como enviado ao fim da janela rastreada; sem confirmação de "
    "transmissão (estação ainda sem encoder/modulador)"
)


def mark_telecommands_sent(conn: Connection, pass_ids: Iterable[int]) -> int:
    """Marca como 'sent' os telecomandos das passagens que a estação concluiu.

    Só passagens 'completed' chegam aqui: uma janela perdida devolve os seus
    comandos à fila (`release_telecommands`), porque nesse caso a estação
    sequer apontou a antena.
    """
    pass_ids = list(pass_ids)
    if not pass_ids:
        return 0

    sent = conn.execute(text("""
        UPDATE telecommands
        SET status = 'sent', sent_at = now()
        WHERE scheduled_pass_id = ANY(:pass_ids)
          AND status = 'queued'
        RETURNING id
    """), {"pass_ids": pass_ids}).scalars().all()

    for telecommand_id in sent:
        conn.execute(text("""
            INSERT INTO execution_logs (telecommand_id, status, message)
            VALUES (:telecommand_id, 'sent', :message)
        """), {"telecommand_id": telecommand_id, "message": SENT_BY_PASS_COMPLETION})

    return len(sent)


def upsert_tracking_status(conn: Connection, satellite_id: int, **fields: Any) -> None:
    """Grava a posição corrente de um satélite (uma linha por satélite).

    Estado atual, não histórico: por isso ON CONFLICT DO UPDATE em vez de
    INSERT. Um satélite que falha guarda o motivo em status_message, para que
    uma falha isolada fique visível em vez de virar silêncio.
    """
    columns = [
        "latitude_deg", "longitude_deg", "altitude_km",
        "azimuth_deg", "elevation_deg", "range_km",
        "is_visible", "tle_epoch", "data_source", "status_message", "checked_at",
    ]
    params = {"satellite_id": satellite_id}
    params.update({column: fields.get(column) for column in columns})
    params["is_visible"] = bool(fields.get("is_visible", False))

    assignments = ", ".join(f"{column} = EXCLUDED.{column}" for column in columns)
    conn.execute(text(f"""
        INSERT INTO satellite_tracking_status (satellite_id, {", ".join(columns)})
        VALUES (:satellite_id, {", ".join(f":{column}" for column in columns)})
        ON CONFLICT (satellite_id) DO UPDATE SET {assignments}
    """), params)
