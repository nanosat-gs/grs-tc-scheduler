"""TC Scheduler: decide o que a estação rastreia, e quando.

Fecha o circuito que estava aberto entre os dois lados da estação: o operador
cria telecomandos na interface web, que ficavam parados no banco sem ninguém
consumir; e o controle de rotor sabia apontar, mas dependia de um humano no
gpredict para dizer para onde.

O laço aqui é deliberadamente lento e sem tempo real. Quem aponta o rotor é o
Station Manager, que recebe uma ordem por passagem e a conduz até o LOS — assim
um replanejamento pesado (propagar 24h de N satélites) nunca atrasa o
apontamento, e o rotor sobrevive a um restart deste processo.

Cada tarefa do ciclo tem o seu próprio intervalo, num laço só: nada aqui
compete por tempo, então concorrência entre as tarefas do plano só traria
dificuldade de depuração. A única thread do processo é a da API de leitura
(`tc_scheduler.api`), que não compartilha estado com o laço — ela abre o
próprio engine e só lê.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from spacelab_tracking import (
    build_satellite,
    from_tle_lines,
    get_orbital_data,
    get_tracking_info,
    predict_passes,
    satellite_epoch,
)

from tc_scheduler import config, db, schema_check
from tc_scheduler.planner import (
    PassCandidate,
    is_worth_tracking,
    mark_telecommand_carriers,
    plan_passes,
)
from tc_scheduler.station_manager import (
    StationManagerClient,
    StationManagerError,
    StationManagerUnavailable,
)

logger = logging.getLogger("tc_scheduler")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_orbital_data(satellite: dict):
    """Dados orbitais de um satélite, com o TLE manual tendo precedência.

    Um TLE preenchido à mão é uma decisão explícita de operador — sobrepor isso
    com o catálogo público anularia o motivo de tê-lo preenchido.
    """
    if satellite.get("tle_line1") and satellite.get("tle_line2"):
        return from_tle_lines(
            satellite["tle_line1"], satellite["tle_line2"], satellite["name"]
        )
    return get_orbital_data(satellite["norad_id"])


def _prediction_row(candidate: PassCandidate, outcome: str,
                    lost_to: Optional[PassCandidate], telecommand_count: int) -> dict:
    satellite_pass = candidate.satellite_pass
    return {
        "satellite_id": candidate.satellite_id,
        "aos_time": satellite_pass.aos_time,
        "los_time": satellite_pass.los_time,
        "culmination_time": getattr(satellite_pass, "culmination_time", None),
        "max_elevation_deg": satellite_pass.max_elevation_deg,
        "outcome": outcome,
        "lost_to_satellite_id": lost_to.satellite_id if lost_to else None,
        "lost_to_aos_time": lost_to.satellite_pass.aos_time if lost_to else None,
        "forced": candidate.forced,
        "carries_telecommands": candidate.carries_telecommands,
        "telecommand_count": telecommand_count,
    }


def find_decision(decisions: list[dict], satellite_id: int, aos: datetime) -> Optional[str]:
    """A decisão do operador para esta passagem, se houver.

    Casada por satélite + AOS previsto dentro da tolerância, e não pelo id da
    linha: o plano é apagado e recriado a cada ciclo. Havendo mais de uma, vale
    a de AOS mais próximo.
    """
    matches = [
        d for d in decisions
        if d["satellite_id"] == satellite_id
        and abs(d["predicted_aos"] - aos) <= db.PASS_MATCH_TOLERANCE
    ]
    if not matches:
        return None
    return min(matches, key=lambda d: abs(d["predicted_aos"] - aos))["decision"]


class Scheduler:
    def __init__(self, engine, station: StationManagerClient) -> None:
        self._engine = engine
        self._station = station
        self._last_replan = 0.0
        self._last_tracking_poll = 0.0
        self._last_control_check = 0.0
        self._control_version: Optional[tuple] = None
        self._running = True

    def stop(self, *_: object) -> None:
        logger.info("Encerrando...")
        self._running = False

    # --- Planejamento -------------------------------------------------------

    def replan(self) -> None:
        """Recalcula o plano: prevê passagens, escolhe e compromete comandos.

        Toda passagem aproveitável de satélite ativo é candidata, tenha
        telecomando ou não — o satélite transmite telemetria sem esperar
        uplink. Satélite com a recepção desligada pelo operador volta ao
        comportamento antigo: só a passagem que leva telecomando.
        """
        started = time.perf_counter()
        now = now_utc()

        with self._engine.begin() as conn:
            satellites = db.fetch_candidate_satellites(conn)
            db.prune_pass_decisions(conn, before=now - timedelta(hours=6))
            decisions = db.fetch_pass_decisions(conn, since=now - timedelta(hours=6))

        if not satellites:
            logger.info(
                "Nada a planejar: nenhum satélite ativo com dado orbital. Preencha "
                "satellites.norad_id para habilitar o agendamento automático."
            )

        candidates: list[PassCandidate] = []
        reception_off: list[tuple] = []   # (satélite, passagem): só para mostrar
        for satellite in satellites:
            try:
                orbital_data = load_orbital_data(satellite.__dict__)
                satrec = build_satellite(orbital_data)
                passes = predict_passes(
                    satrec,
                    satellite.name,
                    station=config.GROUND_STATION,
                    search_window_hours=config.HORIZON_HOURS,
                )
            except Exception as error:
                # Um satélite com TLE ruim não pode impedir o planejamento dos
                # outros — a estação continua operando com o que tem.
                logger.exception("Falha ao prever passagens de %s: %s", satellite.name, error)
                continue

            usable = [
                p for p in passes
                if is_worth_tracking(
                    p, config.MIN_PASS_DURATION_SECONDS, config.MIN_PASS_MAX_ELEVATION_DEG
                )
            ]
            logger.info(
                "%s: %d passagens em %.0fh, %d aproveitáveis (%d telecomandos, "
                "prioridade máx. %d, recepção %s)",
                satellite.name, len(passes), config.HORIZON_HOURS, len(usable),
                satellite.pending_telecommands, satellite.max_priority,
                "ligada" if satellite.track_passes else "desligada",
            )

            epoch = satellite_epoch(satrec)
            satellite_candidates = [
                PassCandidate(
                    satellite_id=satellite.id,
                    satellite_name=satellite.name,
                    satellite_pass=p,
                    pending_telecommands=satellite.pending_telecommands,
                    max_priority=satellite.max_priority,
                    tle_epoch=epoch,
                    data_source=orbital_data.source,
                    forced=find_decision(decisions, satellite.id, p.aos_time) == "force",
                    skipped=find_decision(decisions, satellite.id, p.aos_time) == "skip",
                )
                for p in usable
            ]
            satellite_candidates = mark_telecommand_carriers(satellite_candidates)

            if satellite.track_passes:
                candidates.extend(satellite_candidates)
                continue

            # Recepção desligada: só disputa o rotor a passagem que leva
            # telecomando (ou a que o operador forçou). As outras ficam na
            # previsão, marcadas, para o operador ver o que está deixando de
            # rastrear.
            for candidate in satellite_candidates:
                if candidate.carries_telecommands or candidate.forced:
                    candidates.append(candidate)
                else:
                    reception_off.append(candidate)

        outcomes = plan_passes(candidates)
        telecommand_count: dict[int, int] = {}

        committed = 0
        with self._engine.begin() as conn:
            db.clear_planned_passes(conn)
            for outcome in outcomes:
                if outcome.outcome != "planned":
                    continue
                candidate = outcome.candidate
                pass_id = db.insert_planned_pass(
                    conn, candidate.satellite_id, candidate.satellite_pass,
                    candidate.tle_epoch, candidate.data_source,
                )
                assigned = db.assign_telecommands_to_pass(
                    conn, pass_id, candidate.satellite_id
                )
                telecommand_count[id(candidate)] = assigned
                committed += 1
                logger.info(
                    "Agendado: %s AOS %s (el.máx %.1f graus), %s",
                    candidate.satellite_name,
                    candidate.satellite_pass.aos_time.isoformat(),
                    candidate.satellite_pass.max_elevation_deg,
                    f"{assigned} telecomandos" if assigned else "só recepção",
                )

            db.replace_pass_predictions(
                conn,
                [_prediction_row(o.candidate, o.outcome, o.lost_to,
                                 telecommand_count.get(id(o.candidate), 0))
                 for o in outcomes]
                + [_prediction_row(c, "reception_off", None, 0) for c in reception_off],
                computed_at=now,
            )

        logger.info(
            "Plano atualizado: %d passagens comprometidas de %d candidatas (%.2fs)",
            committed, len(candidates), time.perf_counter() - started,
        )

    def replan_if_operator_changed(self) -> None:
        """Replaneja logo depois de uma ação do operador no painel.

        A API grava a decisão no banco (outra thread, sem memória
        compartilhada); este laço percebe pela versão das tabelas de controle.
        Sem isso, pular uma passagem só apareceria no próximo ciclo, até 5
        minutos depois.
        """
        with self._engine.begin() as conn:
            version = db.control_version(conn)
        if self._control_version is None:
            self._control_version = version
            return
        if version != self._control_version:
            self._control_version = version
            logger.info("Decisão do operador registrada; replanejando.")
            self._last_replan = time.monotonic()
            self.replan()

    # --- Posição corrente ---------------------------------------------------

    def update_tracking_status(self) -> None:
        """Atualiza no banco onde está cada satélite agora."""
        with self._engine.begin() as conn:
            satellites = db.fetch_satellites_with_orbital_data(conn)

        for satellite in satellites:
            fields: dict = {"checked_at": now_utc()}
            try:
                orbital_data = load_orbital_data(satellite)
                satrec = build_satellite(orbital_data)
                info = get_tracking_info(
                    satrec, satellite["name"], when=now_utc(), station=config.GROUND_STATION
                )
                fields.update(
                    latitude_deg=info.geodetic.latitude_deg,
                    longitude_deg=info.geodetic.longitude_deg,
                    altitude_km=info.geodetic.altitude_km,
                    azimuth_deg=info.topocentric.azimuth_deg,
                    elevation_deg=info.topocentric.elevation_deg,
                    range_km=info.topocentric.range_km,
                    is_visible=info.is_visible,
                    tle_epoch=info.tle_epoch,
                    data_source=orbital_data.source,
                    status_message=None,
                )
            except Exception as error:
                # Guardado no banco, e não só no log: uma falha isolada precisa
                # ficar visível para quem olha o painel.
                logger.warning("Sem posição para %s: %s", satellite["name"], error)
                fields["status_message"] = str(error)

            with self._engine.begin() as conn:
                db.upsert_tracking_status(conn, satellite["id"], **fields)

    # --- Execução das passagens ---------------------------------------------

    def drive_passes(self) -> None:
        """Assume a passagem no AOS e a encerra no LOS."""
        now = now_utc()

        with self._engine.begin() as conn:
            finished = db.close_finished_passes(conn, now)
            for pass_id in finished:
                logger.info("Passagem %d concluída.", pass_id)
            if finished:
                # A janela foi rastreada até o fim, então os comandos dela
                # saem da fila. Sem encoder não há confirmação de transmissão:
                # o motivo da marcação fica registrado em execution_logs.
                marked = db.mark_telecommands_sent(conn, finished)
                if marked:
                    logger.info(
                        "%d telecomandos marcados como enviados ao fim da janela "
                        "(sem confirmação de transmissão).", marked,
                    )

            missed = db.expire_missed_passes(conn, now)
            if missed:
                released = db.release_telecommands(conn, missed)
                logger.warning(
                    "Passagens perdidas (%s): janela encerrou sem assumirmos; "
                    "%d telecomandos devolvidos à fila.",
                    ", ".join(str(p) for p in missed), released,
                )

            active = db.fetch_active_pass(conn)
            to_activate = None if active else db.fetch_pass_to_activate(conn, now)

        if to_activate is not None:
            self._activate(to_activate)
        elif active is not None:
            self._ensure_still_tracking(active)

    def _activate(self, scheduled_pass: dict) -> None:
        name = scheduled_pass["satellite_name"]
        try:
            orbital_data = load_orbital_data(scheduled_pass)
            self._station.track_satellite(
                orbital_data.to_json(),
                until_iso=scheduled_pass["los_time"].isoformat(),
                satellite_name=name,
                downlink_frequency_hz=scheduled_pass.get("downlink_frequency_hz"),
            )
        except (StationManagerUnavailable, StationManagerError) as error:
            # Não marca 'missed' aqui: a janela ainda está aberta, e o próximo
            # tick tenta de novo. Só o LOS decide que a passagem foi perdida.
            logger.error("Não foi possível assumir a passagem de %s: %s", name, error)
            return
        except Exception as error:
            logger.exception("Falha ao preparar a passagem de %s", name)
            with self._engine.begin() as conn:
                db.mark_pass(conn, scheduled_pass["id"], "cancelled", str(error))
                db.release_telecommands(conn, [scheduled_pass["id"]])
            return

        with self._engine.begin() as conn:
            db.mark_pass(conn, scheduled_pass["id"], "active")
        logger.info(
            "Passagem de %s assumida pelo Station Manager até %s.",
            name, scheduled_pass["los_time"].isoformat(),
        )

    def _ensure_still_tracking(self, active: dict) -> None:
        """Rearma o Station Manager se ele tiver reiniciado no meio da passagem.

        Simétrico à razão de o laço de apontamento morar lá: se um dos dois
        lados cair, o outro recupera. Reenviar é idempotente e barato.
        """
        try:
            tracking = self._station.get_tracking()
        except (StationManagerUnavailable, StationManagerError) as error:
            logger.warning("Station Manager não respondeu ao get_tracking: %s", error)
            return

        if tracking is None:
            logger.warning(
                "Station Manager não está mais rastreando %s; reenviando a ordem.",
                active["satellite_name"],
            )
            self._activate(active)

    # --- Laço principal -----------------------------------------------------

    def run(self) -> None:
        logger.info(
            "TC Scheduler ativo. Estação: %s (%.4f, %.4f, %.0f m) | horizonte %.0fh | "
            "replanejamento a cada %.0fs",
            config.GROUND_STATION["name"], config.GROUND_STATION["latitude_deg"],
            config.GROUND_STATION["longitude_deg"], config.GROUND_STATION["altitude_m"],
            config.HORIZON_HOURS, config.REPLAN_INTERVAL_SECONDS,
        )

        while self._running:
            cycle_started = time.monotonic()
            try:
                if cycle_started - self._last_replan >= config.REPLAN_INTERVAL_SECONDS:
                    self._last_replan = cycle_started
                    self.replan()
                elif cycle_started - self._last_control_check >= config.CONTROL_POLL_SECONDS:
                    self._last_control_check = cycle_started
                    self.replan_if_operator_changed()

                if cycle_started - self._last_tracking_poll >= config.TRACKING_POLL_INTERVAL_SECONDS:
                    self._last_tracking_poll = cycle_started
                    self.update_tracking_status()

                self.drive_passes()
            except Exception:
                # Banco fora do ar, rede caindo: loga e tenta no próximo tick.
                # Derrubar o processo faria o container reiniciar em laço e
                # perder o plano em andamento.
                logger.exception("Falha no ciclo do scheduler; tentando de novo no próximo tick.")

            time.sleep(config.TICK_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description="TC Scheduler: decide o que a estação rastreia, e quando")
    parser.add_argument("--no-api", action="store_true",
                        help="Não sobe a API de leitura (o painel fica sem a visão de satélites)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    engine = db.build_engine(config.DATABASE_URL)
    # As tabelas de controle de passagens são nossas: criadas aqui, antes de a
    # API subir, porque ela lê e escreve nelas.
    db.ensure_control_schema(engine)
    # O schema é propriedade do TC Generator, noutro repositório, sem
    # migrations ligando os dois. Conferir aqui converte um ProgrammingError
    # no meio de um ciclo (logado como "Falha no ciclo do scheduler", a
    # mensagem mais genérica possível) numa linha explícita no boot.
    schema_check.check(engine)
    station = StationManagerClient(
        config.STATION_MANAGER_ADDRESS, config.ZMQ_REQUEST_TIMEOUT_MS
    )
    scheduler = Scheduler(engine, station)

    api_data = None
    if config.API_ENABLED and not args.no_api:
        api_data = _start_api()

    # SIGTERM é como o Docker pede para o container parar.
    signal.signal(signal.SIGTERM, scheduler.stop)
    signal.signal(signal.SIGINT, scheduler.stop)

    try:
        scheduler.run()
    finally:
        station.close()
        engine.dispose()
        if api_data is not None:
            api_data.close()


def _start_api():
    """Sobe a API de leitura numa thread daemon, e devolve o `station_data` dela.

    Engine próprio, separado do que o laço usa: são dois pools independentes,
    e SQLAlchemy é thread-safe. Nenhum objeto mutável cruza as duas threads.

    Daemon porque o dono do processo é o laço de planejamento — se ele parar,
    a API não tem por que segurar o container de pé. Uma falha aqui é logada e
    não impede o Scheduler de planejar: consultar é o serviço acessório, e
    rastrear é o principal.
    """
    from tc_scheduler import api, station_data as station_data_module
    from tc_scheduler.operator_control import OperatorControl

    try:
        data = station_data_module.from_environment()
        if data is None:
            return None
        # Engine próprio também para o controle do operador: a leitura do
        # painel e as ações dele não disputam o mesmo pool de duas conexões.
        control = OperatorControl(db.build_engine(config.DATABASE_URL))
        thread = threading.Thread(
            target=api.create_app(data, control).run,
            # threaded=True: sem isso, uma consulta lenta ao banco travaria
            # qualquer outra requisição, inclusive o healthcheck do compose.
            kwargs={"host": config.API_HOST, "port": config.API_PORT,
                    "debug": False, "use_reloader": False, "threaded": True},
            daemon=True,
        )
        thread.start()
        logger.info("API de leitura em http://%s:%d", config.API_HOST, config.API_PORT)
        return data
    except Exception:
        logger.exception("Falha ao subir a API de leitura; o Scheduler segue planejando.")
        return None


if __name__ == "__main__":
    main()
