"""O ciclo de vida do telecomando: manda, espera a resposta, casa, transita.

O TC Scheduler é o ÚNICO que escreve `telecommands.status` (ver
uplink-pares-e-estados-fs2.md §3.6). Os outros módulos gravam fatos:

- o `grs-uplink` grava cada tentativa em `uplink_transmissions` (com o `ack`
  do rádio, a prova N1 de que a rajada saiu);
- o `grs-telemetry-decoder` grava cada quadro recebido em `decoded_frames`
  (e o `0x13` decodificado em `fs2_tc_feedback`).

Este módulo lê os dois e aplica as transições:

    pending/queued --(ack do rádio)--> sent
    sent --(resposta casada, conteúdo decodificado, sucesso)--> confirmed
    sent --(resposta casada, 0x13 com erro)--> failed
    sent --(resposta casada, conteúdo NÃO decodificado)--> response_received
    sent --(prazo vencido sem resposta)--> no_response
    response_received/no_response --(a mesma resposta decodificada depois)--> confirmed/failed

**Um TC por vez por satélite (stop-and-wait).** O próximo só sobe depois da
resposta do anterior, ou do prazo. É o que torna o casamento inequívoco sem
número de sequência no protocolo, e o que o buffer de 5 posições do TTC2
exige (§4.1).

**Casar** uma resposta com uma transmissão: o callsign de quem pediu (bytes
8–14 da resposta) é o da estação, o tipo da resposta é o esperado, e — no
`0x13` — o ID do TC ecoado (byte 15) é o que subiu. Só o cabeçalho é
necessário: um tipo ainda sem decoder já casa, como `response_received`.

Este arquivo é a lógica pura (casar, decidir) e o orquestrador. O SQL está em
`lifecycle_store.py`; o pedido ao uplink, em `uplink_client.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)

PING_ANSWER = 0x02
TC_FEEDBACK = 0x13
RESPONSE_PKT_IDS = (PING_ANSWER, TC_FEEDBACK)

# Por qual rádio cada resposta desce (process_tc.c v1.0): o pong pelo TTC_1,
# o feedback pelo TTC_0. Serve à evidência do `no_response`.
RESPONSE_RADIO = {PING_ANSWER: "vhf", TC_FEEDBACK: "uhf"}

# ERRNO_FB_* do satellite.h do OBDH2.
FEEDBACK_MESSAGES = {
    0: "sucesso",
    -1: "timeout do mission manager",
    -2: "falha de comunicação com o subsistema",
    -3: "falha de comunicação com o dispositivo",
    -4: "falha ao gravar na FRAM",
    -5: "falha ao agendar o TC",
}

# Estados em que um TC espera (ou ainda aceita) uma resposta.
AWAITING = ("sent", "response_received", "no_response")


def normalize_callsign(raw: bytes | str) -> str:
    text = raw.decode("latin-1") if isinstance(raw, (bytes, bytearray)) else raw
    return text.strip(" \x00")


# --- lógica pura ---------------------------------------------------------------


@dataclass(frozen=True)
class ResponseFrame:
    """Uma linha de decoded_frames que é resposta a telecomando."""

    decoded_frame_id: int
    raw_packet_id: int
    received_at: datetime
    radio: Optional[str]
    status: str                 # o status do decoder: ok, not_implemented, layout_mismatch...
    pkt_id: int
    payload: bytes              # o pacote FSat (ngham_payload)
    detail: Optional[str] = None
    error_code: Optional[int] = None   # de fs2_tc_feedback, quando decodificado

    @property
    def requester_callsign(self) -> str:
        return normalize_callsign(self.payload[8:15])

    @property
    def echoed_tc_id(self) -> Optional[int]:
        return self.payload[15] if self.pkt_id == TC_FEEDBACK and len(self.payload) > 15 else None


@dataclass(frozen=True)
class Transmission:
    """Uma linha de uplink_transmissions com `ack`, mais o status do TC."""

    transmission_id: int
    telecommand_id: int
    satellite_id: int
    tc_pkt_id: int
    expected_response_pkt_id: int
    station_callsign: str
    ended_at: datetime
    telecommand_status: str


def match_rule(frame: ResponseFrame, transmission: Transmission) -> Optional[str]:
    """A regra que casou, ou None. A regra vai para tc_responses (auditoria)."""
    if frame.pkt_id != transmission.expected_response_pkt_id:
        return None
    if len(frame.payload) < 15:
        return None
    if frame.requester_callsign != normalize_callsign(transmission.station_callsign):
        return None
    if frame.pkt_id == TC_FEEDBACK:
        if frame.echoed_tc_id != transmission.tc_pkt_id:
            return None
        return "callsign+tc_id"
    return "callsign"


@dataclass(frozen=True)
class Decision:
    status: str
    message: str


def decide(frame: ResponseFrame) -> Decision:
    """O que a resposta casada diz do telecomando."""
    if frame.status != "ok":
        why = frame.detail or frame.status
        return Decision("response_received",
                        f"resposta 0x{frame.pkt_id:02X} recebida, conteúdo não decodificado ({why})")
    if frame.pkt_id == TC_FEEDBACK:
        code = frame.error_code
        if code is None and len(frame.payload) >= 22:
            code = int.from_bytes(frame.payload[20:22], "big", signed=True)
        if code is None:
            return Decision("response_received", "TC Feedback sem código de erro legível")
        text = FEEDBACK_MESSAGES.get(code, f"código {code} desconhecido")
        if code == 0:
            return Decision("confirmed", "TC Feedback: sucesso")
        return Decision("failed", f"TC Feedback {code}: {text}")
    return Decision("confirmed", f"resposta 0x{frame.pkt_id:02X} recebida")


def pick_transmission(frame: ResponseFrame, candidates: list[Transmission],
                      late_window: timedelta,
                      clock_skew: timedelta = timedelta(seconds=2)) -> Optional[tuple[Transmission, str]]:
    """A transmissão mais recente, antes da resposta, que casa com ela."""
    eligible = [
        t for t in candidates
        if t.telecommand_status in AWAITING
        and t.ended_at - clock_skew <= frame.received_at <= t.ended_at + late_window
    ]
    for transmission in sorted(eligible, key=lambda t: t.ended_at, reverse=True):
        rule = match_rule(frame, transmission)
        if rule is not None:
            return transmission, rule
    return None


# --- orquestrador ------------------------------------------------------------------


class LifecycleStore(Protocol):
    def frames_after(self, cursor: int, limit: int) -> list[ResponseFrame]: ...
    def cursor(self) -> int: ...
    def save_cursor(self, cursor: int) -> None: ...
    def candidate_transmissions(self, frame: ResponseFrame, late_window: timedelta) -> list[Transmission]: ...
    def record_response(self, frame: ResponseFrame, transmission: Transmission, rule: str,
                        decision: Decision) -> bool: ...
    def apply_response(self, transmission: Transmission, frame: ResponseFrame,
                       decision: Decision, rule: str) -> bool: ...
    def expired(self, now: datetime, timeouts: dict[int, timedelta]) -> list[Transmission]: ...
    def mark_no_response(self, transmission: Transmission, now: datetime, timeout: timedelta) -> bool: ...
    def open_sessions(self, now: datetime) -> dict[int, bool]: ...
    def awaiting(self, satellite_id: int) -> bool: ...
    def next_telecommand(self, satellite_id: int, pass_id: Optional[int]) -> Optional[int]: ...
    def mark_sent(self, telecommand_id: int, reply: dict[str, Any], now: datetime) -> bool: ...
    def mark_rejected(self, telecommand_id: int, detail: str) -> bool: ...
    def log_tx_failure(self, telecommand_id: int, reply: dict[str, Any]) -> None: ...


class UplinkUnavailable(RuntimeError):
    pass


class Uplink(Protocol):
    def transmit(self, telecommand_id: int, allow_no_doppler: bool = False) -> dict[str, Any]: ...


@dataclass
class LifecycleSettings:
    response_timeouts: dict[int, timedelta]
    late_window: timedelta = timedelta(minutes=10)
    retry_after: timedelta = timedelta(seconds=15)
    batch: int = 200


@dataclass
class TickReport:
    matched: int = 0
    unmatched: int = 0
    expired: int = 0
    sent: list[int] = field(default_factory=list)
    rejected: list[int] = field(default_factory=list)
    tx_failed: list[int] = field(default_factory=list)


class Lifecycle:
    def __init__(self, store: LifecycleStore, uplink: Uplink, settings: LifecycleSettings) -> None:
        self._store = store
        self._uplink = uplink
        self._settings = settings
        # Satélite -> quando pode tentar de novo depois de uma falha de rádio.
        # Em memória de propósito: um restart do Scheduler pode tentar de novo.
        self._retry_at: dict[int, datetime] = {}

    def tick(self, now: datetime, active_pass: Optional[dict[str, Any]]) -> TickReport:
        report = TickReport()
        self._correlate(report)
        self._expire(now, report)
        self._send(now, active_pass, report)
        return report

    # 1. as respostas novas -------------------------------------------------------

    def _correlate(self, report: TickReport) -> None:
        cursor = self._store.cursor()
        frames = self._store.frames_after(cursor, self._settings.batch)
        for frame in frames:
            found = pick_transmission(
                frame, self._store.candidate_transmissions(frame, self._settings.late_window),
                self._settings.late_window)
            if found is None:
                # Resposta a outra estação, a um TC já fechado, ou duplicada.
                report.unmatched += 1
                continue
            transmission, rule = found
            decision = decide(frame)
            self._store.record_response(frame, transmission, rule, decision)
            if self._store.apply_response(transmission, frame, decision, rule):
                report.matched += 1
                logger.info("TC %d: %s (%s, quadro %d).", transmission.telecommand_id,
                            decision.status, decision.message, frame.decoded_frame_id)
        if frames:
            self._store.save_cursor(frames[-1].decoded_frame_id)

    # 2. prazos ------------------------------------------------------------------------

    def _expire(self, now: datetime, report: TickReport) -> None:
        timeouts = self._settings.response_timeouts
        for transmission in self._store.expired(now, timeouts):
            timeout = timeouts[transmission.expected_response_pkt_id]
            if self._store.mark_no_response(transmission, now, timeout):
                report.expired += 1
                logger.warning("TC %d sem resposta em %.0f s: no_response.",
                               transmission.telecommand_id, timeout.total_seconds())

    # 3. o próximo TC --------------------------------------------------------------------

    def _send(self, now: datetime, active_pass: Optional[dict[str, Any]], report: TickReport) -> None:
        # Satélite -> (passagem, liberação sem Doppler). Na passagem: só os
        # `queued` dela, e o Doppler é obrigatório. Na sessão manual: `pending`
        # e `queued`, e o Doppler só deixa de ser obrigatório se o operador
        # liberou ao abrir a sessão. A sessão manual vale por cima da passagem.
        sessions: dict[int, tuple[Optional[int], bool]] = {}
        if active_pass is not None:
            sessions[active_pass["satellite_id"]] = (active_pass["id"], False)
        for satellite_id, allow_no_doppler in self._store.open_sessions(now).items():
            sessions[satellite_id] = (None, allow_no_doppler)

        for satellite_id, (pass_id, allow_no_doppler) in sessions.items():
            retry_at = self._retry_at.get(satellite_id)
            if retry_at is not None and now < retry_at:
                continue
            if self._store.awaiting(satellite_id):
                continue          # stop-and-wait: um TC por vez
            telecommand_id = self._store.next_telecommand(satellite_id, pass_id)
            if telecommand_id is None:
                continue
            self._transmit(satellite_id, telecommand_id, now, report, allow_no_doppler)

    def _transmit(self, satellite_id: int, telecommand_id: int, now: datetime,
                  report: TickReport, allow_no_doppler: bool = False) -> None:
        try:
            reply = self._uplink.transmit(telecommand_id, allow_no_doppler=allow_no_doppler)
        except UplinkUnavailable as error:
            self._retry_at[satellite_id] = now + self._settings.retry_after
            logger.warning("Uplink indisponível para o TC %d: %s", telecommand_id, error)
            report.tx_failed.append(telecommand_id)
            return

        if not reply.get("ok"):
            if reply.get("error") == "rejected":
                self._store.mark_rejected(telecommand_id, reply.get("detail") or "")
                report.rejected.append(telecommand_id)
            else:
                self._retry_at[satellite_id] = now + self._settings.retry_after
                self._store.log_tx_failure(telecommand_id, reply)
                report.tx_failed.append(telecommand_id)
            return

        if reply.get("status") == "ack":
            self._retry_at.pop(satellite_id, None)
            if self._store.mark_sent(telecommand_id, reply, now):
                report.sent.append(telecommand_id)
                logger.info("TC %d no ar (tentativa %s, %.0f Hz).", telecommand_id,
                            reply.get("transmission_id"), reply.get("tx_freq_hz") or 0)
            return

        # O rádio recusou ou falhou: o TC fica na fila e tenta de novo depois.
        self._retry_at[satellite_id] = now + self._settings.retry_after
        self._store.log_tx_failure(telecommand_id, reply)
        report.tx_failed.append(telecommand_id)
        logger.warning("TC %d não saiu: %s (%s).", telecommand_id, reply.get("status"),
                       reply.get("detail"))
