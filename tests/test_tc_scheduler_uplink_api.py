"""As rotas de uplink da API: sessão manual e o que subiu.

O contrato com quem chama (operador, teste ponta a ponta): caminhos, códigos
de status, e o 503 claro quando o uplink está desligado. O SQL do
UplinkControl é exercitado no ponta a ponta da estação.
"""
import pytest

from tc_scheduler.api import create_app
from tc_scheduler.operator_control import ControlError, NotFound


class FakeStationData:
    def snapshot(self):
        return {"satellites": [], "database_available": True}

    def is_available(self):
        return True


class FakeUplinkControl:
    def __init__(self):
        self.opened = []
        self.closed = []

    def open_session(self, body):
        if not isinstance(body, dict) or "satellite" not in body:
            raise ControlError("satellite: informe o código ou o id do satélite")
        if body["satellite"] == "NADA":
            raise NotFound("satélite 'NADA' não existe")
        self.opened.append(body)
        return {"session": {"id": 1, "satellite_id": 7}}

    def close_session(self, session_id):
        self.closed.append(session_id)
        return {"closed": session_id}

    def overview(self):
        return {"sessions": [], "transmissions": []}


@pytest.fixture
def client():
    uplink = FakeUplinkControl()
    app = create_app(FakeStationData(), None, uplink)
    app.config.update(TESTING=True)
    return app.test_client(), uplink


def test_abre_sessao(client):
    http, uplink = client

    response = http.post("/api/uplink/session", json={"satellite": "FS2", "duration_s": 300})

    assert response.status_code == 200
    assert response.get_json() == {"session": {"id": 1, "satellite_id": 7}}
    assert uplink.opened == [{"satellite": "FS2", "duration_s": 300}]


@pytest.mark.parametrize("body, status", [({"duration_s": 60}, 400), ({"satellite": "NADA"}, 404)])
def test_sessao_invalida(client, body, status):
    http, _ = client
    assert http.post("/api/uplink/session", json=body).status_code == status


def test_fecha_sessao_e_visao_geral(client):
    http, uplink = client

    assert http.delete("/api/uplink/session/5").get_json() == {"closed": 5}
    assert http.get("/api/uplink").get_json() == {"sessions": [], "transmissions": []}
    assert uplink.closed == [5]


def test_uplink_desligado_responde_503_e_o_resto_segue():
    app = create_app(FakeStationData())
    app.config.update(TESTING=True)
    http = app.test_client()

    assert http.post("/api/uplink/session", json={"satellite": "FS2"}).status_code == 503
    assert http.get("/api/uplink").status_code == 503
    assert http.get("/health").status_code == 200
