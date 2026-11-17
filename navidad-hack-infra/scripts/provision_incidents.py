"""Optional step 6: the incident hack track (Filomena · DANA · cortes de calle).

Run after the four-step pipeline, as the Lakebase project owner, on the production
branch. Idempotent; reruns replace the road graph of the cities it loads.

  1. CREATE EXTENSION postgis, pgrouting + schema navidad_geo (sql/incidents.sql)
  2. per city (Madrid, Barcelona, Valencia): OpenStreetMap drive network from Overpass
     → navidad_geo.ways/vertices, largest strongly connected component, household snap
  3. the three scenario presets, zones INACTIVE on production (simulations activate them
     on a child branch, so production road costs stay at baseline)
  4. grants for the app's service principal on navidad_geo, plus CAN_MANAGE on the
     Lakebase project so the app can create and delete simulation branches

Road data © OpenStreetMap contributors (ODbL). The app must show that attribution.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from deploy_app import _pg_connect
from road_network import INCIDENT_CITIES, build_edges, city_bbox, city_id_offset, fetch_osm, load_edges, read_edges

SKILL_ROOT = Path(__file__).resolve().parents[1]
SQL_FILE = SKILL_ROOT / "sql" / "incidents.sql"
SCENARIOS = {"filomena_madrid": "Madrid", "dana_valencia": "Valencia", "cortes_barcelona": "Barcelona"}
GRAPH_DIR = SKILL_ROOT / "data"  # pre-built <city>_osm.csv.gz (ODbL); used before Overpass
OVERPASS_PAUSE_S = 15  # be polite to the shared public Overpass server


def _sql(conn: Any, query: str, params: tuple | None = None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else []


def _app_sp(client: Any, app_name: str) -> str:
    app = client.apps.get(name=app_name)
    sp = getattr(app, "service_principal_client_id", None)
    if not sp:
        raise RuntimeError(f"app {app_name} has no service principal yet; deploy it first")
    return sp


def grant_app(conn: Any, client: Any, *, project_id: str, app_name: str) -> str:
    """Postgres grants on navidad_geo + project permission to manage simulation branches."""
    sp = _app_sp(client, app_name)
    role = '"' + sp.replace('"', "") + '"'
    for stmt in (
        f"GRANT USAGE ON SCHEMA navidad_geo TO {role}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA navidad_geo TO {role}",
        f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA navidad_geo TO {role}",
        f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA navidad_geo TO {role}",
    ):
        _sql(conn, stmt)
    client.api_client.do(
        "PATCH",
        f"/api/2.0/permissions/database-projects/{project_id}",
        body={"access_control_list": [{"service_principal_name": sp, "permission_level": "CAN_MANAGE"}]},
    )
    return sp


def provision_incidents(
    *,
    project_id: str,
    schema: str,
    client: Any,
    app_name: str | None = None,
    cities: tuple[str, ...] = INCIDENT_CITIES,
    osm_cache_dir: str | Path | None = None,
    graph_dir: str | Path | None = GRAPH_DIR,
    fetch: Callable[..., dict[str, Any]] = fetch_osm,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    conn = _pg_connect(client, project_id)
    if conn is None:
        raise RuntimeError("cannot connect to the Lakebase production branch as the current user")
    report: dict[str, Any] = {"cities": {}}
    try:
        _sql(conn, SQL_FILE.read_text())
        log("navidad_geo schema + postgis/pgrouting ready")
        cache = Path(osm_cache_dir) if osm_cache_dir else None
        for i, city in enumerate(cities):
            t0 = time.time()
            prebuilt = Path(graph_dir) / f"{city.lower()}_osm.csv.gz" if graph_dir else None
            cached = cache / f"{city}.json" if cache else None
            if prebuilt and prebuilt.exists():
                edges, osm_ts = read_edges(prebuilt, city)
                source = "prebuilt"
            else:
                if cached and cached.exists():
                    osm = json.loads(cached.read_text())
                else:
                    if i:
                        time.sleep(OVERPASS_PAUSE_S)
                    osm = fetch(city_bbox(city))
                    if cached:
                        cached.parent.mkdir(parents=True, exist_ok=True)
                        cached.write_text(json.dumps(osm))
                edges = build_edges(osm, city, city_id_offset(city))
                osm_ts = osm.get("osm3s", {}).get("timestamp_osm_base", "")
                source = "overpass"
            conn.autocommit = False
            counts = load_edges(conn, city, edges, osm_ts)
            counts["source"] = source
            conn.commit()
            conn.autocommit = True
            counts["main_component"] = _sql(conn, "SELECT navidad_geo.mark_components(%s)", (city,))[0][0]
            counts["households"] = _sql(conn, "SELECT navidad_geo.snap_households(%s, %s)", (schema, city))[0][0]
            counts["seconds"] = round(time.time() - t0, 1)
            report["cities"][city] = counts
            log(f"{city}: {counts}")
        loaded = [sc for sc, c in SCENARIOS.items() if c in cities]
        for sc in loaded:
            _sql(conn, "SELECT navidad_geo.load_scenario(%s)", (sc,))
        _sql(conn, "UPDATE navidad_geo.zone SET active = false")
        for city in cities:
            _sql(conn, "SELECT navidad_geo.refresh_costs(%s)", (city,))
        _sql(conn, "TRUNCATE navidad_geo.sim_leg, navidad_geo.sim_route, navidad_geo.sim_suggestion, navidad_geo.sim_kpi")
        _sql(conn, "ANALYZE navidad_geo.ways; ANALYZE navidad_geo.vertices; ANALYZE navidad_geo.household_node")
        report["scenarios"] = loaded
        if app_name:
            report["app_service_principal"] = grant_app(conn, client, project_id=project_id, app_name=app_name)
            log(f"grants for {app_name} done")
    finally:
        conn.close()
    return report


INCIDENTS_TEMPLATE = SKILL_ROOT / "templates" / "incidents"
APP_TEMPLATE = SKILL_ROOT / "templates" / "app"
_STATIC_MOUNT = '    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")\n'
_MOUNT_INCIDENTS = (
    "    # Planner (incident hack track): /planner + /api/sim* (simulations on Lakebase branches).\n"
    "    from incidents import mount_incidents\n\n"
    "    mount_incidents(app, STATIC_DIR)\n\n"
)


def app_template_with_incidents(dest: str | Path, base: str | Path = APP_TEMPLATE) -> Path:
    """Plumbing app template + the incident module, wired, in ``dest`` (pass as deploy_app source_template).

    One deploy then serves both the plumbing page (/) and /planner (old /incidencias redirects). The page needs
    navidad_geo at request time only, so provision_incidents can run after the deploy.
    """
    import shutil

    dest = Path(dest)
    shutil.copytree(base, dest, dirs_exist_ok=True)
    shutil.copy2(INCIDENTS_TEMPLATE / "incidents.py", dest / "incidents.py")
    shutil.copy2(INCIDENTS_TEMPLATE / "static" / "planner.html", dest / "static" / "planner.html")
    (dest / "static" / "incidencias.html").unlink(missing_ok=True)  # renamed to planner.html
    app_py = dest / "app.py"
    src = app_py.read_text()
    if "mount_incidents" not in src:
        if _STATIC_MOUNT not in src:
            raise RuntimeError("app.py does not serve /static; cannot wire the incident module")
        app_py.write_text(src.replace(_STATIC_MOUNT, _MOUNT_INCIDENTS + _STATIC_MOUNT, 1))
    return add_nav(dest)


CONTROL_TEMPLATE = SKILL_ROOT / "templates" / "control"
_MOUNT_CONTROL = (
    "    # Real-time control center: /control + /api/control/* (reads Lakebase navidad_live).\n"
    "    # The incident agent reuses complete_from_gateway with its own model and a bigger token budget.\n"
    "    from control import AGENT_MAX_TOKENS, mount_control\n\n"
    "    agent_model = os.environ.get(\"NAVIDAD_AGENT_MODEL\") or os.environ.get(\"NAVIDAD_MODEL\", DEFAULT_MODEL)\n"
    "    mount_control(app, STATIC_DIR, lambda prompt: complete_from_gateway(agent_model, prompt, max_tokens=AGENT_MAX_TOKENS))\n\n"
)
_HELPER_SIG = "def complete_from_gateway(model: str, prompt: str) -> str:"
_HELPER_SIG_NEW = "def complete_from_gateway(model: str, prompt: str, max_tokens: int = 64) -> str:"


def _patch_gateway_helper(src: str) -> str:
    """Give the app's gateway helper a max_tokens parameter (the ping keeps 64)."""
    if _HELPER_SIG_NEW in src:
        return src
    if _HELPER_SIG not in src or src.count('"max_tokens": 64,') != 1:
        raise RuntimeError("app.py has an unexpected complete_from_gateway; cannot wire the control-center agent")
    src = src.replace(_HELPER_SIG, _HELPER_SIG_NEW, 1).replace('"max_tokens": 64,', '"max_tokens": max_tokens,', 1)
    return src.replace('payload["max_tokens"] = 256', 'payload["max_tokens"] = max(256, max_tokens)', 1)


NAV_PAGES = ("index.html", "map.html", "planner.html", "incidencias.html", "control.html")
_NAV_CSS = '<link rel="stylesheet" href="/static/nav.css" />'
_NAV_JS = '<script src="/static/nav.js"></script>'
_PAGE_LINK = re.compile(r'\s*<a href="/(?:control|incidencias|planner|plumbing)?"[^>]*>[^<]*</a>')


def add_nav(dest: str | Path) -> Path:
    """Left navigation rail (Mapa de rutas · Centro de control · Incidencias · Diagnóstico) on every page:
    copies static/nav.{css,js}, includes them and removes the old page links from the headers. Idempotent."""
    import shutil

    dest = Path(dest)
    for f in ("nav.css", "nav.js"):
        shutil.copy2(APP_TEMPLATE / "static" / f, dest / "static" / f)
    for page in NAV_PAGES:
        f = dest / "static" / page
        if not f.exists():
            continue
        html = f.read_text()
        if _NAV_CSS not in html:
            html = html.replace("</head>", f"{_NAV_CSS}\n</head>", 1)
        if _NAV_JS not in html:
            html = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + "\n" + _NAV_JS, html, count=1)
        head_end = html.find("</header>")
        if head_end != -1:  # page links live in the (first) header; leave other anchors alone
            start = html.rfind("<header", 0, head_end)
            header = _PAGE_LINK.sub("", html[start:head_end])
            header = re.sub(r'\s*<span class="links">\s*</span>', "", header)
            html = html[:start] + header + html[head_end:]
        f.write_text(html)
    return dest


SEARCH_TEMPLATE = SKILL_ROOT / "templates" / "search"
_SEARCH_CSS = '<link rel="stylesheet" href="/static/search.css" />'
_SEARCH_HOOK = '<script src="/static/search-hook.js"></script>'
_SEARCH_JS = '<script src="/static/search.js" defer></script>'
_LEAFLET_JS = 'leaflet.js"></script>'
_MOUNT_SEARCH = (
    "    # Bonus «Búsqueda»: search bar on every page (Lakebase Search over families and crews).\n"
    "    from search import mount_search\n\n"
    "    mount_search(app)\n\n"
)


def add_search(dest: str | Path) -> Path:
    """Search bar on every page + /api/search*. The bar hides itself until the index exists
    (provision_search / notebook 08_busqueda). Idempotent; needs the control-center module."""
    import shutil

    dest = Path(dest)
    shutil.copy2(SEARCH_TEMPLATE / "search.py", dest / "search.py")
    for f in ("search.js", "search-hook.js", "search.css"):
        shutil.copy2(SEARCH_TEMPLATE / "static" / f, dest / "static" / f)
    for page in NAV_PAGES:
        f = dest / "static" / page
        if not f.exists():
            continue
        html = f.read_text()
        if _SEARCH_CSS not in html:
            html = html.replace("</head>", f"{_SEARCH_CSS}\n</head>", 1)
        if _SEARCH_HOOK not in html and _LEAFLET_JS in html:
            html = html.replace(_LEAFLET_JS, f"{_LEAFLET_JS}\n{_SEARCH_HOOK}", 1)
        if _SEARCH_JS not in html:
            html = html.replace("</body>", f"{_SEARCH_JS}\n</body>", 1)
        f.write_text(html)
    app_py = dest / "app.py"
    src = app_py.read_text()
    if "mount_search" not in src:
        if _STATIC_MOUNT not in src:
            raise RuntimeError("app.py does not serve /static; cannot wire the search bar")
        app_py.write_text(src.replace(_STATIC_MOUNT, _MOUNT_SEARCH + _STATIC_MOUNT, 1))
    return dest


def add_control_center(dest: str | Path) -> Path:
    """Wire the control-center module into an app source folder (idempotent).

    The page reads navidad_live at request time only, so provision_live can run later."""
    import shutil

    dest = Path(dest)
    shutil.copy2(CONTROL_TEMPLATE / "control.py", dest / "control.py")
    shutil.copy2(CONTROL_TEMPLATE / "static" / "control.html", dest / "static" / "control.html")
    app_py = dest / "app.py"
    src = app_py.read_text()
    if "mount_control" not in src:
        if _STATIC_MOUNT not in src:
            raise RuntimeError("app.py does not serve /static; cannot wire the control center")
        src = src.replace(_STATIC_MOUNT, _MOUNT_CONTROL + _STATIC_MOUNT, 1)
    old = ("    from control import mount_control\n\n"
           "    mount_control(app, STATIC_DIR, run_complete)\n")  # first version of the wiring
    if old in src:
        start = src.index("    # Real-time control center")
        src = src[:start] + _MOUNT_CONTROL.rstrip("\n") + "\n" + src[src.index(old) + len(old):]
    app_py.write_text(_patch_gateway_helper(src))
    add_nav(dest)
    return add_search(dest)


MAP_TEMPLATE = SKILL_ROOT / "templates" / "map"
_INDEX_ROUTE = (
    '    @app.get("/")\n'
    "    def index() -> FileResponse:\n"
    '        return FileResponse(STATIC_DIR / "index.html")\n'
)
_MAP_ROUTES = (
    "    # Route map track: «Mapa de rutas» is the home page; the plumbing page moves to /plumbing.\n"
    "    from routes_map import mount_map\n\n"
    "    mount_map(app, run_sql)\n\n"
    '    @app.get("/")\n'
    "    def index() -> FileResponse:\n"
    '        return FileResponse(STATIC_DIR / "map.html")\n\n'
    '    @app.get("/plumbing")\n'
    "    def plumbing() -> FileResponse:\n"
    '        return FileResponse(STATIC_DIR / "index.html")\n'
)


def app_template_for_workshop(dest: str | Path, base: str | Path = APP_TEMPLATE) -> Path:
    """Plumbing app + «Mapa de rutas» (home page) + Incidencias, wired, in ``dest``.

    What the reference workbench looks like after a hack session; pass as deploy_app's
    source_template so a from-zero app opens on the map.
    """
    import shutil

    dest = app_template_with_incidents(dest, base)
    shutil.copy2(MAP_TEMPLATE / "routes_map.py", dest / "routes_map.py")
    shutil.copy2(MAP_TEMPLATE / "static" / "map.html", dest / "static" / "map.html")
    app_py = dest / "app.py"
    src = app_py.read_text()
    if "mount_map" not in src:
        if _INDEX_ROUTE not in src:
            raise RuntimeError("app.py has no default index route; cannot install the map as home page")
        app_py.write_text(src.replace(_INDEX_ROUTE, _MAP_ROUTES, 1))
    return add_control_center(dest)
