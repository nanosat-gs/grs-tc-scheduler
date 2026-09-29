"""API do TC Scheduler: leitura do plano e controle do operador.

Expõe por HTTP o que `station_data` sabe responder sobre o plano da estação,
e as ações do operador sobre passagens (`operator_control`). Existe para que o
painel do GRS Manager não precise de uma conexão com o Postgres: o banco tem
um dono só, e quem quiser lê-lo — ou pedir uma mudança no plano — pergunta a
ele.

As ações do operador são as únicas escritas no banco, e vão só para as tabelas
do próprio Scheduler. Sem autenticação: o compose publica a porta só em
127.0.0.1, e o painel alcança a API pela rede interna.

Os caminhos são os mesmos que o painel servia quando lia o banco direto
(`/api/station`, `/api/satellite/<code>`, `/api/tle/refresh`), para que o
cliente do outro lado seja um repasse de URL e nada da página precise mudar.

Roda numa thread do processo do Scheduler (ver `main.main`), mas nasce
executável sozinho — `python -m tc_scheduler.api` — de propósito: o laço de
replanejamento é SGP4 em Python puro, CPU-bound, e segura a GIL. Se a latência
durante um replan incomodar, a saída é subir este módulo num container próprio,
sem tocar em código.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)


def create_app(station_data: Any, control: Any = None) -> Flask:
    """Monta a aplicação em cima de um `station_data` já construído.

    Recebe as dependências prontas em vez de construí-las: é o que permite
    testar a API inteira contra dublês, sem Postgres nenhum. Sem `control`, as
    rotas do operador respondem 503 e a leitura segue funcionando.
    """
    from tc_scheduler.operator_control import ControlError, NotFound

    app = Flask(__name__)

    def control_call(action):
        """Traduz o resultado de uma ação do operador em resposta HTTP.

        400 para pedido inválido (a mensagem vai para a tela como está), 404
        para satélite inexistente, 503 para banco fora — três coisas que o
        painel mostra de jeitos diferentes.
        """
        if control is None:
            return jsonify({"error": "controle de passagens indisponível"}), 503
        try:
            return jsonify(action())
        except ControlError as error:
            return jsonify({"error": str(error)}), 400
        except NotFound as error:
            return jsonify({"error": str(error)}), 404
        except Exception as error:
            logger.warning("Ação do operador falhou: %s", error)
            return jsonify({"error": "banco indisponível"}), 503

    @app.get("/api/passes")
    def passes():
        """As passagens previstas, escolhidas ou não, com a decisão do operador."""
        return control_call(control.predictions if control else None)

    @app.put("/api/satellites/<code>/reception")
    def reception(code: str):
        """Liga/desliga o rastreio de passagens e cadastra a frequência de downlink."""
        body = request.get_json(silent=True)
        return control_call(lambda: control.set_reception(code, body))

    @app.put("/api/passes/decision")
    def decision():
        """Pular, forçar ou desfazer (null) a decisão sobre uma passagem."""
        body = request.get_json(silent=True)

        def act():
            if not isinstance(body, dict):
                raise ControlError("esperava um objeto JSON")
            for field in ("satellite_code", "aos", "decision"):
                if field not in body:
                    raise ControlError(f"falta o campo {field}")
            control.set_decision(body["satellite_code"], body["aos"], body["decision"])
            return {"ok": True}

        return control_call(act)

    @app.get("/health")
    def health():
        """No ar, e com banco? São duas perguntas.

        A API responder já diz que o processo está vivo; `database_available`
        distingue disso o caso em que ela subiu mas o Postgres não veio junto.
        """
        return jsonify({"ok": True, "database_available": station_data.is_available()})

    @app.get("/api/station")
    def station():
        """Satélites e próximas passagens.

        200 mesmo com o banco fora do ar: `database_available: false` é um
        estado a mostrar na tela, e não um erro do cliente que perguntou.
        """
        return jsonify(station_data.snapshot())

    @app.get("/api/satellite/<code>")
    def satellite(code: str):
        """404 só para satélite que não existe.

        A distinção importa do outro lado: o cliente traduz 404 em "não
        encontrado" e qualquer outra falha em "sem acesso". Confundir os dois
        faria um código digitado errado aparecer como banco indisponível.
        """
        detail = station_data.satellite_detail(code)
        if detail is None:
            return jsonify({"error": f"satélite {code} não encontrado"}), 404
        return jsonify(detail)

    @app.post("/api/tle/refresh")
    def refresh_tle():
        """Revalida os elementos orbitais no CelesTrak, sem esperar o cache expirar.

        POST porque muda estado (o cache de TLE) — um GET aqui seria disparado
        por qualquer prefetch de navegador. Escreve no cache, não no banco.
        """
        return jsonify(station_data.refresh_orbital_data())

    return app


def main() -> None:
    """Sobe só a API, sem o laço de planejamento.

    Modo de escape para quando o replan segurando a GIL atrapalhar: um serviço
    a mais no compose, mesma imagem, mesmo repositório.
    """
    import logging as _logging

    from tc_scheduler import config, db, station_data as station_data_module
    from tc_scheduler.operator_control import OperatorControl

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    data = station_data_module.from_environment()
    if data is None:
        raise SystemExit("PG_DATABASE_URL não definida: a API não tem o que servir.")

    engine = db.build_engine(config.DATABASE_URL)
    db.ensure_control_schema(engine)
    control = OperatorControl(engine)

    logger.info("API em http://%s:%d", config.API_HOST, config.API_PORT)
    try:
        create_app(data, control).run(
            host=config.API_HOST, port=config.API_PORT,
            debug=False, use_reloader=False, threaded=True,
        )
    finally:
        data.close()


if __name__ == "__main__":
    main()
