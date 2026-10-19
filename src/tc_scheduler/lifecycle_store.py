"""O SQL do ciclo de vida do telecomando (tc_lifecycle.py).

Lê:
- `mission_control.uplink_transmissions` (do grs-uplink);
- `mission_control.decoded_frames` e `fs2_tc_feedback` (do grs-telemetry-decoder).

Escreve:
- `telecommands.status` e `execution_logs` (do TC Generator; o Scheduler é o
  único que escreve status);
- as tabelas próprias em `mission_control`: `tc_responses`, `uplink_sessions`
  e `tc_lifecycle_state`, criadas em `db.ensure_control_schema`.

Toda transição de status tem guarda de estado de origem (`WHERE status = ...`):
se dois ticks, ou o operador pelo TC Generator, mexerem no mesmo TC, quem
chegar depois não desfaz o que o outro fez.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from tc_scheduler.tc_lifecycle import (
    AWAITING,
    RESPONSE_PKT_IDS,
    RESPONSE_RADIO,
    Decision,
    ResponseFrame,
    Transmission,
)

SCHEMA = "mission_control"
CURSOR_KEY = "last_decoded_frame_id"

_TRANSMISSION_COLUMNS = """
    t.id AS transmission_id, t.telecommand_id, tc.satellite_id, t.tc_pkt_id,
    t.expected_response_pkt_id, t.station_callsign, t.ended_at, tc.status AS telecommand_status
"""


def _transmission(row) -> Transmission:
    return Transmission(**{key: row[key] for key in (
        "transmission_id", "telecommand_id", "satellite_id", "tc_pkt_id",
        "expected_response_pkt_id", "station_callsign", "ended_at", "telecommand_status")})


def _log(conn, telecommand_id: int, status: str, message: str, details: dict[str, Any]) -> None:
    conn.execute(text("""
        INSERT INTO execution_logs (telecommand_id, status, message, details)
        VALUES (:id, :status, :message, CAST(:details AS JSONB))
    """), {"id": telecommand_id, "status": status, "message": message,
           "details": json.dumps(details, default=str)})


class PostgresLifecycleStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- cursor dos quadros ------------------------------------------------------

    def cursor(self) -> int:
        with self._engine.begin() as conn:
            value = conn.execute(text(f"SELECT value FROM {SCHEMA}.tc_lifecycle_state WHERE key = :k"),
                                 {"k": CURSOR_KEY}).scalar()
            if value is not None:
                return int(value)
            # Primeira vez: começa do quadro mais novo. Respostas mais antigas
            # que o próprio ciclo de vida não têm transmissão para casar.
            start = conn.execute(text(f"SELECT COALESCE(MAX(id), 0) FROM {SCHEMA}.decoded_frames")).scalar()
            conn.execute(text(f"""
                INSERT INTO {SCHEMA}.tc_lifecycle_state (key, value) VALUES (:k, :v)
                ON CONFLICT (key) DO NOTHING
            """), {"k": CURSOR_KEY, "v": start})
            return int(start)

    def save_cursor(self, cursor: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(f"UPDATE {SCHEMA}.tc_lifecycle_state SET value = :v WHERE key = :k"),
                         {"k": CURSOR_KEY, "v": cursor})

    def frames_after(self, cursor: int, limit: int) -> list[ResponseFrame]:
        with self._engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT f.id, f.raw_packet_id, f.received_at, f.radio, f.status, f.pkt_id,
                       f.ngham_payload, f.detail, fb.error_code
                FROM {SCHEMA}.decoded_frames f
                LEFT JOIN {SCHEMA}.fs2_tc_feedback fb ON fb.decoded_frame_id = f.id
                WHERE f.id > :cursor AND f.pkt_id = ANY(:pkts) AND f.ngham_payload IS NOT NULL
                ORDER BY f.id
                LIMIT :limit
            """), {"cursor": cursor, "pkts": list(RESPONSE_PKT_IDS), "limit": limit}).mappings().all()
        return [ResponseFrame(
            decoded_frame_id=row["id"], raw_packet_id=row["raw_packet_id"],
            received_at=row["received_at"], radio=row["radio"], status=row["status"],
            pkt_id=row["pkt_id"], payload=bytes(row["ngham_payload"]), detail=row["detail"],
            error_code=row["error_code"],
        ) for row in rows]

    # --- casamento ----------------------------------------------------------------

    def candidate_transmissions(self, frame: ResponseFrame, late_window: timedelta) -> list[Transmission]:
        with self._engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT {_TRANSMISSION_COLUMNS}
                FROM {SCHEMA}.uplink_transmissions t
                JOIN telecommands tc ON tc.id = t.telecommand_id
                WHERE t.status = 'ack' AND t.ended_at IS NOT NULL
                  AND t.expected_response_pkt_id = :pkt
                  AND t.ended_at >= :received_at - make_interval(secs => :late)
                  AND t.ended_at <= :received_at + interval '2 seconds'
                ORDER BY t.ended_at DESC
                LIMIT 20
            """), {"pkt": frame.pkt_id, "received_at": frame.received_at,
                   "late": late_window.total_seconds()}).mappings().all()
        return [_transmission(row) for row in rows]

    def record_response(self, frame: ResponseFrame, transmission: Transmission, rule: str,
                        decision: Decision) -> bool:
        with self._engine.begin() as conn:
            return conn.execute(text(f"""
                INSERT INTO {SCHEMA}.tc_responses (
                    telecommand_id, transmission_id, decoded_frame_id, raw_packet_id, pkt_id,
                    radio, received_at, frame_status, requester_callsign, echoed_tc_id,
                    error_code, match_rule, decision)
                VALUES (:tc, :tx, :frame, :raw, :pkt, :radio, :received, :frame_status,
                        :requester, :echoed, :code, :rule, :decision)
                ON CONFLICT (decoded_frame_id) DO NOTHING
            """), {
                "tc": transmission.telecommand_id, "tx": transmission.transmission_id,
                "frame": frame.decoded_frame_id, "raw": frame.raw_packet_id, "pkt": frame.pkt_id,
                "radio": frame.radio, "received": frame.received_at, "frame_status": frame.status,
                "requester": frame.requester_callsign, "echoed": frame.echoed_tc_id,
                "code": frame.error_code, "rule": rule, "decision": decision.status,
            }).rowcount == 1

    def apply_response(self, transmission: Transmission, frame: ResponseFrame,
                       decision: Decision, rule: str) -> bool:
        # response_received não rebaixa um TC que outra resposta já fechou.
        allowed = list(AWAITING) if decision.status in ("confirmed", "failed") else ["sent", "no_response"]
        with self._engine.begin() as conn:
            updated = conn.execute(text("""
                UPDATE telecommands
                SET status = :status, status_message = :message,
                    confirmed_at = CASE WHEN :status = 'confirmed' THEN :received ELSE confirmed_at END
                WHERE id = :id AND status = ANY(:allowed)
                RETURNING id
            """), {"status": decision.status, "message": decision.message,
                   "received": frame.received_at, "id": transmission.telecommand_id,
                   "allowed": allowed}).first()
            if updated is None:
                return False
            _log(conn, transmission.telecommand_id, decision.status, decision.message, {
                "transmission_id": transmission.transmission_id,
                "decoded_frame_id": frame.decoded_frame_id, "raw_packet_id": frame.raw_packet_id,
                "pkt_id": f"0x{frame.pkt_id:02X}", "radio": frame.radio,
                "frame_status": frame.status, "frame_detail": frame.detail,
                "error_code": frame.error_code, "match_rule": rule,
            })
        return True

    # --- prazos ----------------------------------------------------------------------

    def expired(self, now: datetime, timeouts: dict[int, timedelta]) -> list[Transmission]:
        with self._engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT DISTINCT ON (t.telecommand_id) {_TRANSMISSION_COLUMNS}
                FROM {SCHEMA}.uplink_transmissions t
                JOIN telecommands tc ON tc.id = t.telecommand_id
                WHERE tc.status = 'sent' AND t.status = 'ack' AND t.ended_at IS NOT NULL
                ORDER BY t.telecommand_id, t.ended_at DESC
            """)).mappings().all()
        expired = []
        for row in rows:
            transmission = _transmission(row)
            timeout = timeouts.get(transmission.expected_response_pkt_id)
            if timeout is not None and transmission.ended_at + timeout < now:
                expired.append(transmission)
        return expired

    def mark_no_response(self, transmission: Transmission, now: datetime, timeout: timedelta) -> bool:
        radio = RESPONSE_RADIO.get(transmission.expected_response_pkt_id)
        window_end = transmission.ended_at + timeout
        with self._engine.begin() as conn:
            # A evidência que houver: quadros que chegaram no rádio esperado,
            # dentro da janela, mas que nem o NGHam conseguiu ler.
            unreadable = conn.execute(text(f"""
                SELECT COUNT(*) FROM {SCHEMA}.decoded_frames
                WHERE status = 'ngham_failed' AND radio = :radio
                  AND received_at BETWEEN :start AND :end
            """), {"radio": radio, "start": transmission.ended_at, "end": window_end}).scalar()
            message = (f"sem resposta 0x{transmission.expected_response_pkt_id:02X} em "
                       f"{timeout.total_seconds():.0f} s")
            if unreadable:
                message += f"; {unreadable} quadro(s) ilegível(is) na {radio.upper()} na janela"
            updated = conn.execute(text("""
                UPDATE telecommands SET status = 'no_response', status_message = :message
                WHERE id = :id AND status = 'sent'
                RETURNING id
            """), {"id": transmission.telecommand_id, "message": message}).first()
            if updated is None:
                return False
            _log(conn, transmission.telecommand_id, "no_response", message, {
                "transmission_id": transmission.transmission_id,
                "expected_response": f"0x{transmission.expected_response_pkt_id:02X}",
                "expected_radio": radio, "window_start": transmission.ended_at,
                "window_end": window_end, "ngham_failed_in_window": unreadable,
            })
        return True

    # --- sessões e envio -----------------------------------------------------------------

    def open_sessions(self, now: datetime) -> list[int]:
        with self._engine.connect() as conn:
            return list(conn.execute(text(f"""
                SELECT DISTINCT satellite_id FROM {SCHEMA}.uplink_sessions
                WHERE closed_at IS NULL AND until_time > :now
            """), {"now": now}).scalars())

    def awaiting(self, satellite_id: int) -> bool:
        """Há um TC deste satélite no ar esperando resposta?

        Só conta o `sent` que tem transmissão com `ack`. Um `sent` antigo,
        inferido no fim de uma passagem antes da fatia de uplink, nunca vai
        receber resposta nem vencer prazo (não há transmissão para medir), e
        travaria o stop-and-wait do satélite para sempre.
        """
        with self._engine.connect() as conn:
            return conn.execute(text(f"""
                SELECT 1 FROM telecommands tc
                WHERE tc.satellite_id = :s AND tc.status = 'sent'
                  AND EXISTS (SELECT 1 FROM {SCHEMA}.uplink_transmissions t
                              WHERE t.telecommand_id = tc.id AND t.status = 'ack')
                LIMIT 1
            """), {"s": satellite_id}).first() is not None

    def next_telecommand(self, satellite_id: int, pass_id: Optional[int]) -> Optional[int]:
        """Na passagem: os `queued` dela. Em sessão manual: `pending` e `queued`."""
        if pass_id is not None:
            condition, params = "status = 'queued' AND scheduled_pass_id = :pass", {"pass": pass_id}
        else:
            condition, params = "status IN ('pending', 'queued')", {}
        with self._engine.connect() as conn:
            return conn.execute(text(f"""
                SELECT id FROM telecommands
                WHERE satellite_id = :s AND {condition}
                ORDER BY priority DESC, created_at, id
                LIMIT 1
            """), {"s": satellite_id, **params}).scalar()

    def mark_sent(self, telecommand_id: int, reply: dict[str, Any], now: datetime) -> bool:
        message = "transmitido; o rádio confirmou a rajada inteira (N1)"
        with self._engine.begin() as conn:
            updated = conn.execute(text("""
                UPDATE telecommands SET status = 'sent', sent_at = :now, status_message = :message
                WHERE id = :id AND status IN ('pending', 'queued')
                RETURNING id
            """), {"id": telecommand_id, "now": now, "message": message}).first()
            if updated is None:
                return False
            _log(conn, telecommand_id, "sent", message, {
                "transmission_id": reply.get("transmission_id"), "evidence": "N1",
                "tx_freq_hz": reply.get("tx_freq_hz"), "doppler_hz": reply.get("doppler_hz"),
                "expected_response": reply.get("expected_response_pkt_id"),
            })
        return True

    def mark_rejected(self, telecommand_id: int, detail: str) -> bool:
        message = f"rejeitado no solo: {detail}"
        with self._engine.begin() as conn:
            updated = conn.execute(text("""
                UPDATE telecommands SET status = 'failed', status_message = :message
                WHERE id = :id AND status IN ('pending', 'queued')
                RETURNING id
            """), {"id": telecommand_id, "message": message}).first()
            if updated is None:
                return False
            _log(conn, telecommand_id, "failed", message, {"stage": "uplink", "error": "rejected"})
        return True

    def log_tx_failure(self, telecommand_id: int, reply: dict[str, Any]) -> None:
        status = reply.get("status") or reply.get("error") or "erro"
        with self._engine.begin() as conn:
            _log(conn, telecommand_id, "tx_failed",
                 f"não transmitido ({status}): {reply.get('detail') or 'sem detalhe'}", reply)
