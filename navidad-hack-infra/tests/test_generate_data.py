from collections import Counter
from datetime import datetime

from generate_data import (
    CAMPAIGN_BRINGERS,
    CANARIAS_BBOX,
    CITIES_BY_NAME,
    CITY_TOUR_ORDER,
    ES_CITIES,
    PENINSULA_BALEARES_BBOX,
    ROUTE_IDS,
    GenerateConfig,
    checksum,
    generate,
    generate_households,
    generate_journeys,
    generate_people,
    generate_preferences,
    on_land,
)


def _in_bbox(lat, lon, bbox):
    return bbox["lat_min"] <= lat <= bbox["lat_max"] and bbox["lon_min"] <= lon <= bbox["lon_max"]


def test_household_count_respects_scale():
    rows = generate_households(GenerateConfig(scale=0.0001))  # 50 households
    assert len(rows) == 50


def test_tour_order_covers_every_city_once():
    assert sorted(CITY_TOUR_ORDER) == sorted(c.name for c in ES_CITIES)
    assert CITY_TOUR_ORDER[-2:] == ["Las Palmas de Gran Canaria", "Santa Cruz de Tenerife"]


def test_coords_are_spain_near_city_and_on_land():
    rows = generate_households(GenerateConfig(seed=20260106, scale=0.004))
    for h in rows:
        city = CITIES_BY_NAME[h.city]
        box = CANARIAS_BBOX if city.autonomous_community == "Canarias" else PENINSULA_BALEARES_BBOX
        assert _in_bbox(h.lat, h.lon, box), h
        assert abs(h.lat - city.lat) <= 0.08
        assert abs(h.lon - city.lon) <= 0.08
        assert on_land(city, h.lat, h.lon), h


def test_land_mask_rejects_open_sea():
    barcelona = CITIES_BY_NAME["Barcelona"]
    assert on_land(barcelona, barcelona.lat, barcelona.lon)
    assert not on_land(barcelona, 41.33, 2.24)  # Mediterranean off Barceloneta
    madrid = CITIES_BY_NAME["Madrid"]
    assert on_land(madrid, madrid.lat + 0.07, madrid.lon - 0.07)


def test_household_geography_columns_follow_city():
    rows = generate_households(GenerateConfig(scale=0.002))
    for h in rows:
        city = CITIES_BY_NAME[h.city]
        assert h.postcode_area == city.postcode_area and len(h.postcode_area) == 2
        assert h.province == city.province
        assert h.autonomous_community == city.autonomous_community
        expected_tz = "Atlantic/Canary" if city.autonomous_community == "Canarias" else "Europe/Madrid"
        assert h.timezone == expected_tz
        assert h.label.startswith("Familia ")


def test_households_are_not_a_single_stacked_pin():
    rows = generate_households(GenerateConfig(seed=20260106, scale=0.001))
    madrid = [h for h in rows if h.city == "Madrid"]
    assert len({(round(h.lat, 4), round(h.lon, 4)) for h in madrid}) > 10


def test_regional_gift_bringers():
    rows = generate_households(GenerateConfig(scale=0.02))
    by_region = Counter((h.autonomous_community, h.nochebuena_bringer) for h in rows)
    assert by_region[("País Vasco", "olentzero")] > 0
    assert by_region[("Cataluña", "tio")] > 0
    assert by_region[("Galicia", "apalpador")] > 0
    for h in rows:
        if h.nochebuena_bringer in ("olentzero",):
            assert h.autonomous_community in ("País Vasco", "Comunidad Foral de Navarra")
        if h.nochebuena_bringer == "tio":
            assert h.autonomous_community == "Cataluña"
        if h.nochebuena_bringer == "apalpador":
            assert h.autonomous_community == "Galicia"
        assert h.visits_reyes or h.nochebuena_bringer != "none"


def test_people_count_and_roles():
    cfg = GenerateConfig(scale=0.0001)
    hh = generate_households(cfg)
    people = generate_people(cfg, hh)
    assert len(people) == cfg.scaled_people
    assert {p.role for p in people} <= {"child", "adult"}
    assert all(p.age_years >= 0 for p in people)
    by_hh = Counter(p.household_id for p in people)
    assert all(1 <= n <= 8 for n in by_hh.values())


def test_preferences_one_to_one():
    cfg = GenerateConfig(scale=0.0001)
    hh = generate_households(cfg)
    people = generate_people(cfg, hh)
    prefs = generate_preferences(cfg, people)
    assert [p.person_id for p in prefs] == [p.person_id for p in people]
    assert all(0 <= x.naughty_nice_score <= 100 for x in prefs)
    assert {x.favourite_king for x in prefs} <= {"melchor", "gaspar", "baltasar"}


def test_visits_match_household_traditions_plus_rare_revisits():
    cfg = GenerateConfig(scale=0.0004, n_years=2)
    hh = generate_households(cfg)
    stops = generate_journeys(cfg, hh)
    base = [s for s in stops if not s.is_revisit]
    expected = sum((h.nochebuena_bringer != "none") + h.visits_reyes for h in hh) * cfg.n_years
    assert len(base) == expected
    by_id = {h.household_id: h for h in hh}
    for s in base:
        h = by_id[s.household_id]
        assert s.bringer in CAMPAIGN_BRINGERS[s.campaign]
        if s.campaign == "nochebuena":
            assert s.bringer == h.nochebuena_bringer
        else:
            assert h.visits_reyes
    revisit_frac = sum(s.is_revisit for s in stops) / len(stops)
    assert 0 < revisit_frac < 0.02


def test_journey_primary_key_unique_and_routes_stay_in_one_city():
    cfg = GenerateConfig(scale=0.0004, n_years=2)
    hh = generate_households(cfg)
    by_id = {h.household_id: h for h in hh}
    stops = generate_journeys(cfg, hh)
    keys = [(s.year, s.campaign, s.route_id, s.stop_number) for s in stops]
    assert len(keys) == len(set(keys))
    route_city = {rid: city for (_, city, _), rid in ROUTE_IDS.items()}
    for s in stops:
        assert by_id[s.household_id].city == route_city[s.route_id]


def test_campaign_nights_and_timezones():
    cfg = GenerateConfig(scale=0.002, n_years=1)
    hh = generate_households(cfg)
    stops = generate_journeys(cfg, hh)
    by_id = {h.household_id: h for h in hh}
    for s in stops:
        t = datetime.fromisoformat(s.arrived_at_utc.replace("Z", "+00:00"))
        canary = by_id[s.household_id].timezone == "Atlantic/Canary"
        local_hour = (t.hour + (0 if canary else 1)) % 24
        if s.campaign == "nochebuena":
            assert (t.month, t.day) in ((12, 24), (12, 25)) and t.year == s.year
            assert local_hour >= 22 or local_hour < 8
        else:
            assert (t.year, t.month) == (s.year + 1, 1) and t.day in (5, 6)
            assert 1 <= local_hour < 9


def test_same_seed_same_checksum():
    a = generate(GenerateConfig(scale=0.0001, seed=20260106))
    b = generate(GenerateConfig(scale=0.0001, seed=20260106))
    assert checksum(a) == checksum(b)
    assert len(a.households) == len(b.households) == 50


def test_column_order_matches_dictionary():
    from test_data_dictionary import REQUIRED_TABLES
    from generate_data import COLUMN_ORDER
    assert COLUMN_ORDER == REQUIRED_TABLES


def test_routes_follow_a_space_filling_curve_not_a_longitude_sweep():
    import math
    from generate_data import GenerateConfig, generate
    ds = generate(GenerateConfig(scale=0.02))
    hh = {h.household_id: h for h in ds.households}

    def legs(year, campaign, route_id):
        stops = sorted((s for s in ds.journeys if s.year == year and s.campaign == campaign and s.route_id == route_id),
                       key=lambda s: s.stop_number)
        pts = [(hh[s.household_id].lat, hh[s.household_id].lon) for s in stops]
        out = []
        for (a, b), (c, d) in zip(pts, pts[1:]):
            out.append(6371000 * math.hypot(math.radians(c - a), math.radians(d - b) * math.cos(math.radians(a))))
        return sorted(out)

    from collections import Counter
    year = ds.journeys[0].year
    biggest = Counter((s.campaign, s.route_id) for s in ds.journeys if s.year == year).most_common(1)[0][0]
    lg = legs(year, *biggest)
    assert len(lg) > 500
    assert lg[len(lg) // 2] < 600            # median leg: hundreds of metres, not kilometres
    assert lg[int(0.95 * len(lg))] < 2500    # no city-wide north–south jumps


def test_hilbert_index_known_order_1_and_2():
    from generate_data import hilbert_index
    assert [hilbert_index(x, y, order=1) for x, y in [(0, 0), (0, 1), (1, 1), (1, 0)]] == [0, 1, 2, 3]
    assert [hilbert_index(x, y, order=2) for x, y in [(0, 0), (1, 0), (1, 1), (0, 1), (0, 2)]] == [0, 1, 2, 3, 4]


def test_hilbert_index_is_a_bijection_with_unit_steps():
    from generate_data import hilbert_index
    order = 4
    n = 1 << order
    by_d = {hilbert_index(x, y, order=order): (x, y) for x in range(n) for y in range(n)}
    assert sorted(by_d) == list(range(n * n))           # every cell once
    for d in range(n * n - 1):                          # consecutive cells are grid neighbours
        (x0, y0), (x1, y1) = by_d[d], by_d[d + 1]
        assert abs(x0 - x1) + abs(y0 - y1) == 1


def test_hilbert_sort_key_breaks_ties_by_household_id():
    from types import SimpleNamespace
    from generate_data import hilbert_sort_key
    rows = [SimpleNamespace(household_id=i, lat=40.0, lon=-3.0) for i in (7, 3, 5)]
    rows.append(SimpleNamespace(household_id=1, lat=40.1, lon=-2.9))
    key = hilbert_sort_key(rows)
    same = sorted(rows[:3], key=key)
    assert [h.household_id for h in same] == [3, 5, 7]
    assert hilbert_sort_key([])(None) == 0
