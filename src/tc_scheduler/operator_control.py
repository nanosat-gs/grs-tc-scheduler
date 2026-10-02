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
from tc_scheduler.radios import Radio, annotate

logger = logging.getLogger(__name__)

DECISIONS = ("skip", "force")

# Faixa do USRP N210 (DC a 6 GHz, conforme a daughterboard). Fora disso é
# erro de unidade — MHz digitado onde se esperava Hz, quase sempre.
MIN_FREQUENCY_HZ = 1e6
MAX_FREQUENCY_HZ = 6e9

# Um satélite com mais portadoras que isto é quase certamente cadastro errado.
MAX_DOWNLINKS = 8
MAX_DOWNLINK_NAME = 40


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
           COALESCE(r.track_passes, TRUE) AS track_passes
    FROM satellites s
    LEFT JOIN {CONTROL_SCHEMA}.satellite_reception r ON r.satellite_id = s.id
    ORDER BY s.name
"""

_DOWNLINKS_SQL = f"""
    SELECT s.code, d.name, d.frequency_hz, d.enabled, d.measured_offset_hz, d.measured_at
    FROM {CONTROL_SCHEMA}.satellite_downlinks d
    JOIN satellites s ON s.id = d.satellite_id
    ORDER BY s.code, d.position, d.id
"""


class OperatorControl:
    def __init__(self, engine, radios: Optional[list[Radio]] = None) -> None:
        self._engine = engine
        self._radios = list(radios or [])

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
            downlinks: dict[str, list[dict]] = {}
            for row in conn.execute(text(_DOWNLINKS_SQL)).mappings():
                downlinks.setdefault(row["code"], []).append({
                    "name": row["name"], "frequency_hz": row["frequency_hz"],
                    "enabled": bool(row["enabled"]),
                    "measured_offset_hz": row["measured_offset_hz"],
                    "measured_at": _iso(row["measured_at"]),
                })

        computed_at = max((p["computed_at"] for p in passes), default=None)
        return {
            "computed_at": _iso(computed_at),
            "passes": [_prediction_to_dict(p) for p in passes],
            "radios": [radio.to_dict() for radio in self._radios],
            "reception": [
                {
                    "code": r["code"], "name": r["name"], "status": r["status"],
                    "track_passes": bool(r["track_passes"]),
                    "downlinks": annotate(downlinks.get(r["code"], []), self._radios),
                }
                for r in reception
            ],
        }

    # --- Escrita --------------------------------------------------------------

    def set_reception(self, code: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Liga/desliga o rastreio de passagens para recepção."""
        from sqlalchemy import text

        if not isinstance(changes, dict) or not changes:
            raise ControlError("envie track_passes")
        if "downlink_frequency_hz" in changes:
            raise ControlError("a frequência agora é por downlink: "
                               f"PUT /api/satellites/{code}/downlinks")
        unknown = set(changes) - {"track_passes"}
        if unknown:
            raise ControlError(f"campo desconhecido: {', '.join(sorted(unknown))}")

        track = changes.get("track_passes")
        if not isinstance(track, bool):
            raise ControlError("track_passes precisa ser true ou false")

        with self._engine.begin() as conn:
            satellite_id = self._satellite_id(conn, code)
            conn.execute(text(f"""
                INSERT INTO {CONTROL_SCHEMA}.satellite_reception
                    (satellite_id, track_passes, updated_at)
                VALUES (:satellite_id, :track, now())
                ON CONFLICT (satellite_id) DO UPDATE SET
                    track_passes = EXCLUDED.track_passes,
                    updated_at = now()
            """), {"satellite_id": satellite_id, "track": track})

        logger.info("Recepção de %s alterada pelo operador: %s", code, changes)
        return self._reception_of(code)

    def set_downlinks(self, code: str, downlinks: Any) -> dict[str, Any]:
        """Substitui a lista de downlinks do satélite, inteira, numa transação.

        A lista inteira, e não um item por vez: a ORDEM importa (o primeiro
        ligado é a referência do Doppler, e numa faixa com dois fica o
        primeiro), e uma edição parcial deixaria a ordem a cargo de quem
        chegou por último.
        """
        from sqlalchemy import text

        cleaned = _validate_downlinks(downlinks)

        with self._engine.begin() as conn:
            satellite_id = self._satellite_id(conn, code)
            # O desvio medido continua valendo para o downlink que ficou com o
            # mesmo nome e a mesma frequência; para quem mudou de frequência
            # (inclusive porque o operador aplicou a correção), ele não vale mais.
            measured = {
                (row["name"], row["frequency_hz"]): (row["measured_offset_hz"], row["measured_at"])
                for row in conn.execute(text(f"""
                    SELECT name, frequency_hz, measured_offset_hz, measured_at
                    FROM {CONTROL_SCHEMA}.satellite_downlinks WHERE satellite_id = :id
                """), {"id": satellite_id}).mappings()
            }
            conn.execute(text(f"""
                DELETE FROM {CONTROL_SCHEMA}.satellite_downlinks WHERE satellite_id = :id
            """), {"id": satellite_id})
            for position, downlink in enumerate(cleaned):
                offset, at = measured.get((downlink["name"], downlink["frequency_hz"]), (None, None))
                conn.execute(text(f"""
                    INSERT INTO {CONTROL_SCHEMA}.satellite_downlinks
                        (satellite_id, name, frequency_hz, enabled, position,
                         measured_offset_hz, measured_at)
                    VALUES (:satellite_id, :name, :frequency_hz, :enabled, :position,
                            :measured_offset_hz, :measured_at)
                """), {"satellite_id": satellite_id, "position": position,
                       "measured_offset_hz": offset, "measured_at": at, **downlink})

        logger.info("Downlinks de %s alterados pelo operador: %s", code,
                    ", ".join(f"{d['name']} {d['frequency_hz'] / 1e6:.4f} MHz" for d in cleaned)
                    or "nenhum")
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


def _validate_downlinks(downlinks: Any) -> list[dict[str, Any]]:
    if not isinstance(downlinks, list):
        raise ControlError("downlinks precisa ser uma lista")
    if len(downlinks) > MAX_DOWNLINKS:
        raise ControlError(f"no máximo {MAX_DOWNLINKS} downlinks por satélite")

    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(downlinks, start=1):
        if not isinstance(item, dict):
            raise ControlError(f"downlink {index}: esperava um objeto")
        unknown = set(item) - {"name", "frequency_hz", "enabled"}
        if unknown:
            raise ControlError(f"downlink {index}: campo desconhecido {', '.join(sorted(unknown))}")

        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ControlError(f"downlink {index}: dê um nome (ex.: beacon, dados)")
        name = name.strip()
        if len(name) > MAX_DOWNLINK_NAME:
            raise ControlError(f"downlink {index}: nome com mais de {MAX_DOWNLINK_NAME} caracteres")
        if name.lower() in seen:
            raise ControlError(f"downlink {name} repetido")
        seen.add(name.lower())

        frequency = item.get("frequency_hz")
        if isinstance(frequency, bool) or not isinstance(frequency, (int, float)) \
                or not math.isfinite(frequency):
            raise ControlError(f"{name}: frequency_hz precisa ser um número, em Hz")
        if not MIN_FREQUENCY_HZ <= frequency <= MAX_FREQUENCY_HZ:
            raise ControlError(
                f"{name}: frequency_hz fora de 1 MHz a 6 GHz: {frequency:g} Hz "
                "(a frequência vai em Hz, não em MHz)"
            )

        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ControlError(f"{name}: enabled precisa ser true ou false")

        cleaned.append({"name": name, "frequency_hz": float(frequency), "enabled": enabled})
    return cleaned
