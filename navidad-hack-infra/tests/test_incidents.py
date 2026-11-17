import re
import sys
import time
from pathlib import Path

import pytest

from road_network import (
    INCIDENT_CITIES,
    build_edges,
    city_bbox,
    city_id_offset,
    overpass_query,
    vertices_of,
)

SKILL_ROOT = Path(__file__).resolve().parents[1]
SQL = (SKILL_ROOT / "sql" / "incidents.sql").read_text()
INCIDENTS_DIR = SKILL_ROOT / "templates" / "incidents"


def _osm(ways, nodes):
    els = [{"type": "node", "id": i, "lon": x, "lat": y} for i, (x, y) in nodes.items()]
    els += [{"type": "way", "id": 100 + k, "nodes": w["nodes"], "tags": w["tags"]} for k, w in enumerate(ways)]
    return {"elements": els}


NODES = {1: (-3.700, 40.400), 2: (-3.699, 40.400), 3: (-3.698, 40.400), 4: (-3.699, 40.401), 5: (-3.697, 40.400)}


def test_ways_split_at_intersections_only():
    osm = _osm(
        [
            {"nodes": [1, 2, 3, 5], "tags": {"highway": "residential"}},  # 2 is shared, 3 is a shape point
            {"nodes": [2, 4], "tags": {"highway": "primary"}},
        ],
        NODES,
    )
    edges = build_edges(osm, "Madrid", start_id=10)
    pairs = sorted((e.source, e.target) for e in edges)
    assert pairs == [(1, 2), (2, 4), (2, 5)]
    assert [e.id for e in edges] == [10, 11, 12]
    long_edge = next(e for e in edges if (e.source, e.target) == (2, 5))
    assert len(long_edge.coords) == 3  # keeps the shape point
    assert set(vertices_of(edges)) == {1, 2, 4, 5}
    primary = next(e for e in edges if e.highway == "primary")
    assert primary.main and not long_edge.main


def test_oneway_and_reverse_oneway():
    osm = _osm(
        [
            {"nodes": [1, 2], "tags": {"highway": "residential", "oneway": "yes"}},
            {"nodes": [2, 3], "tags": {"highway": "residential", "oneway": "-1"}},
            {"nodes": [3, 5], "tags": {"highway": "residential"}},
            {"nodes": [5, 4], "tags": {"highway": "motorway"}},  # motorways are one-way by default
        ],
        NODES,
    )
    by = {(e.source, e.target): e for e in build_edges(osm, "Madrid")}
    assert by[(1, 2)].reverse_cost_s == -1
    assert (3, 2) in by and by[(3, 2)].reverse_cost_s == -1  # oneway=-1 is stored reversed
    assert by[(3, 5)].reverse_cost_s == by[(3, 5)].cost_s > 0
    assert by[(5, 4)].reverse_cost_s == -1


def test_speed_respects_maxspeed_and_links():
    osm = _osm(
        [
            {"nodes": [1, 2], "tags": {"highway": "primary"}},
            {"nodes": [2, 3], "tags": {"highway": "primary", "maxspeed": "30"}},
            {"nodes": [3, 5], "tags": {"highway": "primary_link"}},
        ],
        NODES,
    )
    e = {(x.source, x.target): x for x in build_edges(osm, "Madrid")}
    speed = lambda x: x.length_m / x.cost_s * 3.6  # noqa: E731
    assert round(speed(e[(1, 2)])) == 50
    assert round(speed(e[(2, 3)])) == 30
    assert round(speed(e[(3, 5)])) == 35


def test_bbox_and_query_cover_households():
    s, w, n, e = city_bbox("Valencia")
    assert s < 39.39 and n > 39.55 and w < -0.456 and e > -0.32  # measured household extent
    q = overpass_query((s, w, n, e))
    assert "highway" in q and "out body" in q and "residential" in q
    with pytest.raises(ValueError):
        city_bbox("Atlantis")
    offsets = [city_id_offset(c) for c in INCIDENT_CITIES]
    assert len(set(offsets)) == 3 and min(offsets) >= 10_000_000


def test_sql_is_idempotent_and_complete():
    assert "CREATE EXTENSION IF NOT EXISTS pgrouting" in SQL
    assert not re.search(r"CREATE TABLE (?!IF NOT EXISTS)", SQL)
    assert not re.search(r"CREATE FUNCTION", SQL.replace("CREATE OR REPLACE FUNCTION", ""))
    for fn in ("load_scenario", "refresh_costs", "simulate", "suggest", "sim_geojson", "set_minute",
               "snap_households", "mark_components"):
        assert f"FUNCTION navidad_geo.{fn}(" in SQL
    for sc in ("filomena_madrid", "dana_valencia", "cortes_barcelona"):
        assert f"'{sc}'" in SQL
    for pgr in ("pgr_dijkstraCost", "pgr_dijkstra(", "pgr_TSP", "pgr_dijkstraCostMatrix", "pgr_strongComponents"):
        assert pgr in SQL
    assert "OpenStreetMap" in SQL


# --- app module ---------------------------------------------------------------------

sys.path.insert(0, str(INCIDENTS_DIR))
import incidents  # noqa: E402


class FakeConn:
    def __init__(self, log):
        self.log = log

    def cursor(self, cursor_factory=None):
        return FakeCursor(self.log)

    def close(self):
        pass


class FakeCursor:
    def __init__(self, log):
        self.log = log
        self.rows = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        self.description = [("x",)]
        if "FROM navidad_geo.sim_suggestion" in sql:
            self.rows = [{"kind": "desvio", "route_id": 1, "household_id": None, "from_bringer": None,
                          "to_bringer": None, "detail": "{}"}]
        elif "set_minute" in sql:
            self.rows = [{"n": 3}]
        elif "apply_params" in sql:
            self.rows = [{"k": {"stops": 10, "late_stops": 2, "late_stops_without_params": 3,
                                "params": {"helpers": params[0], "boost": params[1]}}}]
        elif "simulate" in sql:
            self.rows = [{"k": {"stops": 10, "late_stops": 2}}]
        elif "suggest" in sql:
            self.rows = [{"k": {"helpers": 1}}]
        elif "sim_geojson" in sql:
            self.rows = [{"g": {"zones": []}}]
        elif "FROM navidad_geo.sim_suggestion" in sql:
            self.rows = [{"kind": "desvio", "route_id": 1, "household_id": None, "from_bringer": None,
                          "to_bringer": None, "detail": "{}"}]
        else:
            self.rows, self.description = [], None

    def executemany(self, sql, seq):
        self.log.append((sql, list(seq)))

    def fetchall(self):
        return self.rows


class FakeLakebase:
    def __init__(self):
        self.branches = ["sim-old-1", "sim-old-2", "sim-old-3"]
        self.created, self.deleted, self.log = [], [], []

    def create_branch(self, b):
        self.created.append(b)
        self.branches.append(b)

    def delete_branch(self, b):
        assert b.startswith("sim-")
        self.deleted.append(b)
        self.branches.remove(b)

    def sim_branches(self):
        return [{"branch": b, "created": f"2026-11-12T00:00:0{i}Z"} for i, b in enumerate(self.branches)]

    def connect(self, branch="production"):
        self.log.append(("connect", branch))
        return FakeConn(self.log)

    def user(self):
        return "sp"


def _wait(sims, branch):
    for _ in range(200):
        if sims.view(branch)["status"] != "running":
            return sims.view(branch)
        time.sleep(0.01)
    raise AssertionError("simulation did not finish")


def test_simulation_runs_on_a_new_branch_and_caps_branches():
    lb = FakeLakebase()
    sims = incidents.Simulations(lb, "navidad_test")
    branch = sims.start("dana_valencia", None)
    assert branch.startswith("sim-dana-")
    s = _wait(sims, branch)
    assert s["status"] == "done", s["error"]
    assert [p["phase"] for p in s["phases"]] == incidents.PHASES
    assert s["kpi"]["stops"] == 10 and s["suggest"]["helpers"] == 1
    assert lb.created == [branch]
    assert lb.deleted == ["sim-old-1"]  # 3 old + the new one → keep at most 3
    assert ("connect", "production") not in lb.log  # simulation never touches production
    minute_calls = [p for sql, p in lb.log if isinstance(sql, str) and "set_minute" in sql]
    assert minute_calls == [("dana_valencia", incidents.ALL_ZONES_MINUTE)]


def test_replay_minute_and_apply_writes_only_applied_plan():
    lb = FakeLakebase()
    sims = incidents.Simulations(lb, "navidad_test")
    branch = sims.start("cortes_barcelona", None)
    _wait(sims, branch)
    sims.rerun(branch, 20)
    s = _wait(sims, branch)
    assert s["minute"] == 20 and lb.created == [branch]  # rerun reuses the branch
    lb.log.clear()
    applied = sims.apply(branch)
    assert applied["rows"] == 1 and applied["by_kind"] == {"desvio": 1}
    writes = [sql for sql, _ in lb.log if isinstance(sql, str) and re.match(r"\s*(INSERT|DELETE|UPDATE)", sql)]
    assert writes and all("navidad_geo.applied_plan" in w for w in writes)


def test_rejects_unknown_scenario_and_parallel_runs():
    sims = incidents.Simulations(FakeLakebase(), "navidad_test")
    with pytest.raises(Exception):
        sims.start("atlantis", None)
    with pytest.raises(ValueError):
        incidents.Simulations(FakeLakebase(), "bad; drop")


def test_only_sim_branches_can_be_deleted():
    lb = incidents.Lakebase("navidad-x", client=object())
    with pytest.raises(ValueError):
        lb.delete_branch("production")


def test_page_has_attribution_and_endpoints():
    html = (INCIDENTS_DIR / "static" / "planner.html").read_text()
    assert "OpenStreetMap" in html and 'lang="es"' in html
    for path in ("/api/incidents/scenarios", "/api/sim", "/minute", "/apply"):
        assert path in html
    assert "inspirados en" in html


def test_prebuilt_graph_roundtrip_and_shipped_files(tmp_path):
    from road_network import read_edges, save_edges

    osm = _osm([{"nodes": [1, 2, 3, 5], "tags": {"highway": "residential", "oneway": "yes"}},
                {"nodes": [2, 4], "tags": {"highway": "primary"}}], NODES)
    edges = build_edges(osm, "Valencia", start_id=7)
    path = tmp_path / "valencia_osm.csv.gz"
    save_edges(path, edges, "2026-11-12T00:00:00Z")
    back, ts = read_edges(path, "Valencia")
    assert ts == "2026-11-12T00:00:00Z"
    assert [(e.id, e.source, e.target, e.main, e.reverse_cost_s) for e in back] == \
           [(e.id, e.source, e.target, e.main, e.reverse_cost_s) for e in edges]
    assert len(back[1].coords) == len(edges[1].coords)
    import gzip
    for city in ("madrid", "valencia", "barcelona"):
        shipped = SKILL_ROOT / "data" / f"{city}_osm.csv.gz"
        assert shipped.exists()
        with gzip.open(shipped, "rt") as f:
            assert "OpenStreetMap contributors, ODbL" in f.readline()


def test_provision_incidents_prefers_prebuilt_and_loads_matching_scenarios(monkeypatch):
    import provision_incidents as pi

    executed = []

    class Cur:
        description = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            executed.append((sql, params))
            self.description = [("x",)] if sql.lstrip().startswith("SELECT navidad_geo.") else None

        def fetchall(self):
            return [(1,)]

        def copy_expert(self, sql, buf):
            executed.append((sql, None))

    class Conn:
        autocommit = True

        def cursor(self):
            return Cur()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(pi, "_pg_connect", lambda client, project_id: Conn())

    def no_fetch(*a, **k):
        raise AssertionError("must not call Overpass when a prebuilt graph exists")

    report = pi.provision_incidents(project_id="navidad-x", schema="navidad_x", client=object(),
                                    cities=("Valencia",), fetch=no_fetch, log=lambda m: None)
    assert report["cities"]["Valencia"]["source"] == "prebuilt"
    assert report["scenarios"] == ["dana_valencia"]
    assert any(p == ("dana_valencia",) for _, p in executed)
    assert not any(p == ("filomena_madrid",) for _, p in executed)


def test_app_template_with_incidents_is_wired(tmp_path, monkeypatch):
    import provision_incidents as pi
    from fastapi.testclient import TestClient

    dest = pi.app_template_with_incidents(tmp_path / "app")
    assert (dest / "incidents.py").exists() and (dest / "static" / "planner.html").exists()
    assert "mount_incidents(app, STATIC_DIR)" in (dest / "app.py").read_text()
    index = (dest / "static" / "index.html").read_text()
    assert "/static/nav.js" in index and '"/planner"' in (dest / "static" / "nav.js").read_text()  # left rail
    pi.app_template_with_incidents(dest)  # idempotent
    assert (dest / "app.py").read_text().count("mount_incidents(app, STATIC_DIR)") == 1
    monkeypatch.syspath_prepend(str(dest))
    for mod in ("app", "incidents"):
        sys.modules.pop(mod, None)
    import importlib
    app_mod = importlib.import_module("app")
    client = TestClient(app_mod.create_app(execute_sql=lambda q: [], complete=lambda p: ""))
    assert client.get("/planner").status_code == 200
    assert client.get("/static/planner.html").status_code == 200
    assert client.get("/incidencias", follow_redirects=False).headers["location"] == "/planner"
    sys.modules.pop("app", None)


def test_workshop_template_has_map_home_incidents_and_plumbing(tmp_path, monkeypatch):
    import importlib
    import provision_incidents as pi
    from fastapi.testclient import TestClient

    dest = pi.app_template_for_workshop(tmp_path / "app")
    pi.app_template_for_workshop(dest)  # idempotent
    src = (dest / "app.py").read_text()
    assert src.count("mount_map(app, run_sql)") == 1 and src.count("mount_incidents(app, STATIC_DIR)") == 1
    monkeypatch.syspath_prepend(str(dest))
    for mod in ("app", "incidents", "routes_map"):
        sys.modules.pop(mod, None)
    calls = []

    def run_sql(query, params=None):
        calls.append(query)
        if "min(year)" in query:
            return [{"min_year": 2021, "max_year": 2025}]
        if "GROUP BY city, autonomous_community" in query and "avg(lat)" in query:
            return [{"city": "Valencia", "autonomous_community": "Comunitat Valenciana", "timezone": "Europe/Madrid",
                     "lat": 39.47, "lon": -0.37, "households": 10}]
        return []

    app_mod = importlib.import_module("app")
    client = TestClient(app_mod.create_app(execute_sql=run_sql, complete=lambda p: ""))
    home = client.get("/")
    assert home.status_code == 200 and "Mapa de rutas" in home.text and "/static/nav.js" in home.text  # pages via the left rail
    assert "Taller de Navidad" in client.get("/plumbing").text
    assert client.get("/planner").status_code == 200
    opts = client.get("/api/route/options").json()
    assert opts["years"] == [2021, 2022, 2023, 2024, 2025] and opts["cities"][0]["city"] == "Valencia"
    assert client.get("/api/route", params={"year": 2025, "city": "Valencia", "campaign": "x"}).status_code == 400
    assert client.get("/api/route", params={"year": 2025, "city": "Valencia"}).json()["routes"] == []
    for mod in ("app", "routes_map"):
        sys.modules.pop(mod, None)


def test_workshop_map_works_with_the_real_execute_sql(tmp_path, monkeypatch):
    """Regression: the map passes (query, params); the template's executor must accept both."""
    import importlib
    import provision_incidents as pi
    from fastapi.testclient import TestClient

    dest = pi.app_template_for_workshop(tmp_path / "app")
    monkeypatch.syspath_prepend(str(dest))
    for mod in ("app", "incidents", "routes_map"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    for k, v in {"NAVIDAD_PG_HOST": "h", "NAVIDAD_PG_USER": "u", "NAVIDAD_PG_PASSWORD": "p", "NAVIDAD_SCHEMA": "navidad_x"}.items():
        monkeypatch.setenv(k, v)
    seen = []

    class Cur:
        description = [("x",)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, query, params=None):
            seen.append(params)

        def fetchall(self):
            return []

    class Conn:
        autocommit = True

        def cursor(self, cursor_factory=None):
            return Cur()

        def close(self):
            pass

    monkeypatch.setattr(app_mod.psycopg2, "connect", lambda **kw: Conn())
    client = TestClient(app_mod.create_app(complete=lambda p: ""))
    r = client.get("/api/route", params={"year": 2025, "city": "Valencia", "campaign": "nochebuena"})
    assert r.status_code == 200, r.text
    assert (2025, "nochebuena", "Valencia") in seen
    for mod in ("app", "routes_map", "incidents"):
        sys.modules.pop(mod, None)


def test_planner_params_are_validated_and_replan_needs_a_simulation():
    import pytest as _pytest
    from fastapi import HTTPException
    from incidents import Simulations, _params
    assert _params(2, 1.25) == (2, 1.25)
    for bad in ((5, 1.0), (-1, 1.0), (0, 0.9), (0, 1.6)):
        with _pytest.raises(HTTPException):
            _params(*bad)

    class LB:
        def sim_branches(self):
            return []
    sims = Simulations(LB(), "navidad_x")
    sims.state["sim-dana-1"] = {"branch": "sim-dana-1", "scenario": "dana_valencia", "status": "done", "kpi": None,
                                "phases": [], "helpers": 0, "boost": 1.0}
    with _pytest.raises(HTTPException):
        sims.replan("sim-dana-1", 1, 1.2)          # no kpi yet → must simulate first


def test_apply_params_sql_scales_load_by_helpers_and_travel_share():
    sql = (INCIDENTS_DIR.parents[1] / "sql" / "incidents.sql").read_text()
    fn = sql[sql.index("CREATE OR REPLACE FUNCTION navidad_geo.apply_params"):]
    assert "load_raw * (1 - tf + tf / b) / (1 + helper_capacity * h)" in fn
    assert "late_stops_without_params" in fn and "ADD COLUMN IF NOT EXISTS load_raw" in sql


def test_plan_without_scenario_moves_the_end_of_the_night(monkeypatch):
    import datetime as _dt
    from incidents import PlanRequest, plan_night
    t0 = _dt.datetime(2025, 12, 24, 21, 0, tzinfo=_dt.timezone.utc)

    class Conn:
        def close(self):
            pass

    class LB:
        def connect(self, branch):
            assert branch == "production"   # read-only plan: no simulation branch
            return Conn()

    def fake_query(conn, sql, params=None):
        if "max(year)" in sql:
            return [{"y": 2025}]
        if "GROUP BY j.route_id" in sql:
            return [{"route_id": 1, "bringer": "papa_noel", "stops": 1000, "start_utc": t0,
                     "end_utc": t0 + _dt.timedelta(hours=10), "dwell_min": 5000, "timezone": "Europe/Madrid"}]
        return [{"lat": 39.4, "lon": -0.37, "bringer": "papa_noel"}]
    monkeypatch.setitem(plan_night.__globals__, "_query", fake_query)
    base = plan_night(LB(), "navidad_x", PlanRequest(city="Valencia"))
    assert base["load"] == 1.0 and base["hours_saved"] == 0
    p = plan_night(LB(), "navidad_x", PlanRequest(city="Valencia", helpers=1, boost=1.2))
    load = (1 - 0.62 + 0.62 / 1.2) / (1 + 0.6)
    assert abs(p["load"] - round(load, 3)) < 1e-9 and p["hours_saved"] == round(10 * (1 - load), 2)
    assert p["routes"][0]["stops_per_carrier"] == 500 and p["mode"] == "plan"


def test_tsp_keeps_one_component_and_reports_skips():
    body = SQL[SQL.index("-- resecuenciar"):SQL.index("'resequence_skipped', tsp_skip")]
    # one component anchored on a single node (ties → lowest id), not «all nodes with max reach»
    assert "ORDER BY count(*) DESC, start_vid LIMIT 1" in body
    assert "max(x.n_reach)" not in body
    # the cost matrix is inside the guarded block, and skips are visible (KPI + WARNING), not a NOTICE
    begin = body.index("BEGIN")
    assert body.index("pgr_dijkstraCostMatrix") > begin
    assert "RAISE NOTICE" not in body and "RAISE WARNING" in body
    assert "tsp_skip := format('error %s: %s', SQLSTATE, SQLERRM)" in body


def test_map_expensive_revisit_threshold_fits_hilbert_routes():
    sys.path.insert(0, str(SKILL_ROOT / "templates" / "map"))
    from routes_map import EXPENSIVE_KM, classify_revisit

    assert EXPENSIVE_KM == 1.0
    assert classify_revisit(25, 1.0) == "vuelta_cara"
    assert classify_revisit(1, 0.2) == "reintento_inmediato"
    assert classify_revisit(25, 0.4) == "recuperacion_cercana"
    html = (SKILL_ROOT / "templates" / "map" / "static" / "map.html").read_text()
    assert "barrido por longitud" not in html
