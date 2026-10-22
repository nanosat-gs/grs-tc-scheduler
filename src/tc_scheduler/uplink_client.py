"""Cliente da :5593 do grs-uplink: "transmita o telecomando N".

REQ/REP com prazo, e o socket descartado no timeout (o REQ que perdeu uma
resposta fica travado) — o mesmo cuidado do StationManagerClient. O prazo
precisa cobrir a duração da rajada e a espera do rádio pelo BURST_ACK.
"""

from __future__ import annotations

import json
from typing import Any

import zmq

from tc_scheduler.tc_lifecycle import UplinkUnavailable


class UplinkClient:
    def __init__(self, address: str, timeout_ms: int, context: zmq.Context | None = None) -> None:
        self._address = address
        self._timeout_ms = timeout_ms
        self._context = context or zmq.Context.instance()
        self._socket: zmq.Socket | None = None

    def _connect(self) -> zmq.Socket:
        if self._socket is None:
            socket = self._context.socket(zmq.REQ)
            socket.setsockopt(zmq.LINGER, 0)
            socket.connect(self._address)
            self._socket = socket
        return self._socket

    def _discard(self) -> None:
        if self._socket is not None:
            self._socket.close(linger=0)
            self._socket = None

    def transmit(self, telecommand_id: int, allow_no_doppler: bool = False) -> dict[str, Any]:
        socket = self._connect()
        request = {"op": "transmit", "telecommand_id": telecommand_id}
        if allow_no_doppler:
            request["allow_no_doppler"] = True
        try:
            socket.send(json.dumps(request).encode())
            if not socket.poll(self._timeout_ms):
                self._discard()
                raise UplinkUnavailable(f"o grs-uplink em {self._address} não respondeu "
                                        f"em {self._timeout_ms / 1000:.0f} s")
            reply = json.loads(socket.recv())
        except zmq.ZMQError as error:
            self._discard()
            raise UplinkUnavailable(f"ZMQ: {error}") from error
        except ValueError as error:
            raise UplinkUnavailable(f"resposta ilegível do grs-uplink: {error}") from error
        if not isinstance(reply, dict):
            raise UplinkUnavailable("resposta do grs-uplink não é um objeto")
        return reply

    def close(self) -> None:
        self._discard()
