"""Passagem simulada: a próxima passagem real de uma órbita, começando agora.

Na bancada RF o FS-2 fica parado em cima da mesa, e isso vale até a etapa 3:
primeiro o Pi com o Si4463, depois os PlutoSDR. Para exercitar uma passagem
(apontamento, Doppler, sintonia, uplink) sem esperar a órbita, a estação
precisa de uma passagem que comece quando o operador quiser.

**A passagem simulada é um TLE**, e não um relógio adiantado. É a mesma
órbita, com a época recuada de Δ e o nodo ascendente girado junto com a Terra
(a diferença de GMST entre `agora + Δ` e `agora`). No instante t, o satélite
deslocado está, visto da estação, exatamente onde o original estaria em t + Δ:
mesmos azimute, elevação e Doppler, a menos de 0,001° e 0,01 Hz.

Por que um TLE: quem precisa da passagem calcula a partir dele, cada um com o
seu relógio NTP, em tempo real. A estação (Station Manager: rotor, Doppler e
sintonia; uplink corrigido) recebe o TLE no `track_satellite`. O rádio da
bancada, que vai aplicar o Doppler ao FS-2 parado (o simulador no Pi agora, o
adaptador do Pluto depois), lê o mesmo TLE em `GET /api/simulated-pass` e faz
a conta com a geometria dele. São duas contas independentes da mesma órbita,
e é isso que dá valor ao teste. Um relógio adiantado só funcionaria com todos
adiantados igual. Foi o que impediu o uplink no modo `next-pass` do simulador.

A órbita pode ser do próprio satélite ou emprestada: o FS-2 ainda não tem TLE.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import pi
from typing import Optional

from sgp4.api import WGS72, Satrec, jday
from sgp4.exporter import export_tle
from spacelab_tracking import build_satellite, predict_passes
from spacelab_tracking.coordinates import gmst_rad

# A janela começa um pouco depois de criada: o Scheduler assume a passagem no
# próximo tick, e o rotor tem tempo de ir ao azimute do AOS.
DEFAULT_LEAD_S = 15.0
DEFAULT_MIN_PEAK_DEG = 30.0
# Três dias acham uma passagem de 30°+ para qualquer LEO de inclinação média.
DEFAULT_SEARCH_HOURS = 72.0
# A conferência do TLE deslocado: o AOS tem de cair onde foi pedido.
AOS_TOLERANCE_S = 5.0
PEAK_TOLERANCE_DEG = 0.5

_JD_1949_12_31 = 2433281.5   # o zero da época do sgp4init


class NoSuitablePass(Exception):
    """A órbita não tem passagem com a elevação pedida na janela de busca."""


@dataclass(frozen=True)
class SimulatedPass:
    tle_line1: str
    tle_line2: str
    orbit_name: str            # de quem é a órbita (o próprio satélite ou o doador)
    shift_s: float             # Δ: a passagem original acontece Δ segundos depois
    original_aos: datetime
    satellite_pass: object     # SatellitePass da órbita deslocada (AOS ~ agora)


def _gmst(when: datetime) -> float:
    jd, fr = jday(when.year, when.month, when.day, when.hour, when.minute,
                  when.second + when.microsecond / 1e6)
    return gmst_rad(jd, fr)


def shift_orbit(satrec: Satrec, shift_s: float, at: datetime) -> tuple[str, str]:
    """TLE de uma órbita que, em t, está onde `satrec` estaria em t + shift_s.

    Visto da Terra, não do espaço: além de recuar a época, gira o nodo pela
    rotação da Terra no intervalo. A rotação vem do mesmo GMST que a
    `spacelab-tracking` usa de TEME para ECEF, calculado em `at`. O erro do
    termo quadrático do GMST em poucos dias é desprezível.
    """
    dtheta = _gmst(at + timedelta(seconds=shift_s)) - _gmst(at)
    epoch = satrec.jdsatepoch - _JD_1949_12_31 + satrec.jdsatepochF - shift_s / 86400.0
    shifted = Satrec()
    shifted.sgp4init(WGS72, "i", satrec.satnum, epoch, satrec.bstar, satrec.ndot,
                     satrec.nddot, satrec.ecco, satrec.argpo, satrec.inclo, satrec.mo,
                     satrec.no_kozai, (satrec.nodeo - dtheta) % (2 * pi))
    return export_tle(shifted)


def next_pass_now(orbital_data, orbit_name: str, station: dict, now: datetime,
                  min_peak_deg: float = DEFAULT_MIN_PEAK_DEG,
                  lead_s: float = DEFAULT_LEAD_S,
                  search_hours: float = DEFAULT_SEARCH_HOURS,
                  min_elevation_deg: Optional[float] = None) -> SimulatedPass:
    """A próxima passagem com elevação máxima >= `min_peak_deg`, com AOS em now + lead_s.

    Confere o TLE deslocado antes de devolvê-lo. Uma conta errada aqui viraria
    uma passagem simulada que a estação e a bancada calculariam diferente, e
    ninguém saberia de quem é o erro.
    """
    satrec = build_satellite(orbital_data)
    start = now + timedelta(seconds=lead_s)
    passes = predict_passes(satrec, orbit_name, station=station, start=now,
                            search_window_hours=search_hours,
                            min_elevation_deg=min_elevation_deg)
    chosen = next((p for p in passes
                   if p.aos_time >= start and p.max_elevation_deg >= min_peak_deg), None)
    if chosen is None:
        raise NoSuitablePass(f"{orbit_name}: nenhuma passagem com elevação máxima >= "
                             f"{min_peak_deg:g}° nas próximas {search_hours:g} h")

    shift_s = (chosen.aos_time - start).total_seconds()
    line1, line2 = shift_orbit(satrec, shift_s, now)

    shifted = Satrec.twoline2rv(line1, line2)
    window_h = (lead_s + chosen.duration_seconds + 300) / 3600
    found = predict_passes(shifted, orbit_name, station=station, start=now,
                           search_window_hours=window_h, min_elevation_deg=min_elevation_deg)
    if (not found
            or abs((found[0].aos_time - start).total_seconds()) > AOS_TOLERANCE_S
            or abs(found[0].max_elevation_deg - chosen.max_elevation_deg) > PEAK_TOLERANCE_DEG):
        got = (f"AOS {found[0].aos_time:%H:%M:%S}, pico {found[0].max_elevation_deg:.1f}°"
               if found else "nenhuma passagem")
        raise RuntimeError(f"TLE deslocado não reproduz a passagem: esperava AOS "
                           f"{start:%H:%M:%S} e pico {chosen.max_elevation_deg:.1f}°, veio {got}")
    return SimulatedPass(tle_line1=line1, tle_line2=line2, orbit_name=orbit_name,
                         shift_s=shift_s, original_aos=chosen.aos_time,
                         satellite_pass=found[0])
