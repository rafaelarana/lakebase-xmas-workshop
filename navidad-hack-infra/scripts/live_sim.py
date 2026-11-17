"""Delivery-night simulator for the control center (pure Python, testable).

Replays the planned stops of one night (journeys_sync) on an accelerated clock:
- each route is split into ``helpers`` contiguous segments, one helper each
  (duendes for the Nochebuena bringers, pajes for the Reyes Magos);
- a stop produces an ``entregado`` event at its planned arrival time;
- a fraction ``exception_rate`` produce an ``excepcion`` instead, and some of those are
  retried later by the same helper (``entregado`` again, 20–40 simulated minutes later).

The producer notebook turns events into Zerobus records (or direct Lakebase rows).
"""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator

EXCEPTION_CODES = {  # code: (weight, retry probability)
    "chimenea_bloqueada": (3, 0.5),
    "nino_despierto": (3, 0.8),
    "perro_guardian": (2, 0.6),
    "direccion_no_encontrada": (2, 0.2),
    "regalo_equivocado": (1, 0.0),
    "retraso_transporte": (2, 0.7),   # trineo (Nochebuena) / camello (Reyes)
    "clima": (1, 0.3),
}
# Regional profiles: an overall rate multiplier and per-code weight multipliers, so the map shows real
# differences (the north gets weather, big cities lose addresses, islands depend on transport…).
REGIONS = {
    "norte": {"cities": {"A Coruña", "Vigo", "Gijón", "Bilbao", "San Sebastián", "Vitoria-Gasteiz", "Pamplona"},
              "rate": 1.5, "codes": {"clima": 5.0, "retraso_transporte": 1.5}},
    "grandes": {"cities": {"Madrid", "Barcelona"},
                "rate": 1.25, "codes": {"direccion_no_encontrada": 3.0, "perro_guardian": 0.5}},
    "islas": {"cities": {"Las Palmas de Gran Canaria", "Santa Cruz de Tenerife", "Palma"},
              "rate": 1.4, "codes": {"retraso_transporte": 4.0, "clima": 1.5}},
    "sur": {"cities": {"Sevilla", "Málaga", "Córdoba", "Granada", "Murcia", "Alicante", "Valencia"},
            "rate": 0.7, "codes": {"nino_despierto": 2.5, "clima": 0.3}},
}
# Incident bursts (zones from the incidents track, navidad_geo.zone): inside the zone, during the middle of
# the night, exceptions jump and are rarely fixed by a retry — the hot spot the agent has to explain.
INCIDENTS = {
    "dana_valencia": {"label": "DANA Valencia", "rate": 0.12, "codes": {"clima": 3, "direccion_no_encontrada": 2}, "retry": 0.1},
    "cortes_barcelona": {"label": "Cortes de calle Barcelona", "rate": 0.10,
                         "codes": {"retraso_transporte": 3, "direccion_no_encontrada": 2}, "retry": 0.3},
    "filomena_madrid": {"label": "Borrasca tipo Filomena Madrid", "rate": 0.06, "codes": {"clima": 4, "retraso_transporte": 2},
                        "retry": 0.15},
}
BURST_WINDOW = (0.35, 0.65)  # middle share of the zone's own visits (by arrival time)


def burst_window(stops: list, zone: set[int] | frozenset) -> tuple[datetime, datetime] | None:
    """Time window of the burst: the middle BURST_WINDOW share of the stops INSIDE the zone, ranked by arrival.

    Not a fraction of the whole night: with compact (Hilbert) routes a zone is visited in one or two short
    blocks (DANA Valencia: deciles 1–2 and 8–9 of the night), so a fixed 35–65 % of the night hits ~1 % of it."""
    times = sorted(s.arrived_at for s in stops if s.household_id in zone)
    if not times:
        return None
    lo = int(len(times) * BURST_WINDOW[0])
    hi = max(lo, int(len(times) * BURST_WINDOW[1]) - 1)
    return times[lo], times[hi]


def region_of(city: str) -> dict | None:
    return next((r for r in REGIONS.values() if city in r["cities"]), None)


DUENDES = ["Pinito", "Cascabel", "Jengibre", "Turrón", "Polvorón", "Mazapán", "Estrella", "Copito",
           "Muérdago", "Avellana", "Canela", "Chispa", "Bombón", "Piñón", "Nevado", "Regaliz"]
PAJES = ["Aldebarán", "Sirio", "Vega", "Altair", "Rigel", "Antares", "Capella", "Deneb",
         "Arturo", "Polaris", "Mira", "Lira", "Orión", "Castor", "Pólux", "Electra"]
REYES = {"melchor", "gaspar", "baltasar"}


@dataclass(frozen=True)
class Stop:
    route_id: int
    stop_number: int
    household_id: int
    bringer: str
    city: str
    arrived_at: datetime   # planned arrival (UTC, aware)
    lat: float
    lon: float


@dataclass(order=True)
class Event:
    event_ts: datetime
    seq: int
    event_type: str = field(compare=False)
    stop: Stop = field(compare=False)
    helper_id: str = field(compare=False)
    helper_name: str = field(compare=False)
    helper_kind: str = field(compare=False)
    exception_code: str | None = field(compare=False, default=None)
    gifts: int = field(compare=False, default=0)
    event_id: str = field(compare=False, default_factory=lambda: str(uuid.uuid4()))


def helper_kind(bringer: str) -> str:
    return "paje" if bringer in REYES else "duende"


def assign_helpers(stops: Iterable[Stop], helpers: int) -> dict[tuple[int, int], tuple[str, str, str]]:
    """(route_id, stop_number) -> (helper_id, helper_name, helper_kind); contiguous segments per route."""
    by_route: dict[int, list[Stop]] = {}
    for s in stops:
        by_route.setdefault(s.route_id, []).append(s)
    out: dict[tuple[int, int], tuple[str, str, str]] = {}
    for route_id, rs in by_route.items():
        rs.sort(key=lambda s: s.stop_number)
        k = max(1, min(helpers, len(rs)))
        size = -(-len(rs) // k)
        kind = helper_kind(rs[0].bringer)
        names = PAJES if kind == "paje" else DUENDES
        for i, s in enumerate(rs):
            seg = i // size
            name = names[(route_id * 7 + seg) % len(names)]
            out[(route_id, s.stop_number)] = (f"{route_id}-{seg + 1}", f"{name} {seg + 1}", kind)
    return out


def _weights(mult: dict[str, float]) -> list[float]:
    return [EXCEPTION_CODES[c][0] * mult.get(c, 1.0) for c in EXCEPTION_CODES]


def build_events(stops: list[Stop], *, helpers: int = 4, exception_rate: float = 0.015, seed: int = 7,
                 regional: bool = True, incident: str | None = None,
                 incident_households: set[int] | frozenset = frozenset()) -> list[Event]:
    """``regional``: apply REGIONS; ``incident``: a key of INCIDENTS, with the households inside its zone."""
    rng = random.Random(seed)
    codes = list(EXCEPTION_CODES)
    base_w = _weights({})
    who = assign_helpers(stops, helpers)
    burst = INCIDENTS.get(incident or "")
    window = burst_window(stops, incident_households) if burst else None
    if window is None:
        burst = None
    else:
        w0, w1 = window
    region_w: dict[str, tuple[float, list[float]]] = {}
    events: list[Event] = []
    seq = 0
    for s in stops:
        hid, hname, kind = who[(s.route_id, s.stop_number)]
        gifts = rng.randint(1, 4)
        if s.city not in region_w:
            r = region_of(s.city) if regional else None
            region_w[s.city] = (r["rate"], _weights(r["codes"])) if r else (1.0, base_w)
        mult, weights = region_w[s.city]
        rate, retry_override = exception_rate * mult, None
        if burst and s.household_id in incident_households and w0 <= s.arrived_at <= w1:
            rate, weights, retry_override = burst["rate"], _weights({c: 0.01 for c in codes} | burst["codes"]), burst["retry"]
        if rng.random() < rate:
            code = rng.choices(codes, weights)[0]
            events.append(Event(s.arrived_at, seq, "excepcion", s, hid, hname, kind, code, 0)); seq += 1
            retry_p = EXCEPTION_CODES[code][1] if retry_override is None else retry_override
            if rng.random() < retry_p:
                retry = s.arrived_at + timedelta(minutes=rng.randint(20, 40))
                events.append(Event(retry, seq, "entregado", s, hid, hname, kind, None, gifts)); seq += 1
        else:
            events.append(Event(s.arrived_at, seq, "entregado", s, hid, hname, kind, None, gifts)); seq += 1
    events.sort()
    return events


def micros(ts: datetime) -> int:
    return int(ts.timestamp() * 1_000_000)


def to_record(e: Event, *, sim_run_id: str, year: int, campaign: str, sent: datetime) -> dict:
    """Zerobus JSON record = one bronze row (timestamps as epoch microseconds)."""
    s = e.stop
    return {"event_id": e.event_id, "sent_ts": micros(sent), "event_ts": micros(e.event_ts), "sim_run_id": sim_run_id,
            "year": year, "campaign": campaign, "city": s.city, "route_id": s.route_id, "stop_number": s.stop_number,
            "household_id": s.household_id, "bringer": s.bringer, "helper_id": e.helper_id,
            "helper_name": e.helper_name, "helper_kind": e.helper_kind, "event_type": e.event_type,
            "exception_code": e.exception_code, "gifts": e.gifts, "lat": s.lat, "lon": s.lon}


class Clock:
    """Simulated time = night_start + (real elapsed) × speed."""

    def __init__(self, night_start: datetime, night_end: datetime, real_seconds: float, real_start: datetime | None = None):
        self.night_start, self.night_end = night_start, night_end
        self.speed = max(1.0, (night_end - night_start).total_seconds() / max(1.0, real_seconds))
        self.real_start = real_start or datetime.now(timezone.utc)

    def sim_now(self, real_now: datetime | None = None) -> datetime:
        real_now = real_now or datetime.now(timezone.utc)
        return self.night_start + timedelta(seconds=(real_now - self.real_start).total_seconds() * self.speed)


def due(events: list[Event], start: int, sim_now: datetime) -> Iterator[int]:
    """Indexes of events with event_ts <= sim_now, from position ``start`` (events sorted)."""
    i = start
    while i < len(events) and events[i].event_ts <= sim_now:
        yield i
        i += 1
