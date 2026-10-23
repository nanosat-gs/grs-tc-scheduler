"""A thread do ciclo de vida: ritmo próprio, passagem ativa lida a cada tick, falha contida."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from tc_scheduler.lifecycle_runner import LifecycleRunner

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


class FakeLifecycle:
    def __init__(self, error: Exception | None = None, delay_s: float = 0.0) -> None:
        self.calls: list[tuple[datetime, object]] = []
        self.error = error
        self.delay_s = delay_s

    def tick(self, now, active_pass):
        self.calls.append((now, active_pass))
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.error is not None:
            raise self.error


def test_cada_tick_le_a_passagem_ativa_e_o_relogio():
    passes = iter([None, {"id": 3, "satellite_id": 7}])
    lifecycle = FakeLifecycle()
    runner = LifecycleRunner(lifecycle, lambda: next(passes), 1.0, clock=lambda: T0)

    runner.tick_once()
    runner.tick_once()

    assert lifecycle.calls == [(T0, None), (T0, {"id": 3, "satellite_id": 7})]


def test_falha_nao_derruba_a_thread_e_loga_uma_vez(caplog):
    lifecycle = FakeLifecycle(error=RuntimeError("grs-uplink fora"))
    runner = LifecycleRunner(lifecycle, lambda: None, 1.0, clock=lambda: T0)

    with caplog.at_level(logging.WARNING, logger="tc_scheduler.lifecycle_runner"):
        for _ in range(5):
            runner.tick_once()

    assert len(lifecycle.calls) == 5
    assert [r.message for r in caplog.records].count(
        "Ciclo de vida do TC falhou (RuntimeError: grs-uplink fora); tentando no próximo tick.") == 1


def test_a_leitura_da_passagem_que_falha_tambem_fica_contida():
    def broken():
        raise ConnectionError("banco fora")

    runner = LifecycleRunner(FakeLifecycle(), broken, 1.0)
    runner.tick_once()                                  # não levanta


def test_uplink_lento_nao_segura_quem_esta_fora_da_thread():
    """O motivo da thread: um pedido ao uplink que demora (rádio sem
    responder) não pode atrasar a ativação de passagens no laço principal."""
    lifecycle = FakeLifecycle(delay_s=0.5)
    runner = LifecycleRunner(lifecycle, lambda: None, 0.01)
    runner.start()
    try:
        started = time.monotonic()
        main_loop_ticks = 0
        while time.monotonic() - started < 0.3:      # o "laço principal" segue
            main_loop_ticks += 1
            time.sleep(0.01)
        assert main_loop_ticks > 10
        assert threading.current_thread() is not runner
    finally:
        runner.stop()
        runner.join(timeout=2)
    assert not runner.is_alive() and lifecycle.calls


def test_parar_acorda_a_espera_entre_ticks():
    runner = LifecycleRunner(FakeLifecycle(), lambda: None, 60.0)
    runner.start()
    time.sleep(0.05)

    started = time.monotonic()
    runner.stop()
    runner.join(timeout=2)

    assert not runner.is_alive() and time.monotonic() - started < 1.0
