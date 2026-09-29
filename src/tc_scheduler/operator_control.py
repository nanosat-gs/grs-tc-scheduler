"""O que o operador decide sobre passagens e recepção, pelo painel.

Separado de `station_data` de propósito: aquele módulo só lê, e continua só
lendo. Aqui mora a única escrita que a API faz no banco, e ela vai só para as
tabelas do próprio Scheduler (`mission_control`), nunca para o schema do TC
Generator. O plano em si (`scheduled_passes`) continua sendo escrito só pelo
laço de planejamento: o operador registra a intenção, o laço percebe e
replaneja em segundos (`Scheduler.replan_if_operator_changed`).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Optional

from tc_scheduler.db import CONTROL_SCHEMA, PASS_MATCH_TOLERANCE

logger = logging.getLogger(__name__)

DECISIONS = ("skip", "force")

# Faixa do USRP N210 (DC a 6 GHz, conforme a daughterboard). Fora disso é
# erro de unidade — MHz digitado onde se esperava Hz, quase sempre.
MIN_FREQUENCY_HZ = 1e6
MAX_FREQUENCY_HZ = 6e9


class ControlError(ValueError):
    """Pedido do operador inválido; a mensagem vai para a tela como está."""


class NotFound(LookupError):
    """Satélite com esse código não existe."""


_PREDICTIONS_SQL = f"""
    SELECT p.aos_time, p.los_time, p.culmination_time, p.max_elevation_deg,
           p.outcome, p.forced, p.carries_telecommands, p.telecommand_count,
           p.computed_at, p.lost_to_aos_time,
           s.code AS satellite_code, s.name AS satellite_name,
           w.name AS lost_to_satellite_name,
           d.decision
    FROM {CONTROL_SCHEMA}.pass_predictions p
    JOIN satellites s ON s.id = p.satellite_id
    LEFT JOIN satellites w ON w.id = p.lost_to_satellite_id
    LEFT JOIN LATERAL (
        SELECT d.decision
        FROM {CONTROL_SCHEMA}.pass_decisions d
        WHERE d.satellite_id = p.satellite_id
          AND abs(extract(epoch FROM d.predicted_aos - p.aos_time)) <= :tolerance
        ORDER BY abs(extract(epoch FROM d.predicted_aos - p.aos_time))
        LIMIT 1
    ) d ON TRUE
    WHERE p.los_time >= now()
    ORDER BY p.aos_time
"""

_RECEPTION_SQL = f"""
    SELECT s.code, s.name, s.status,
           COALESCE(r.track_passes, TRUE) AS track_passes,
           r.downlink_frequency_hz
    FROM satellites s
    LEFT JOIN {CONTROL_SCHEMA}.satellite_reception r ON r.satellite_id = s.id
    ORDER BY s.name
"""


class OperatorControl:
    def __init__(self, engine) -> None:
        self._engine = engine

    # --- Leitura --------------------------------------------------------------

    def predictions(self) -> dict[str, Any]:
        """As passagens previstas no horizonte, escolhidas ou não, e por quê."""
        from sqlalchemy import text

        with self._engine.connect() as conn:
            passes = [dict(r) for r in conn.execute(
                text(_PREDICTIONS_SQL),
                {"tolerance": PASS_MATCH_TOLERANCE.total_seconds()},
            ).mappings()]
            reception = [dict(r) for r in conn.execute(text(_RECEPTION_SQL)).mappings()]

        computed_at = max((p["computed_at"] for p in passes), default=None)
        return {
            "computed_at": _iso(computed_at),
            "passes": [_prediction_to_dict(p) for p in passes],
            "reception": [
                {
                    "code": r["code"], "name": r["name"], "status": r["status"],
                    "track_passes": bool(r["track_passes"]),
                    "downlink_frequency_hz": r["downlink_frequency_hz"],
                }
                for r in reception
            ],
        }

    # --- Escrita --------------------------------------------------------------

    def set_reception(self, code: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Liga/desliga o rastreio de passagens e cadastra a frequência de downlink.

        Parcial: só os campos enviados mudam.
        """
        from sqlalchemy import text

        if not isinstance(changes, dict) or not changes:
            raise ControlError("envie track_passes e/ou downlink_frequency_hz")
        unknown = set(changes) - {"track_passes", "downlink_frequency_hz"}
        if unknown:
            raise ControlError(f"campo desconhecido: {', '.join(sorted(unknown))}")

        track = changes.get("track_passes")
        if "track_passes" in changes and not isinstance(track, bool):
            raise ControlError("track_passes precisa ser true ou false")

        frequency = changes.get("downlink_frequency_hz")
        if "downlink_frequency_hz" in changes and frequency is not None:
            if isinstance(frequency, bool) or not isinstance(frequency, (int, float)) \
                    or not math.isfinite(frequency):
                raise ControlError("downlink_frequency_hz precisa ser um número, em Hz")
            if not MIN_FREQUENCY_HZ <= frequency <= MAX_FREQUENCY_HZ:
                raise ControlError(
                    f"downlink_frequency_hz fora de 1 MHz a 6 GHz: {frequency:g} Hz "
                    "(a frequência vai em Hz, não em MHz)"
                )

        with self._engine.begin() as conn:
            satellite_id = self._satellite_id(conn, code)
            conn.execute(text(f"""
                INSERT INTO {CONTROL_SCHEMA}.satellite_reception
                    (satellite_id, track_passes, downlink_frequency_hz, updated_at)
                VALUES (:satellite_id, COALESCE(:track, TRUE), :frequency, now())
                ON CONFLICT (satellite_id) DO UPDATE SET
                    track_passes = CASE WHEN :set_track
                                        THEN EXCLUDED.track_passes
                                        ELSE {CONTROL_SCHEMA}.satellite_reception.track_passes END,
                    downlink_frequency_hz = CASE WHEN :set_frequency
                                        THEN EXCLUDED.downlink_frequency_hz
                                        ELSE {CONTROL_SCHEMA}.satellite_reception.downlink_frequency_hz END,
                    updated_at = now()
            """), {
                "satellite_id": satellite_id,
                "track": track,
                "frequency": float(frequency) if frequency is not None else None,
                "set_track": "track_passes" in changes,
                "set_frequency": "downlink_frequency_hz" in changes,
            })

        logger.info("Recepção de %s alterada pelo operador: %s", code, changes)
        return self._reception_of(code)

    def set_decision(self, code: str, aos_iso: str, decision: Optional[str]) -> None:
        """Pular, forçar, ou desfazer a decisão (None) sobre uma passagem."""
        from sqlalchemy import text

        if decision is not None and decision not in DECISIONS:
            raise ControlError("decision precisa ser 'skip', 'force' ou null")
        aos = _parse_aos(aos_iso)

        with self._engine.begin() as conn:
            satellite_id = self._satellite_id(conn, code)
            # Uma decisão por passagem: a nova substitui qualquer anterior que
            # caia na mesma janela de tolerância.
            conn.execute(text(f"""
                DELETE FROM {CONTROL_SCHEMA}.pass_decisions
                WHERE satellite_id = :satellite_id
                  AND abs(extract(epoch FROM predicted_aos - :aos)) <= :tolerance
            """), {"satellite_id": satellite_id, "aos": aos,
                   "tolerance": PASS_MATCH_TOLERANCE.total_seconds()})
            if decision is not None:
                conn.execute(text(f"""
                    INSERT INTO {CONTROL_SCHEMA}.pass_decisions
                        (satellite_id, predicted_aos, decision)
                    VALUES (:satellite_id, :aos, :decision)
                """), {"satellite_id": satellite_id, "aos": aos, "decision": decision})

        logger.info("Passagem de %s (AOS %s): decisão do operador = %s",
                    code, aos.isoformat(), decision or "desfeita")

    # --- Apoio ----------------------------------------------------------------

    def _satellite_id(self, conn, code: str) -> int:
        from sqlalchemy import text

        satellite_id = conn.execute(
            text("SELECT id FROM satellites WHERE code = :code"), {"code": code}
        ).scalar()
        if satellite_id is None:
            raise NotFound(f"satélite {code} não encontrado")
        return satellite_id

    def _reception_of(self, code: str) -> dict[str, Any]:
        return next(r for r in self.predictions()["reception"] if r["code"] == code)


def _parse_aos(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ControlError("aos precisa ser um instante ISO 8601 com fuso")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ControlError(f"aos inválido: {value!r}") from None
    if parsed.tzinfo is None:
        raise ControlError("aos precisa ter fuso (ex.: 2026-09-29T14:03:00Z)")
    return parsed


def _prediction_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "satellite_code": row["satellite_code"],
        "satellite_name": row["satellite_name"],
        "aos_time": _iso(row["aos_time"]),
        "los_time": _iso(row["los_time"]),
        "culmination_time": _iso(row["culmination_time"]),
        "max_elevation_degrees": row["max_elevation_deg"],
        "outcome": row["outcome"],
        "lost_to": (
            {"satellite_name": row["lost_to_satellite_name"],
             "aos_time": _iso(row["lost_to_aos_time"])}
            if row["lost_to_satellite_name"] else None
        ),
        "decision": row["decision"],
        "forced": bool(row["forced"]),
        "carries_telecommands": bool(row["carries_telecommands"]),
        "telecommand_count": row["telecommand_count"],
    }


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None
