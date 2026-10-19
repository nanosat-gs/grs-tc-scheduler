"""O que o operador faz com o uplink pela API: abrir e fechar sessões, ver o que subiu.

Uma sessão de uplink é uma janela em que o Scheduler transmite os TCs de um
satélite fora de uma passagem planejada — um contato manual, ou o simulador
sem esperar uma passagem de verdade. Durante a sessão vale a mesma regra da
passagem: um TC por vez, o próximo só depois da resposta ou do prazo.

A API escreve em `mission_control.uplink_sessions`; o laço do Scheduler lê.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

from tc_scheduler.operator_control import ControlError, NotFound

SCHEMA = "mission_control"


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


class UplinkControl:
    def __init__(self, engine, max_session_s: float) -> None:
        self._engine = engine
        self._max_session_s = max_session_s

    def _satellite_id(self, conn, satellite: Any) -> int:
        if isinstance(satellite, bool) or not isinstance(satellite, (int, str)) or satellite == "":
            raise ControlError("satellite: informe o código ou o id do satélite")
        row = conn.execute(text(
            "SELECT id FROM satellites WHERE code = :code OR id::text = :code LIMIT 1"),
            {"code": str(satellite)}).first()
        if row is None:
            raise NotFound(f"satélite {satellite!r} não existe")
        return row.id

    def open_session(self, body: Any, now: Optional[datetime] = None) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ControlError("esperava um objeto JSON {satellite, duration_s}")
        duration = body.get("duration_s", 600)
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) \
                or not 0 < duration <= self._max_session_s:
            raise ControlError(f"duration_s: de 1 a {self._max_session_s:.0f} s")
        now = now or datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            satellite_id = self._satellite_id(conn, body.get("satellite"))
            row = conn.execute(text(f"""
                INSERT INTO {SCHEMA}.uplink_sessions (satellite_id, opened_at, until_time, opened_by)
                VALUES (:s, :now, :until, :by)
                RETURNING id, satellite_id, opened_at, until_time
            """), {"s": satellite_id, "now": now, "until": now + timedelta(seconds=duration),
                   "by": str(body.get("opened_by") or "api")}).mappings().one()
        return {"session": {"id": row["id"], "satellite_id": row["satellite_id"],
                            "opened_at": _iso(row["opened_at"]), "until": _iso(row["until_time"])}}

    def close_session(self, session_id: int, now: Optional[datetime] = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            row = conn.execute(text(f"""
                UPDATE {SCHEMA}.uplink_sessions SET closed_at = :now
                WHERE id = :id AND closed_at IS NULL
                RETURNING id
            """), {"id": session_id, "now": now}).first()
        if row is None:
            raise NotFound(f"sessão {session_id} não existe ou já foi fechada")
        return {"closed": session_id}

    def overview(self, now: Optional[datetime] = None, limit: int = 20) -> dict[str, Any]:
        """Sessões abertas, e as últimas transmissões com o que aconteceu com cada TC."""
        now = now or datetime.now(timezone.utc)
        with self._engine.connect() as conn:
            sessions = conn.execute(text(f"""
                SELECT u.id, s.code, u.opened_at, u.until_time
                FROM {SCHEMA}.uplink_sessions u JOIN satellites s ON s.id = u.satellite_id
                WHERE u.closed_at IS NULL AND u.until_time > :now
                ORDER BY u.opened_at
            """), {"now": now}).mappings().all()
            transmissions = conn.execute(text(f"""
                SELECT t.id, t.telecommand_id, t.attempt, t.command_type, t.status AS radio_status,
                       t.tx_freq_hz, t.doppler_hz, t.ended_at, t.detail,
                       tc.status AS telecommand_status, tc.status_message,
                       (SELECT json_agg(json_build_object(
                                'pkt_id', r.pkt_id, 'radio', r.radio, 'received_at', r.received_at,
                                'frame_status', r.frame_status, 'error_code', r.error_code,
                                'decision', r.decision) ORDER BY r.received_at)
                        FROM {SCHEMA}.tc_responses r WHERE r.transmission_id = t.id) AS responses
                FROM {SCHEMA}.uplink_transmissions t
                JOIN telecommands tc ON tc.id = t.telecommand_id
                ORDER BY t.id DESC
                LIMIT :limit
            """), {"limit": limit}).mappings().all()
        return {
            "sessions": [{"id": s["id"], "satellite": s["code"], "opened_at": _iso(s["opened_at"]),
                          "until": _iso(s["until_time"])} for s in sessions],
            "transmissions": [{**{k: v for k, v in t.items() if k != "ended_at"},
                               "ended_at": _iso(t["ended_at"]),
                               "responses": t["responses"] or []} for t in transmissions],
        }
