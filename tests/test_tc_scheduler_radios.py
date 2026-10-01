"""O cadastro de rádios do lado do Scheduler: só informa o painel.

A regra tem de ser a MESMA do Station Manager (mgm8.domain.models.route_downlinks),
que é quem de fato roteia na passagem: em ordem, o primeiro downlink ligado de
cada faixa fica com o rádio.
"""

import pytest

from tc_scheduler.radios import annotate, parse_radios

RADIOS = parse_radios("vhf=143000000-148000000,uhf=462000000-470000000")


def d(name, mhz, enabled=True):
    return {"name": name, "frequency_hz": mhz * 1e6, "enabled": enabled}


def test_parse_le_o_formato_do_compose():
    assert [(r.name, r.min_hz, r.max_hz) for r in RADIOS] == [
        ("vhf", 143e6, 148e6), ("uhf", 462e6, 470e6)]
    assert parse_radios("") == []


@pytest.mark.parametrize("text", ["vhf", "vhf=148-143", "vhf=a-b"])
def test_parse_recusa_texto_mal_escrito(text):
    with pytest.raises(ValueError):
        parse_radios(text)


def test_cada_downlink_ganha_o_radio_da_sua_faixa():
    out = annotate([d("beacon", 145.9), d("dados", 468.4)], RADIOS)

    assert [(x["radio"], x["issue"]) for x in out] == [("vhf", None), ("uhf", None)]


def test_desligado_fora_de_faixa_e_faixa_ocupada_dizem_por_que():
    out = annotate([d("beacon", 145.9), d("off", 145.9, enabled=False),
                    d("amador", 436.1), d("beacon-2", 145.8)], RADIOS)

    issues = {x["name"]: x["issue"] for x in out}
    assert issues["beacon"] is None
    assert issues["off"] == "desligado"
    assert "nenhum rádio" in issues["amador"]
    assert "já ouve beacon" in issues["beacon-2"]


def test_sem_radios_so_o_primeiro_ligado_e_ouvido():
    out = annotate([d("off", 145.9, enabled=False), d("beacon", 145.9), d("dados", 468.4)], [])

    assert [x["issue"] for x in out][1] is None
    assert "só o primeiro" in out[2]["issue"]
