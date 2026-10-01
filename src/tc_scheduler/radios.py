"""Os rádios da estação, para dizer ao operador que rádio ouve cada downlink.

Quem DECIDE o roteamento na passagem é o Station Manager (`--radios`, mesmo
formato, mesma variável `STATION_RADIOS` no compose). Aqui a lista serve só
para o painel avisar no cadastro: "nenhum rádio cobre 436 MHz", "o rádio vhf
já ouve o beacon". A regra é a mesma do Station Manager — em ordem, o primeiro
downlink ligado de cada faixa fica com o rádio — e tem de continuar a mesma.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Radio:
    name: str
    min_hz: float
    max_hz: float

    def covers(self, frequency_hz: float) -> bool:
        return self.min_hz <= frequency_hz <= self.max_hz

    def to_dict(self) -> dict:
        return {"name": self.name, "min_hz": self.min_hz, "max_hz": self.max_hz}


def parse_radios(text: str) -> list[Radio]:
    """`vhf=143000000-148000000,uhf=462000000-470000000`. Vazio = nenhum."""
    radios: list[Radio] = []
    for item in filter(None, (part.strip() for part in (text or "").split(","))):
        try:
            name, band = item.split("=", 1)
            low, high = (float(value) for value in band.split("-", 1))
        except ValueError:
            raise ValueError(f"rádio mal escrito {item!r}: esperava nome=min-max em Hz") from None
        if not 0 < low < high:
            raise ValueError(f"rádio {name.strip()}: faixa inválida")
        radios.append(Radio(name.strip(), low, high))
    return radios


def annotate(downlinks: list[dict], radios: list[Radio]) -> list[dict]:
    """Acrescenta a cada downlink `radio` (quem o ouve) e `issue` (por que
    ninguém ouve). Sem rádios declarados, a estação ouve só o primeiro
    downlink ligado, num canal único."""
    taken: dict[str, str] = {}
    first_enabled = True
    out = []
    for downlink in downlinks:
        item = {**downlink, "radio": None, "issue": None}
        if not downlink.get("enabled", True):
            item["issue"] = "desligado"
        elif not radios:
            if first_enabled:
                first_enabled = False
            else:
                item["issue"] = "a estação não declara rádios: só o primeiro downlink é ouvido"
        else:
            radio = next((r for r in radios if r.covers(downlink["frequency_hz"])), None)
            if radio is None:
                item["issue"] = "nenhum rádio da estação cobre essa frequência"
            elif radio.name in taken:
                item["issue"] = f"o rádio {radio.name} já ouve {taken[radio.name]}"
            else:
                taken[radio.name] = downlink["name"]
                item["radio"] = radio.name
        out.append(item)
    return out
