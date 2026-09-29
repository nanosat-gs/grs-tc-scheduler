"""Escolha de quais passagens a estação vai usar.

É aqui que mora a autonomia: em vez de um operador decidir na tela do gpredict
qual satélite acompanhar, o Scheduler prevê todas as passagens visíveis no
horizonte e escolhe. Toda passagem é candidata — o satélite transmite
telemetria sem esperar uplink —, e o telecomando pesa na disputa, mas não é
condição para existir passagem.

A estação tem um rotor só, então duas passagens que se sobrepõem no tempo são
mutuamente exclusivas — a escolha é um problema de seleção de intervalos com
peso. A heurística gulosa (maior pontuação primeiro, descartando o que colide
com algo já comprometido) não é ótima no caso geral, mas passagens de LEO são
curtas e esparsas: colisões são raras, e quando acontecem a diferença para o
ótimo é de uma passagem que volta a aparecer na próxima órbita.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PassCandidate:
    """Uma passagem prevista, com o contexto que decide se vale usá-la."""

    satellite_id: int
    satellite_name: str
    satellite_pass: object          # spacelab_tracking.SatellitePass
    pending_telecommands: int
    max_priority: int
    tle_epoch: object               # datetime do epoch dos elementos usados
    data_source: str                # 'omm' ou 'tle'
    # Decisões do operador para esta passagem específica.
    forced: bool = False
    skipped: bool = False
    # Se esta é a passagem que leva os telecomandos do satélite. Só uma por
    # satélite leva: a fila inteira sai na primeira passagem escolhida, então
    # dar o peso do telecomando a todas as passagens do satélite faria as
    # seguintes vencerem conflitos em nome de comandos que não vão nelas.
    carries_telecommands: bool = True

    @property
    def score(self) -> tuple:
        """Ordem de preferência entre passagens que competem pelo mesmo rotor.

        0. Forçada pelo operador: decisão explícita vence qualquer cálculo.
        1. Prioridade do telecomando mais urgente à espera — é o que o operador
           efetivamente pediu na fila.
        2. Quantos comandos saem nesta passagem: em empate, atender mais.
        3. Elevação máxima: passagem mais alta rende link melhor e mais longo.
           É o único critério entre passagens só de recepção.
        """
        if self.carries_telecommands:
            priority, pending = self.max_priority, self.pending_telecommands
        else:
            priority, pending = 0, 0
        return (
            self.forced,
            priority,
            pending,
            self.satellite_pass.max_elevation_deg,
        )

    def overlaps(self, other: "PassCandidate") -> bool:
        return (
            self.satellite_pass.aos_time < other.satellite_pass.los_time
            and other.satellite_pass.aos_time < self.satellite_pass.los_time
        )


def is_worth_tracking(
    satellite_pass,
    min_duration_seconds: float,
    min_max_elevation_deg: float,
) -> bool:
    """Descarta passagens rasantes ou curtas demais.

    Movimentar a antena custa tempo e desgaste; uma passagem de dois minutos a
    três graus de elevação raramente rende link utilizável o bastante para
    justificar isso.
    """
    return (
        satellite_pass.duration_seconds >= min_duration_seconds
        and satellite_pass.max_elevation_deg >= min_max_elevation_deg
    )


@dataclass(frozen=True)
class PassOutcome:
    """O que o planejamento decidiu sobre uma candidata.

    `planned`, `conflict` (perdeu o rotor para `lost_to`) ou `skipped` (o
    operador pulou). Guardar as perdedoras, e não só as escolhidas, é o que
    deixa o operador ver o que a estação NÃO vai rastrear e por quê.
    """

    candidate: PassCandidate
    outcome: str
    lost_to: Optional[PassCandidate] = None


def plan_passes(candidates: Iterable[PassCandidate]) -> list[PassOutcome]:
    """Decide cada candidata, em ordem cronológica.

    Gulosa: maior pontuação primeiro, descartando o que colide com algo já
    comprometido. Puladas pelo operador não disputam.
    """
    candidates = list(candidates)
    committed: list[PassCandidate] = []
    outcomes: list[PassOutcome] = []

    for candidate in candidates:
        if candidate.skipped:
            outcomes.append(PassOutcome(candidate, "skipped"))

    for candidate in sorted((c for c in candidates if not c.skipped),
                            key=lambda c: c.score, reverse=True):
        conflict = next((c for c in committed if candidate.overlaps(c)), None)
        if conflict is not None:
            logger.info(
                "Passagem de %s (AOS %s) descartada: colide com %s (AOS %s), de "
                "pontuação maior.",
                candidate.satellite_name, candidate.satellite_pass.aos_time.isoformat(),
                conflict.satellite_name, conflict.satellite_pass.aos_time.isoformat(),
            )
            outcomes.append(PassOutcome(candidate, "conflict", conflict))
            continue
        committed.append(candidate)
        outcomes.append(PassOutcome(candidate, "planned"))

    return sorted(outcomes, key=lambda o: o.candidate.satellite_pass.aos_time)


def select_passes(candidates: Iterable[PassCandidate]) -> list[PassCandidate]:
    """Só as escolhidas, em ordem cronológica — é assim que o plano é gravado."""
    return [o.candidate for o in plan_passes(candidates) if o.outcome == "planned"]


def mark_telecommand_carriers(candidates: list[PassCandidate]) -> list[PassCandidate]:
    """Marca, por satélite, a passagem que leva os telecomandos.

    É a primeira passagem aproveitável e não pulada de cada satélite (entrada
    em qualquer ordem). As demais disputam o rotor como recepção. Se a
    primeira perder um conflito, os comandos não se perdem: no replanejamento
    seguinte a próxima passagem vira a primeira — e isso acontece bem antes de
    ela chegar, porque se replaneja a cada poucos minutos.
    """
    first_by_satellite: dict[int, PassCandidate] = {}
    for candidate in sorted(candidates, key=lambda c: c.satellite_pass.aos_time):
        if candidate.skipped:
            continue
        first_by_satellite.setdefault(candidate.satellite_id, candidate)

    carriers = {id(c) for c in first_by_satellite.values()}
    return [
        replace(c, carries_telecommands=id(c) in carriers and c.pending_telecommands > 0)
        for c in candidates
    ]
