from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from es_land import CITY_LAND

# Two separate boxes: a single Spain box would swallow half the Atlantic.
PENINSULA_BALEARES_BBOX = dict(lat_min=35.9, lat_max=43.9, lon_min=-9.4, lon_max=4.4)
CANARIAS_BBOX = dict(lat_min=27.6, lat_max=29.5, lon_min=-18.2, lon_max=-13.3)

JITTER_SIGMA_DEG = 0.025
MAX_OFFSET_DEG = 0.08
MAX_LAND_TRIES = 100

SURNAMES: tuple[str, ...] = (
    "García", "Fernández", "González", "Rodríguez", "López", "Martínez",
    "Sánchez", "Pérez", "Gómez", "Martín", "Jiménez", "Ruiz",
    "Hernández", "Díaz", "Moreno", "Muñoz", "Álvarez", "Romero",
    "Alonso", "Gutiérrez", "Navarro", "Torres", "Domínguez", "Vázquez",
    "Ramos", "Gil", "Ramírez", "Serrano", "Blanco", "Molina",
    "Morales", "Suárez", "Ortega", "Delgado", "Castro", "Ortiz",
    "Rubio", "Marín", "Sanz", "Núñez", "Iglesias", "Medina",
    "Garrido", "Cortés", "Castillo", "Santos", "Lozano", "Guerrero",
)


@dataclass(frozen=True)
class City:
    name: str
    province: str
    autonomous_community: str
    lat: float
    lon: float
    postcode_area: str  # first two digits of the código postal (province)
    utc_offset_hours: int  # winter offset; no DST in Dec/Jan
    weight: float  # municipal population share (INE, rounded)


def _tz(utc_offset_hours: int) -> str:
    return "Atlantic/Canary" if utc_offset_hours == 0 else "Europe/Madrid"


ES_CITIES: list[City] = [
    City("Madrid", "Madrid", "Comunidad de Madrid", 40.4168, -3.7038, "28", 1, 3.40),
    City("Barcelona", "Barcelona", "Cataluña", 41.3874, 2.1686, "08", 1, 1.69),
    City("Valencia", "Valencia", "Comunitat Valenciana", 39.4699, -0.3763, "46", 1, 0.81),
    City("Zaragoza", "Zaragoza", "Aragón", 41.6488, -0.8891, "50", 1, 0.69),
    City("Sevilla", "Sevilla", "Andalucía", 37.3891, -5.9845, "41", 1, 0.69),
    City("Málaga", "Málaga", "Andalucía", 36.7260, -4.4250, "29", 1, 0.59),
    City("Murcia", "Murcia", "Región de Murcia", 37.9922, -1.1307, "30", 1, 0.47),
    City("Palma", "Illes Balears", "Illes Balears", 39.5696, 2.6502, "07", 1, 0.43),
    City("Las Palmas de Gran Canaria", "Las Palmas", "Canarias", 28.1235, -15.4363, "35", 0, 0.38),
    City("Alicante", "Alicante", "Comunitat Valenciana", 38.3452, -0.4810, "03", 1, 0.36),
    City("Bilbao", "Bizkaia", "País Vasco", 43.2630, -2.9350, "48", 1, 0.35),
    City("Córdoba", "Córdoba", "Andalucía", 37.8882, -4.7794, "14", 1, 0.32),
    City("Valladolid", "Valladolid", "Castilla y León", 41.6523, -4.7245, "47", 1, 0.30),
    City("Vigo", "Pontevedra", "Galicia", 42.2406, -8.7207, "36", 1, 0.29),
    City("Gijón", "Asturias", "Principado de Asturias", 43.5322, -5.6611, "33", 1, 0.27),
    City("Vitoria-Gasteiz", "Araba/Álava", "País Vasco", 42.8467, -2.6716, "01", 1, 0.26),
    City("A Coruña", "A Coruña", "Galicia", 43.3623, -8.4115, "15", 1, 0.25),
    City("Granada", "Granada", "Andalucía", 37.1773, -3.5986, "18", 1, 0.23),
    City("Santa Cruz de Tenerife", "Santa Cruz de Tenerife", "Canarias", 28.4636, -16.2518, "38", 0, 0.21),
    City("Pamplona", "Navarra", "Comunidad Foral de Navarra", 42.8125, -1.6458, "31", 1, 0.21),
    City("San Sebastián", "Gipuzkoa", "País Vasco", 43.3183, -1.9812, "20", 1, 0.19),
]

# East → west, following midnight; the Canaries (one hour behind) close the night.
CITY_TOUR_ORDER: list[str] = [
    "Palma",
    "Barcelona",
    "Valencia",
    "Alicante",
    "Zaragoza",
    "Murcia",
    "Pamplona",
    "San Sebastián",
    "Vitoria-Gasteiz",
    "Bilbao",
    "Granada",
    "Madrid",
    "Málaga",
    "Valladolid",
    "Córdoba",
    "Gijón",
    "Sevilla",
    "A Coruña",
    "Vigo",
    "Las Palmas de Gran Canaria",
    "Santa Cruz de Tenerife",
]

CITIES_BY_NAME: dict[str, City] = {c.name: c for c in ES_CITIES}

# Campaigns. Nochebuena = night of 24 Dec; Reyes = night of 5→6 Jan of the next
# calendar year. `year` is always the campaign year (Christmas 2025 → Reyes on 2026-01-06).
CAMPAIGNS: tuple[str, ...] = ("nochebuena", "reyes")
NOCHEBUENA_BRINGERS: tuple[str, ...] = ("papa_noel", "olentzero", "tio", "apalpador")
REYES_BRINGERS: tuple[str, ...] = ("melchor", "gaspar", "baltasar")
CAMPAIGN_BRINGERS: dict[str, tuple[str, ...]] = {
    "nochebuena": NOCHEBUENA_BRINGERS,
    "reyes": REYES_BRINGERS,
}
# Local start time and night length in minutes; arrivals are compressed to fit the night.
CAMPAIGN_NIGHT: dict[str, tuple[int, int, int, int, int]] = {
    # (month, day, hour, year_offset, night_minutes)
    "nochebuena": (12, 24, 22, 0, 9 * 60),
    "reyes": (1, 6, 1, 1, 7 * 60),
}

# Who comes on Nochebuena, by autonomous community. "none" = Reyes only.
REGIONAL_NOCHEBUENA: dict[str, tuple[tuple[str, ...], tuple[float, ...]]] = {
    "País Vasco": (("olentzero", "papa_noel", "none"), (0.55, 0.25, 0.20)),
    "Comunidad Foral de Navarra": (("olentzero", "papa_noel", "none"), (0.45, 0.30, 0.25)),
    "Cataluña": (("tio", "papa_noel", "none"), (0.45, 0.30, 0.25)),
    "Galicia": (("apalpador", "papa_noel", "none"), (0.15, 0.45, 0.40)),
}
DEFAULT_NOCHEBUENA: tuple[tuple[str, ...], tuple[float, ...]] = (
    ("papa_noel", "none"),
    (0.60, 0.40),
)
REYES_VISIT_RATE = 0.92


def _route_ids() -> dict[tuple[str, str, str], int]:
    """Stable route ids: one per (campaign, city, bringer), independent of data and seed."""
    ids: dict[tuple[str, str, str], int] = {}
    for campaign in CAMPAIGNS:
        for city in CITY_TOUR_ORDER:
            for bringer in CAMPAIGN_BRINGERS[campaign]:
                ids[(campaign, city, bringer)] = len(ids) + 1
    return ids


ROUTE_IDS: dict[tuple[str, str, str], int] = _route_ids()


@dataclass(frozen=True)
class GenerateConfig:
    seed: int = 20260106
    n_households: int = 500_000
    n_people: int = 2_000_000
    n_years: int = 5
    first_year: int = 2021
    revisit_rate: float = 0.001
    scale: float = 1.0

    @property
    def households(self) -> int:
        return max(1, int(self.n_households * self.scale))

    @property
    def scaled_people(self) -> int:
        return max(self.households, int(self.n_people * self.scale))


@dataclass
class Household:
    household_id: int
    label: str
    city: str
    province: str
    autonomous_community: str
    lat: float
    lon: float
    postcode_area: str
    timezone: str
    household_size: int
    first_seen_year: int
    nochebuena_bringer: str
    visits_reyes: bool


def _point_in_ring(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def on_land(city: City, lat: float, lon: float) -> bool:
    rings = CITY_LAND.get(city.name)
    if rings is None:  # inland city: the whole jitter box is land
        return True
    return any(_point_in_ring(lon, lat, ring) for ring in rings)


def _jitter_near_city(rng: random.Random, city: City) -> tuple[float, float]:
    for _ in range(MAX_LAND_TRIES):
        dlat = rng.gauss(0.0, JITTER_SIGMA_DEG)
        dlon = rng.gauss(0.0, JITTER_SIGMA_DEG)
        offset = math.hypot(dlat, dlon)
        if offset > MAX_OFFSET_DEG:
            scale = MAX_OFFSET_DEG / offset
            dlat *= scale
            dlon *= scale
        lat, lon = city.lat + dlat, city.lon + dlon
        if on_land(city, lat, lon):
            return lat, lon
    return city.lat, city.lon


def generate_households(cfg: GenerateConfig) -> list[Household]:
    rng = random.Random(cfg.seed)
    n = cfg.households
    weights = [c.weight for c in ES_CITIES]
    year_lo = cfg.first_year
    year_hi = cfg.first_year + cfg.n_years - 1
    rows: list[Household] = []
    for hid in range(1, n + 1):
        city = rng.choices(ES_CITIES, weights=weights, k=1)[0]
        lat, lon = _jitter_near_city(rng, city)
        first, second = rng.sample(SURNAMES, 2)
        options, probs = REGIONAL_NOCHEBUENA.get(city.autonomous_community, DEFAULT_NOCHEBUENA)
        bringer = rng.choices(options, weights=probs, k=1)[0]
        visits_reyes = rng.random() < REYES_VISIT_RATE or bringer == "none"
        rows.append(
            Household(
                household_id=hid,
                label=f"Familia {first} {second}",
                city=city.name,
                province=city.province,
                autonomous_community=city.autonomous_community,
                lat=lat,
                lon=lon,
                postcode_area=city.postcode_area,
                timezone=_tz(city.utc_offset_hours),
                household_size=1,
                first_seen_year=rng.randint(year_lo, year_hi),
                nochebuena_bringer=bringer,
                visits_reyes=visits_reyes,
            )
        )
    return rows


GIVEN_NAMES: tuple[str, ...] = (
    "Lucía", "Hugo", "Martina", "Mateo", "Sofía", "Martín", "María", "Pablo",
    "Julia", "Daniel", "Paula", "Alejandro", "Valeria", "Leo", "Emma", "Álvaro",
    "Daniela", "Manuel", "Carla", "Adrián", "Alba", "Enzo", "Noa", "Mario",
    "Carmen", "Javier", "Elena", "David", "Rosa", "José", "Pilar", "Antonio",
    "Isabel", "Francisco", "Dolores", "Miguel", "Teresa", "Rafael", "Ana", "Luis",
)
REGIONAL_GIVEN_NAMES: dict[str, tuple[str, ...]] = {
    "Cataluña": ("Jordi", "Montserrat", "Pau", "Núria", "Arnau", "Laia", "Oriol", "Mireia"),
    "País Vasco": ("Iker", "Ane", "Unai", "Nahia", "Aitor", "Maialen", "Jon", "Irati"),
    "Comunidad Foral de Navarra": ("Iñigo", "Amaia", "Xabier", "Leire", "Mikel", "Ainhoa"),
    "Galicia": ("Xoán", "Uxía", "Brais", "Antía", "Iago", "Sabela", "Xurxo", "Iria"),
    "Illes Balears": ("Joan", "Catalina", "Biel", "Margalida", "Toni", "Aina"),
}
REGIONAL_NAME_RATE = 0.35

HOUSEHOLD_SIZE_VALUES: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8)
HOUSEHOLD_SIZE_WEIGHTS: tuple[float, ...] = (0.08, 0.18, 0.22, 0.24, 0.16, 0.08, 0.03, 0.01)

GIFT_AFFINITIES: tuple[str, ...] = (
    "juguetes", "libros", "tecnologia", "ropa", "dulces", "musica", "deporte",
)
WINDOW_POLICIES: tuple[str, ...] = ("zapatos", "balcon", "ventana", "chimenea", "magia")
WINDOW_WEIGHTS: tuple[float, ...] = (0.40, 0.25, 0.15, 0.10, 0.10)
DIETARY_NOTES: tuple[str, ...] = (
    "ninguna", "frutos_secos", "gluten", "lactosa", "vegetariana", "vegana",
)
DIETARY_WEIGHTS: tuple[float, ...] = (0.68, 0.07, 0.07, 0.07, 0.06, 0.05)


@dataclass
class Person:
    person_id: int
    household_id: int
    given_name: str
    age_years: int
    role: str  # "child" | "adult"


@dataclass
class Preference:
    person_id: int
    naughty_nice_score: int  # 0-100; low scores get carbón from the Reyes
    gift_affinity: str
    dietary_notes: str
    window_policy: str
    favourite_king: str
    last_updated_year: int


def _household_sizes(rng: random.Random, n_households: int, target_people: int) -> list[int]:
    sizes = rng.choices(HOUSEHOLD_SIZE_VALUES, weights=HOUSEHOLD_SIZE_WEIGHTS, k=n_households)
    total = sum(sizes)
    idx = n_households - 1
    while total < target_people and idx >= 0:
        room = 8 - sizes[idx]
        if room > 0:
            bump = min(room, target_people - total)
            sizes[idx] += bump
            total += bump
        idx -= 1
    idx = n_households - 1
    while total > target_people and idx >= 0:
        spare = sizes[idx] - 1
        if spare > 0:
            cut = min(spare, total - target_people)
            sizes[idx] -= cut
            total -= cut
        idx -= 1
    if total != target_people:
        raise ValueError(
            f"cannot match people count {target_people} across {n_households} households"
        )
    return sizes


def _given_name(rng: random.Random, community: str) -> str:
    regional = REGIONAL_GIVEN_NAMES.get(community)
    if regional and rng.random() < REGIONAL_NAME_RATE:
        return rng.choice(regional)
    return rng.choice(GIVEN_NAMES)


def generate_people(cfg: GenerateConfig, households: list[Household]) -> list[Person]:
    rng = random.Random(cfg.seed + 1)
    sizes = _household_sizes(rng, len(households), cfg.scaled_people)
    people: list[Person] = []
    pid = 1
    for household, size in zip(households, sizes):
        ages: list[int] = []
        for _ in range(size):
            ages.append(rng.randint(0, 90))
        if all(age < 18 for age in ages):
            ages[0] = rng.randint(18, 90)
        for age in ages:
            role = "child" if age < 18 else "adult"
            people.append(
                Person(
                    person_id=pid,
                    household_id=household.household_id,
                    given_name=_given_name(rng, household.autonomous_community),
                    age_years=age,
                    role=role,
                )
            )
            pid += 1
        household.household_size = size
    return people


def generate_preferences(cfg: GenerateConfig, people: list[Person]) -> list[Preference]:
    rng = random.Random(cfg.seed + 2)
    year_lo = cfg.first_year
    year_hi = cfg.first_year + cfg.n_years - 1
    prefs: list[Preference] = []
    for person in people:
        prefs.append(
            Preference(
                person_id=person.person_id,
                naughty_nice_score=rng.randint(0, 100),
                gift_affinity=rng.choice(GIFT_AFFINITIES),
                dietary_notes=rng.choices(DIETARY_NOTES, weights=DIETARY_WEIGHTS, k=1)[0],
                window_policy=rng.choices(WINDOW_POLICIES, weights=WINDOW_WEIGHTS, k=1)[0],
                favourite_king=rng.choice(REYES_BRINGERS),
                last_updated_year=rng.randint(year_lo, year_hi),
            )
        )
    return prefs


@dataclass
class JourneyStop:
    year: int
    campaign: str
    route_id: int
    stop_number: int
    household_id: int
    bringer: str
    arrived_at_utc: str  # ISO-8601 Z
    dwell_minutes: int
    is_revisit: bool


def _king_for(city: City, household: Household) -> str:
    """Each king owns a fixed 120° sector of every city, measured from the centre."""
    angle = math.degrees(math.atan2(household.lat - city.lat, household.lon - city.lon)) % 360.0
    return REYES_BRINGERS[min(2, int(angle // 120.0))]


def _route_groups(households: list[Household]) -> dict[tuple[str, str, str], list[Household]]:
    groups: dict[tuple[str, str, str], list[Household]] = {key: [] for key in ROUTE_IDS}
    for household in households:
        city = CITIES_BY_NAME[household.city]
        if household.nochebuena_bringer != "none":
            groups[("nochebuena", city.name, household.nochebuena_bringer)].append(household)
        if household.visits_reyes:
            groups[("reyes", city.name, _king_for(city, household))].append(household)
    for rows in groups.values():
        rows.sort(key=hilbert_sort_key(rows))
    return groups


HILBERT_ORDER = 16


def hilbert_index(x: int, y: int, order: int = HILBERT_ORDER) -> int:
    """Position of cell (x, y) along a Hilbert curve over a 2^order × 2^order grid."""
    n, d, s = 1 << order, 0, 1 << (order - 1)
    while s:
        rx, ry = (1 if x & s else 0), (1 if y & s else 0)
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x, y = n - 1 - x, n - 1 - y
            x, y = y, x
        s >>= 1
    return d


def hilbert_sort_key(rows: list[Household]):
    """Visit order along a space-filling curve over the group's bounding box: consecutive stops are
    neighbours, so routes (and the crews' contiguous segments) are compact instead of sweeping the city
    west → east (Madrid Nochebuena: 258,758 km → ~3,257 km, leg p50 2.6 km → 28 m).

    Each axis is stretched to the full grid on purpose. One uniform scale with a cos(lat) correction
    was measured (scale 0.3): −1.6 % km overall, but mixed per route (−10 % … +14 %). Not worth
    changing every journey (and the deck's measured figures) for that."""
    if not rows:
        return lambda h: 0
    la0, la1 = min(h.lat for h in rows), max(h.lat for h in rows)
    lo0, lo1 = min(h.lon for h in rows), max(h.lon for h in rows)
    n = (1 << HILBERT_ORDER) - 1
    sx, sy = n / max(lo1 - lo0, 1e-9), n / max(la1 - la0, 1e-9)
    return lambda h: (hilbert_index(int((h.lon - lo0) * sx), int((h.lat - la0) * sy)), h.household_id)


def _night_start_utc(campaign: str, year: int, city: City) -> datetime:
    month, day, hour, year_offset, _ = CAMPAIGN_NIGHT[campaign]
    local = datetime(year + year_offset, month, day, hour, 0, 0, tzinfo=timezone.utc)
    return local - timedelta(hours=city.utc_offset_hours)


def generate_journeys(cfg: GenerateConfig, households: list[Household]) -> list[JourneyStop]:
    rng = random.Random(cfg.seed + 3)
    groups = _route_groups(households)
    years = range(cfg.first_year, cfg.first_year + cfg.n_years)

    # (year, campaign, city, bringer, sequence of (household, is_revisit))
    sequences: list[tuple[int, str, str, str, list[tuple[int, bool]]]] = []
    revisit_count = 0
    for year in years:
        for (campaign, city, bringer), ordered in groups.items():
            if not ordered:
                continue
            seq: list[tuple[int, bool]] = [(h.household_id, False) for h in ordered]
            insert_after: list[int] = []
            for i in range(len(seq)):
                if rng.random() < cfg.revisit_rate:
                    insert_after.append(i)
            for i in reversed(insert_after):
                back = max(0, i - rng.randint(0, 30))
                seq.insert(i + 1, (seq[back][0], True))
                revisit_count += 1
            sequences.append((year, campaign, city, bringer, seq))

    if revisit_count == 0 and sequences:
        seq = sequences[0][4]
        seq.insert(1 if len(seq) > 1 else len(seq), (seq[0][0], True))

    stops: list[JourneyStop] = []
    for year, campaign, city_name, bringer, seq in sequences:
        city = CITIES_BY_NAME[city_name]
        night_minutes = CAMPAIGN_NIGHT[campaign][4]
        start = _night_start_utc(campaign, year, city) + timedelta(
            minutes=CITY_TOUR_ORDER.index(city_name)
        )
        dwells = [rng.randint(2, 8) for _ in seq]
        hops = [rng.randint(1, 4) for _ in seq]
        total = sum(dwells) + sum(hops)
        # Magia navideña: compress the route so it always fits inside the night.
        squeeze = min(1.0, night_minutes / total) if total else 1.0
        elapsed = 0.0
        route_id = ROUTE_IDS[(campaign, city_name, bringer)]
        for stop_number, ((household_id, is_revisit), dwell, hop) in enumerate(
            zip(seq, dwells, hops), start=1
        ):
            arrived = start + timedelta(seconds=round(elapsed * squeeze * 60))
            stops.append(
                JourneyStop(
                    year=year,
                    campaign=campaign,
                    route_id=route_id,
                    stop_number=stop_number,
                    household_id=household_id,
                    bringer=bringer,
                    arrived_at_utc=arrived.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    dwell_minutes=dwell,
                    is_revisit=is_revisit,
                )
            )
            elapsed += dwell + hop
    return stops


COLUMN_ORDER: dict[str, list[str]] = {
    "households": [
        "household_id", "label", "city", "province", "autonomous_community",
        "lat", "lon", "postcode_area", "timezone", "household_size",
        "first_seen_year", "nochebuena_bringer", "visits_reyes",
    ],
    "people": ["person_id", "household_id", "given_name", "age_years", "role"],
    "preferences": [
        "person_id", "naughty_nice_score", "gift_affinity", "dietary_notes",
        "window_policy", "favourite_king", "last_updated_year",
    ],
    "journeys": [
        "year", "campaign", "route_id", "stop_number", "household_id",
        "bringer", "arrived_at_utc", "dwell_minutes", "is_revisit",
    ],
}


@dataclass
class NavidadDataset:
    households: list[Household]
    people: list[Person]
    preferences: list[Preference]
    journeys: list[JourneyStop]


def generate(cfg: GenerateConfig | None = None) -> NavidadDataset:
    cfg = cfg or GenerateConfig()
    households = generate_households(cfg)
    people = generate_people(cfg, households)
    preferences = generate_preferences(cfg, people)
    journeys = generate_journeys(cfg, households)
    return NavidadDataset(
        households=households,
        people=people,
        preferences=preferences,
        journeys=journeys,
    )


def _edge_ids(values: list) -> list:
    return values[:20] + values[-20:]


def checksum(ds: NavidadDataset) -> str:
    payload = {
        "counts": {
            "households": len(ds.households),
            "people": len(ds.people),
            "preferences": len(ds.preferences),
            "journeys": len(ds.journeys),
        },
        "ids": {
            "households": _edge_ids([h.household_id for h in ds.households]),
            "people": _edge_ids([p.person_id for p in ds.people]),
            "preferences": _edge_ids([p.person_id for p in ds.preferences]),
            "journeys": _edge_ids(
                [[s.year, s.campaign, s.route_id, s.stop_number] for s in ds.journeys]
            ),
        },
    }
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate the Navidad hackathon dataset")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=20260106)
    args = parser.parse_args(argv)
    ds = generate(GenerateConfig(scale=args.scale, seed=args.seed))
    print(
        f"households={len(ds.households)} people={len(ds.people)} "
        f"preferences={len(ds.preferences)} journeys={len(ds.journeys)} "
        f"checksum={checksum(ds)}"
    )


if __name__ == "__main__":
    main()
