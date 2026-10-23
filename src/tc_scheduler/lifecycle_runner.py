"""O ciclo de vida do telecomando numa thread própria.

Antes ele rodava no fim de cada tick do laço principal, e o pedido ao
grs-uplink espera a rajada sair e o rádio confirmar: até
UPLINK_REQUEST_TIMEOUT_MS (20 s) com o rádio sem responder. Nesse tempo o
laço não ativava passagem nenhuma — um AOS podia esperar o uplink de outra.

Aqui ele tem o próprio ritmo e o próprio engine (como a API: pools separados,
nenhum objeto mutável cruzando as threads). O laço principal só ativa e
encerra passagens; esta thread lê do banco qual está ativa. O um-TC-por-vez
continua valendo: casar, vencer prazos e enviar seguem em sequência, numa
thread só.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from tc_scheduler.tc_lifecycle import Lifecycle

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LifecycleRunner(threading.Thread):
    def __init__(self, lifecycle: Lifecycle, active_pass: Callable[[], Optional[dict[str, Any]]],
                 tick_seconds: float, clock: Callable[[], datetime] = _now) -> None:
        """:param active_pass: a passagem ativa agora (db.fetch_active_pass), ou None."""
        super().__init__(daemon=True, name="tc-lifecycle")
        self._lifecycle = lifecycle
        self._active_pass = active_pass
        self._tick_seconds = tick_seconds
        self._clock = clock
        # Não `_stop`: é o nome de um método interno de threading.Thread, e
        # sobrescrevê-lo quebra o join() ("'Event' object is not callable").
        self._stopping = threading.Event()
        self._error: Optional[str] = None
        self._error_at = 0.0

    def stop(self) -> None:
        self._stopping.set()

    def run(self) -> None:
        logger.info("Ciclo de vida do TC numa thread própria, a cada %.0f s.", self._tick_seconds)
        while not self._stopping.is_set():
            self.tick_once()
            self._stopping.wait(self._tick_seconds)

    def tick_once(self) -> None:
        """Um tick. O ciclo de vida não pode derrubar a thread: com o banco, o
        decoder ou o grs-uplink fora do ar, a mesma falha se repetiria a cada
        tick, então loga uma vez por minuto, e quando muda."""
        try:
            self._lifecycle.tick(self._clock(), self._active_pass())
            if self._error is not None:
                logger.info("Ciclo de vida do TC voltou a funcionar.")
                self._error = None
        except Exception as error:
            message = f"{type(error).__name__}: {error}".splitlines()[0][:300]
            if message != self._error or time.monotonic() - self._error_at > 60:
                logger.warning("Ciclo de vida do TC falhou (%s); tentando no próximo tick.", message)
                self._error, self._error_at = message, time.monotonic()
