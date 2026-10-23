"""O ciclo de vida do telecomando: casar resposta com transmissão, decidir, e o stop-and-wait.

Lógica pura e orquestrador, sem banco nem rede: o store e o uplink são
dublês com só o que o tc_lifecycle consulta. O SQL do store real é exercitado
no ponta a ponta da estação (grs-station/tests/test_uplink_loop_*).
"""

from __future__ import annotations

import struct
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tc_scheduler.tc_lifecycle import (
    PING_ANSWER,
    TC_FEEDBACK,
    Decision,
    Lifecycle,
    LifecycleSettings,
    ResponseFrame,
    Transmission,
    UplinkUnavailable,
    decide,
    match_rule,
    pick_transmission,
)

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
STATION = b" PY0SIM"


def pong(requester: bytes = STATION) -> bytes:
    return b"\x02 PY0EFS" + requester


def feedback(tc_id: int = 0x45, code: int = 0, requester: bytes = STATION) -> bytes:
    return b"\x13 PY0EFS" + requester + struct.pack(">BIh", tc_id, 1_791_201_600, code)


def frame(payload: bytes, *, id: int = 10, at: datetime = T0 + timedelta(seconds=3),
          status: str = "ok", error_code: int | None = None, detail: str | None = None) -> ResponseFrame:
    return ResponseFrame(decoded_frame_id=id, raw_packet_id=100 + id, received_at=at,
                         radio="uhf" if payload[0] == TC_FEEDBACK else "vhf", status=status,
                         pkt_id=payload[0], payload=payload, detail=detail, error_code=error_code)


def transmission(*, tc: int = 1, tc_pkt_id: int = 0x45, response: int = TC_FEEDBACK,
                 ended: datetime = T0, status: str = "sent", sat: int = 7) -> Transmission:
    return Transmission(transmission_id=50 + tc, telecommand_id=tc, satellite_id=sat,
                        tc_pkt_id=tc_pkt_id, expected_response_pkt_id=response,
                        station_callsign="PY0SIM", ended_at=ended, telecommand_status=status)


# --- casar ---------------------------------------------------------------------------


def test_feedback_casa_por_callsign_e_id_do_tc():
    assert match_rule(frame(feedback(0x45)), transmission()) == "callsign+tc_id"


def test_pong_casa_por_callsign():
    assert match_rule(frame(pong()), transmission(tc_pkt_id=0x40, response=PING_ANSWER)) == "callsign"


@pytest.mark.parametrize("payload, tx", [
    (feedback(0x46), transmission()),                                   # outro TC
    (feedback(0x45, requester=b"PP5ABCD"), transmission()),             # outra estação
    (pong(), transmission()),                                           # esperava 0x13
    (b"\x02 PY0EFS PY0", transmission(tc_pkt_id=0x40, response=PING_ANSWER)),   # curto
])
def test_nao_casa(payload, tx):
    assert match_rule(frame(payload), tx) is None


def test_escolhe_a_transmissao_mais_recente_antes_da_resposta():
    older = transmission(tc=1, ended=T0 - timedelta(seconds=40))
    newer = transmission(tc=2, ended=T0)

    picked = pick_transmission(frame(feedback()), [older, newer], timedelta(minutes=10))

    assert picked == (newer, "callsign+tc_id")


def test_resposta_antes_da_transmissao_ou_fora_da_janela_nao_casa():
    late = timedelta(minutes=10)
    assert pick_transmission(frame(feedback(), at=T0 - timedelta(seconds=10)), [transmission()], late) is None
    assert pick_transmission(frame(feedback(), at=T0 + timedelta(minutes=11)), [transmission()], late) is None


def test_tc_ja_fechado_nao_recebe_resposta():
    closed = transmission(status="confirmed")
    assert pick_transmission(frame(feedback()), [closed], timedelta(minutes=10)) is None


def test_no_response_ainda_aceita_resposta_atrasada():
    late = transmission(status="no_response")
    assert pick_transmission(frame(feedback()), [late], timedelta(minutes=10)) is not None


# --- decidir -------------------------------------------------------------------------


def test_feedback_zero_e_confirmado():
    assert decide(frame(feedback(code=0), error_code=0)).status == "confirmed"


def test_feedback_com_erro_e_falha_com_o_motivo():
    decision = decide(frame(feedback(code=-2), error_code=-2))

    assert decision == Decision("failed", "TC Feedback -2: falha de comunicação com o subsistema")


def test_codigo_lido_do_pacote_se_a_tabela_do_tipo_nao_veio():
    assert decide(frame(feedback(code=-5))).message.startswith("TC Feedback -5")


def test_pong_e_confirmado():
    assert decide(frame(pong())).status == "confirmed"


def test_quadro_nao_decodificado_e_response_received_com_o_motivo():
    decision = decide(frame(feedback(), status="not_implemented"))

    assert decision.status == "response_received"
    assert "not_implemented" in decision.message


# --- o orquestrador ---------------------------------------------------------------------


class FakeStore:
    def __init__(self) -> None:
        self.frames: list[ResponseFrame] = []
        self.transmissions: list[Transmission] = []
        self.status: dict[int, str] = {}           # telecommand_id -> status
        self.satellite_of: dict[int, int] = {}
        self.priority: dict[int, int] = {}
        self.pass_of: dict[int, int | None] = {}
        self.sessions: list[int] = []
        self.no_doppler_sessions: set[int] = set()   # sessões abertas com allow_no_doppler
        self.saved_cursor = 0
        self.responses: list[tuple[int, int, str]] = []
        self.logs: list[tuple[int, str]] = []

    def add_tc(self, tc: int, *, sat: int = 7, status: str = "pending", priority: int = 5,
               pass_id: int | None = None) -> None:
        self.status[tc], self.satellite_of[tc] = status, sat
        self.priority[tc], self.pass_of[tc] = priority, pass_id

    def cursor(self):
        return self.saved_cursor

    def save_cursor(self, cursor):
        self.saved_cursor = cursor

    def frames_after(self, cursor, limit):
        return [f for f in self.frames if f.decoded_frame_id > cursor][:limit]

    def candidate_transmissions(self, frame, late_window):
        return [replace(t, telecommand_status=self.status[t.telecommand_id]) for t in self.transmissions]

    def record_response(self, frame, transmission, rule, decision):
        self.responses.append((frame.decoded_frame_id, transmission.telecommand_id, decision.status))
        return True

    def apply_response(self, transmission, frame, decision, rule):
        allowed = ("sent", "response_received", "no_response") \
            if decision.status in ("confirmed", "failed") else ("sent", "no_response")
        if self.status[transmission.telecommand_id] not in allowed:
            return False
        self.status[transmission.telecommand_id] = decision.status
        return True

    def expired(self, now, timeouts):
        latest = {}
        for t in self.transmissions:
            latest[t.telecommand_id] = t
        return [t for t in latest.values() if self.status[t.telecommand_id] == "sent"
                and t.ended_at + timeouts[t.expected_response_pkt_id] < now]

    def mark_no_response(self, transmission, now, timeout):
        if self.status[transmission.telecommand_id] != "sent":
            return False
        self.status[transmission.telecommand_id] = "no_response"
        return True

    def open_sessions(self, now):
        return {sat: sat in self.no_doppler_sessions for sat in self.sessions}

    def awaiting(self, satellite_id):
        return any(s == "sent" and self.satellite_of[tc] == satellite_id for tc, s in self.status.items())

    def next_telecommand(self, satellite_id, pass_id):
        eligible = [tc for tc, s in self.status.items() if self.satellite_of[tc] == satellite_id and (
            (s == "queued" and self.pass_of[tc] == pass_id) if pass_id is not None
            else s in ("pending", "queued"))]
        return max(eligible, key=lambda tc: (self.priority[tc], -tc), default=None)

    def mark_sent(self, telecommand_id, reply, now):
        self.status[telecommand_id] = "sent"
        self.transmissions.append(transmission(
            tc=telecommand_id, tc_pkt_id=reply["tc_pkt_id"],
            response=reply["expected_response_pkt_id"], ended=now))
        return True

    def mark_rejected(self, telecommand_id, detail):
        self.status[telecommand_id] = "failed"
        return True

    def log_tx_failure(self, telecommand_id, reply):
        self.logs.append((telecommand_id, reply.get("status") or reply.get("error")))


class FakeUplink:
    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.calls: list[int] = []
        self.allowed: list[bool] = []

    def transmit(self, telecommand_id, allow_no_doppler=False):
        self.calls.append(telecommand_id)
        self.allowed.append(allow_no_doppler)
        reply = self.replies.pop(0) if self.replies else ack()
        if isinstance(reply, Exception):
            raise reply
        return reply


def ack(tc_pkt_id=0x45, response=TC_FEEDBACK):
    return {"ok": True, "transmission_id": 1, "status": "ack", "tc_pkt_id": tc_pkt_id,
            "expected_response_pkt_id": response, "tx_freq_hz": 145.9e6, "doppler_hz": None}


SETTINGS = LifecycleSettings(response_timeouts={PING_ANSWER: timedelta(seconds=15),
                                                TC_FEEDBACK: timedelta(seconds=30)})


def lifecycle(store, uplink):
    return Lifecycle(store, uplink, SETTINGS)


def test_sessao_manual_transmite_e_resposta_confirma():
    store, uplink = FakeStore(), FakeUplink()
    store.add_tc(1)
    store.sessions = [7]
    loop = lifecycle(store, uplink)

    assert loop.tick(T0, None).sent == [1]
    assert store.status[1] == "sent"

    store.frames.append(frame(feedback(0x45), error_code=0))
    report = loop.tick(T0 + timedelta(seconds=4), None)

    assert report.matched == 1 and store.status[1] == "confirmed"
    assert store.saved_cursor == 10


def test_passagem_rastreada_nunca_libera_transmitir_sem_doppler():
    store, uplink = FakeStore(), FakeUplink()
    store.add_tc(1, status="queued", pass_id=3)

    lifecycle(store, uplink).tick(T0, {"id": 3, "satellite_id": 7})

    assert uplink.calls == [1] and uplink.allowed == [False]


def test_sessao_manual_so_libera_sem_doppler_se_o_operador_liberou():
    store, uplink = FakeStore(), FakeUplink()
    store.add_tc(1, sat=7)
    store.add_tc(2, sat=8)
    store.sessions = [7, 8]
    store.no_doppler_sessions = {8}

    lifecycle(store, uplink).tick(T0, None)

    assert dict(zip(uplink.calls, uplink.allowed)) == {1: False, 2: True}


def test_sessao_de_outro_satelite_espera_a_passagem_em_andamento():
    """A antena e o Doppler anunciado são do satélite da passagem: a sessão de
    outro satélite, mesmo liberada sem Doppler, só sobe depois do LOS."""
    store, uplink = FakeStore(), FakeUplink()
    store.add_tc(1, sat=7, status="queued", pass_id=3)
    store.add_tc(2, sat=8)
    store.sessions = [8]
    store.no_doppler_sessions = {8}
    loop = lifecycle(store, uplink)

    loop.tick(T0, {"id": 3, "satellite_id": 7})
    assert uplink.calls == [1]                          # só o da passagem

    loop.tick(T0 + timedelta(seconds=40), None)         # depois do LOS
    assert uplink.calls == [1, 2] and uplink.allowed == [False, True]


def test_sessao_do_mesmo_satelite_da_passagem_usa_o_doppler_dela():
    store, uplink = FakeStore(), FakeUplink()
    store.add_tc(1, sat=7)
    store.sessions = [7]

    lifecycle(store, uplink).tick(T0, {"id": 3, "satellite_id": 7})

    assert uplink.calls == [1] and uplink.allowed == [False]


def test_sem_doppler_o_tc_fica_na_fila_e_tenta_de_novo():
    """O Doppler pode voltar (o Station Manager reiniciou, a passagem começou):
    a recusa é falha de rádio, não `failed`."""
    store = FakeStore()
    store.add_tc(1)
    store.sessions = [7]
    uplink = FakeUplink({"ok": False, "error": "no_doppler", "detail": "sem Doppler anunciado"})
    loop = lifecycle(store, uplink)

    report = loop.tick(T0, None)
    assert report.tx_failed == [1] and store.status[1] == "pending"
    assert store.logs == [(1, "no_doppler")]

    loop.tick(T0 + timedelta(seconds=5), None)
    assert uplink.calls == [1]                          # espera o prazo de nova tentativa
    loop.tick(T0 + timedelta(seconds=16), None)
    assert uplink.calls == [1, 1] and store.status[1] == "sent"


def test_um_tc_por_vez_o_proximo_so_depois_da_resposta():
    store, uplink = FakeStore(), FakeUplink()
    store.add_tc(1, priority=9)
    store.add_tc(2, priority=5)
    store.sessions = [7]
    loop = lifecycle(store, uplink)

    loop.tick(T0, None)
    loop.tick(T0 + timedelta(seconds=2), None)
    assert uplink.calls == [1]                          # o 2 espera

    store.frames.append(frame(feedback(0x45), error_code=0))
    loop.tick(T0 + timedelta(seconds=4), None)
    assert uplink.calls == [1, 2]


def test_sem_resposta_vira_no_response_e_libera_o_proximo():
    store, uplink = FakeStore(), FakeUplink()
    store.add_tc(1)
    store.add_tc(2)
    store.sessions = [7]
    loop = lifecycle(store, uplink)

    loop.tick(T0, None)
    report = loop.tick(T0 + timedelta(seconds=31), None)

    assert report.expired == 1 and store.status[1] == "no_response"
    assert uplink.calls == [1, 2]


def test_resposta_atrasada_depois_do_no_response_confirma():
    store, uplink = FakeStore(), FakeUplink()
    store.add_tc(1)
    store.sessions = [7]
    loop = lifecycle(store, uplink)
    loop.tick(T0, None)
    loop.tick(T0 + timedelta(seconds=31), None)

    store.frames.append(frame(feedback(), at=T0 + timedelta(seconds=40), error_code=0))
    loop.tick(T0 + timedelta(seconds=41), None)

    assert store.status[1] == "confirmed"


def test_response_received_avanca_quando_o_quadro_e_reprocessado():
    store, uplink = FakeStore(), FakeUplink()
    store.add_tc(1)
    store.sessions = [7]
    loop = lifecycle(store, uplink)
    loop.tick(T0, None)

    store.frames.append(frame(feedback(), id=10, status="layout_mismatch", detail="tc_feedback com 21 bytes"))
    loop.tick(T0 + timedelta(seconds=4), None)
    assert store.status[1] == "response_received"

    store.frames.append(frame(feedback(), id=11, error_code=0))      # nova versão do decoder
    loop.tick(T0 + timedelta(seconds=60), None)
    assert store.status[1] == "confirmed"


def test_resposta_a_outra_estacao_nao_casa():
    store, uplink = FakeStore(), FakeUplink()
    store.add_tc(1)
    store.sessions = [7]
    loop = lifecycle(store, uplink)
    loop.tick(T0, None)

    store.frames.append(frame(feedback(requester=b"PP5ABCD"), error_code=0))
    report = loop.tick(T0 + timedelta(seconds=4), None)

    assert report.unmatched == 1 and store.status[1] == "sent"


def test_feedback_com_erro_falha():
    store, uplink = FakeStore(), FakeUplink()
    store.add_tc(1)
    store.sessions = [7]
    loop = lifecycle(store, uplink)
    loop.tick(T0, None)

    store.frames.append(frame(feedback(code=-2), error_code=-2))
    loop.tick(T0 + timedelta(seconds=4), None)

    assert store.status[1] == "failed"


def test_rejeitado_no_solo_falha_sem_esperar():
    store = FakeStore()
    store.add_tc(1)
    store.add_tc(2)
    store.sessions = [7]
    uplink = FakeUplink({"ok": False, "error": "rejected", "detail": "module_id 9 desconhecido"})

    report = lifecycle(store, uplink).tick(T0, None)

    assert report.rejected == [1] and store.status[1] == "failed"


@pytest.mark.parametrize("reply", [
    {"ok": True, "transmission_id": 1, "status": "tx_disabled", "detail": "TX desligado"},
    UplinkUnavailable("sem resposta"),
])
def test_radio_que_nao_transmite_deixa_o_tc_na_fila_e_espera_antes_de_tentar(reply):
    store, uplink = FakeStore(), FakeUplink(reply)
    store.add_tc(1)
    store.sessions = [7]
    loop = lifecycle(store, uplink)

    loop.tick(T0, None)
    assert store.status[1] == "pending"
    loop.tick(T0 + timedelta(seconds=5), None)
    assert uplink.calls == [1]                            # ainda no intervalo
    loop.tick(T0 + timedelta(seconds=16), None)
    assert uplink.calls == [1, 1] and store.status[1] == "sent"


def test_passagem_transmite_so_os_queued_dela():
    store, uplink = FakeStore(), FakeUplink()
    store.add_tc(1, status="queued", pass_id=99)
    store.add_tc(2, status="pending")
    store.add_tc(3, status="queued", pass_id=100)

    lifecycle(store, uplink).tick(T0, {"id": 99, "satellite_id": 7})

    assert uplink.calls == [1]


def test_sem_sessao_nem_passagem_nada_sobe():
    store, uplink = FakeStore(), FakeUplink()
    store.add_tc(1)

    lifecycle(store, uplink).tick(T0, None)

    assert uplink.calls == []
