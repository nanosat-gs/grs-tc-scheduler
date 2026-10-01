"""Testes da API de leitura do TC Scheduler.

O que importa aqui é o contrato com o painel: os caminhos, os códigos de
status e o formato das respostas. O conteúdo em si é `station_data`, testado
à parte — por isso o dublê.

O teste que mais paga é o do 404: ele é a diferença entre "esse satélite não
existe" e "não deu para perguntar", e o cliente do outro lado depende dela.
"""

import pytest

from tc_scheduler.api import create_app


class FakeStationData:
    """Substitui o acesso ao banco: os testes são sobre a API, não sobre SQL."""

    def __init__(self, snapshot=None, detail=None, refresh=None, available=True):
        self._snapshot = snapshot or {"satellites": [], "database_available": True}
        self._detail = detail
        self._refresh = refresh or {"database_available": True, "results": [], "updated": 0}
        self._available = available
        self.refresh_calls = 0

    def snapshot(self):
        return self._snapshot

    def satellite_detail(self, code):
        if self._detail is None or self._detail.get("code") != code:
            return None
        return self._detail

    def refresh_orbital_data(self):
        self.refresh_calls += 1
        return self._refresh

    def is_available(self):
        return self._available


@pytest.fixture
def client_for():
    def build(station_data):
        app = create_app(station_data)
        app.config.update(TESTING=True)
        return app.test_client()

    return build


def test_health_reporta_banco_disponivel(client_for):
    response = client_for(FakeStationData(available=True)).get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "database_available": True}


def test_health_responde_mesmo_com_banco_fora(client_for):
    """A API estar de pé e o banco responder são fatos separados: o compose
    precisa poder distinguir "ainda subindo" de "subiu sem o Postgres"."""
    response = client_for(FakeStationData(available=False)).get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "database_available": False}


def test_station_devolve_o_snapshot(client_for):
    snapshot = {"satellites": [{"code": "SAT-001"}], "database_available": True}

    response = client_for(FakeStationData(snapshot=snapshot)).get("/api/station")

    assert response.status_code == 200
    assert response.get_json() == snapshot


def test_station_responde_200_com_banco_fora(client_for):
    """Banco fora do ar é um estado a mostrar na tela, não erro do cliente."""
    snapshot = {"satellites": [], "database_available": False}

    response = client_for(FakeStationData(snapshot=snapshot)).get("/api/station")

    assert response.status_code == 200
    assert response.get_json()["database_available"] is False


def test_satellite_devolve_o_detalhe(client_for):
    detail = {"code": "SAT-001", "name": "Alfa", "database_available": True}

    response = client_for(FakeStationData(detail=detail)).get("/api/satellite/SAT-001")

    assert response.status_code == 200
    assert response.get_json() == detail


def test_satellite_inexistente_e_404(client_for):
    """404 e não 200-com-nulo: é o que deixa o cliente distinguir "não existe"
    de "sem acesso" sem inspecionar o corpo."""
    response = client_for(FakeStationData()).get("/api/satellite/NAO-EXISTE")

    assert response.status_code == 404
    assert "error" in response.get_json()


def test_refresh_chama_a_revalidacao_e_so_por_post(client_for):
    station_data = FakeStationData(refresh={"database_available": True, "results": [], "updated": 3})
    client = client_for(station_data)

    assert client.get("/api/tle/refresh").status_code == 405
    assert station_data.refresh_calls == 0

    response = client.post("/api/tle/refresh")

    assert response.status_code == 200
    assert response.get_json()["updated"] == 3
    assert station_data.refresh_calls == 1


# --- Controle do operador -------------------------------------------------------

from tc_scheduler.operator_control import ControlError, NotFound  # noqa: E402


class FakeControl:
    """Dublê do OperatorControl: registra as chamadas e imita os erros reais."""

    def __init__(self, fail_with=None):
        self.calls = []
        self._fail_with = fail_with

    def predictions(self):
        if self._fail_with:
            raise self._fail_with
        return {"computed_at": None, "passes": [{"satellite_code": "SAT-1"}], "reception": []}

    def set_reception(self, code, changes):
        self.calls.append(("reception", code, changes))
        if code == "NAO-EXISTE":
            raise NotFound(f"satélite {code} não encontrado")
        if changes == {"track_passes": "sim"}:
            raise ControlError("track_passes precisa ser true ou false")
        return {"code": code, "track_passes": True, "downlinks": []}

    def set_downlinks(self, code, downlinks):
        self.calls.append(("downlinks", code, downlinks))
        if downlinks == "ruim":
            raise ControlError("downlinks precisa ser uma lista")
        return {"code": code, "track_passes": True, "downlinks": downlinks}

    def set_decision(self, code, aos, decision):
        self.calls.append(("decision", code, aos, decision))


@pytest.fixture
def control_client():
    def build(control):
        app = create_app(FakeStationData(), control)
        app.config.update(TESTING=True)
        return app.test_client()

    return build


def test_passes_devolve_a_previsao(control_client):
    response = control_client(FakeControl()).get("/api/passes")

    assert response.status_code == 200
    assert response.get_json()["passes"] == [{"satellite_code": "SAT-1"}]


def test_sem_controle_as_rotas_do_operador_dao_503_e_a_leitura_segue(client_for):
    client = client_for(FakeStationData())

    assert client.get("/api/passes").status_code == 503
    assert client.put("/api/passes/decision", json={}).status_code == 503
    assert client.get("/api/station").status_code == 200


def test_banco_fora_vira_503_e_nao_500(control_client):
    response = control_client(FakeControl(fail_with=RuntimeError("conexão recusada"))).get("/api/passes")

    assert response.status_code == 503
    assert response.get_json() == {"error": "banco indisponível"}


def test_recepcao_repassa_os_campos(control_client):
    control = FakeControl()
    response = control_client(control).put(
        "/api/satellites/SAT-1/reception",
        json={"track_passes": False},
    )

    assert response.status_code == 200
    assert control.calls == [("reception", "SAT-1", {"track_passes": False})]


def test_recepcao_invalida_e_400_com_a_mensagem(control_client):
    response = control_client(FakeControl()).put(
        "/api/satellites/SAT-1/reception", json={"track_passes": "sim"})

    assert response.status_code == 400
    assert "track_passes" in response.get_json()["error"]


def test_recepcao_de_satelite_inexistente_e_404(control_client):
    response = control_client(FakeControl()).put(
        "/api/satellites/NAO-EXISTE/reception", json={"track_passes": True})

    assert response.status_code == 404


def test_decisao_repassa_satelite_aos_e_decisao(control_client):
    control = FakeControl()
    response = control_client(control).put("/api/passes/decision", json={
        "satellite_code": "SAT-1", "aos": "2026-09-29T14:03:00Z", "decision": "skip"})

    assert response.status_code == 200
    assert control.calls == [("decision", "SAT-1", "2026-09-29T14:03:00Z", "skip")]


def test_desfazer_decisao_e_decision_null(control_client):
    control = FakeControl()
    control_client(control).put("/api/passes/decision", json={
        "satellite_code": "SAT-1", "aos": "2026-09-29T14:03:00Z", "decision": None})

    assert control.calls[0][3] is None


@pytest.mark.parametrize("body", [None, [], {"aos": "x", "decision": "skip"}])
def test_decisao_sem_os_campos_e_400(control_client, body):
    response = control_client(FakeControl()).put("/api/passes/decision", json=body)

    assert response.status_code == 400


def test_downlinks_repassa_a_lista(control_client):
    control = FakeControl()
    lista = [{"name": "beacon", "frequency_hz": 145_900_000}]

    response = control_client(control).put("/api/satellites/SAT-1/downlinks",
                                           json={"downlinks": lista})

    assert response.status_code == 200
    assert control.calls == [("downlinks", "SAT-1", lista)]


@pytest.mark.parametrize("body", [None, [], {"lista": []}])
def test_downlinks_sem_a_chave_e_400(control_client, body):
    control = FakeControl()

    response = control_client(control).put("/api/satellites/SAT-1/downlinks", json=body)

    assert response.status_code == 400
    assert control.calls == []


def test_downlinks_invalidos_viram_400_com_a_mensagem(control_client):
    response = control_client(FakeControl()).put("/api/satellites/SAT-1/downlinks",
                                                 json={"downlinks": "ruim"})

    assert response.status_code == 400
    assert "lista" in response.get_json()["error"]
