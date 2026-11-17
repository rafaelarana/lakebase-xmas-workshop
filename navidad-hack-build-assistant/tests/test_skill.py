from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DICT_PATH = ROOT / "references" / "data-dictionary.md"

REQUIRED_TABLES = {
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


def test_skill_uses_memory_then_locates_workbench():
    text = (ROOT / "SKILL.md").read_text()
    assert "name: navidad-hack-build-assistant" in text
    assert "write_uc" not in text
    assert "provision_lakebase" not in text
    assert "deploy_app" not in text
    assert "Use Genie memory first" in text
    assert "suffixes such as `-2`" in text
    assert "Which Unity Catalog holds the Navidad data?" in text
    assert "navidad_<tag>" in text
    assert "navidad-<tag>-app" in text
    assert "Where is the hack data" in text
    assert "lakebase-sql.md" in text
    assert "hack-ideas.md" in text
    assert "Suggested tracks include:" in text
    assert "hack-build.md" in text
    assert "hack-routes.md" in text
    assert "hack-segmentation.md" in text
    assert "data-dictionary.md" in text
    assert "collaborative design partner" in text
    assert "Suggest, don't ask open-ended questions" in text
    assert "Explore → Interpret → Build" in text
    assert "executeLakebasePostgresSql" in text
    assert "Data orientation" in text
    assert "app-deploy.md" in text
    assert "app-endpoints.md" in text
    assert "Path A" in text
    assert "OpenStreetMap with a muted CSS filter" in text
    assert "### Language" in text
    assert "Spanish (es-ES)" in text
    assert "Península / Canarias" in text
    assert "campaign" in text


def test_dictionary_lists_every_column():
    text = DICT_PATH.read_text()
    for table, cols in REQUIRED_TABLES.items():
        assert f"## `{table}`" in text
        for col in cols:
            assert f"`{col}`" in text.split(f"## `{table}`", 1)[1].split("## `", 1)[0]
    assert "already a three-agent fleet" in text
    assert "campaign year" in text
    assert "Atlantic/Canary" in text


def test_hack_playbooks_pin_known_pitfalls():
    routes = (ROOT / "references" / "hack-routes.md").read_text()
    segs = (ROOT / "references" / "hack-segmentation.md").read_text()
    build = (ROOT / "references" / "hack-build.md").read_text()
    sql = (ROOT / "references" / "lakebase-sql.md").read_text()
    deploy = (ROOT / "references" / "app-deploy.md").read_text()
    endpoints = (ROOT / "references" / "app-endpoints.md").read_text()
    assert "Explore → Interpret → Build" in routes
    assert "ST_MakePoint(h.lon, h.lat)" in routes
    assert "CREATE EXTENSION IF NOT EXISTS postgis" in routes
    assert "households_sync" in routes
    assert "muted-tiles" in routes
    assert "IGNBaseTodo" in routes
    assert "CC BY 4.0" in routes
    assert "Canarias" in routes
    assert "j.campaign = %s" in routes
    assert "cartocdn" not in routes.lower() or "API key" in routes
    assert "person_id" in segs
    assert "person_segments" in segs
    assert "Lakebase" in segs
    assert "Do not write a new Unity Catalog table unless they explicitly ask" in segs
    assert "hack-routes.md" in build
    assert "Do **not** default to Spark" in build
    assert "GRANT SELECT" in build
    assert "executeLakebasePostgresSql" in sql
    assert "generate_database_credential" in sql
    assert "CREATE TABLE" in sql
    assert "GRANT SELECT" in sql
    assert "databricks_postgres" in sql
    assert "AppDeployment" in deploy
    assert "timedelta" in deploy
    assert "/api/sample-households" in endpoints
    assert "/health" in endpoints


def test_suggested_tracks_are_varied_and_bounded():
    ideas = (ROOT / "references" / "hack-ideas.md").read_text()
    assert "suggested tracks" in ideas.lower()
    assert "not a fixed menu" in ideas
    assert "Reyes fleet rebalancing" in ideas
    assert "fixed paje overhead per helper" in ideas
    assert "already a fleet of three" in ideas
    assert "Route optimisation and maps" in ideas
    assert "Noche de Reyes operations centre" in ideas
    assert "Island logistics" in ideas
    assert "Carbón planner" in ideas
    assert "Regional traditions map" in ideas
    assert "Gift logistics" in ideas
    assert "City growth and capacity forecasting" in ideas
    assert "AI briefing assistant" in ideas
    assert "Suggested tracks include:" in ideas


def test_no_uk_leftovers():
    for path in [ROOT / "SKILL.md", *sorted((ROOT / "references").glob("*.md"))]:
        text = path.read_text()
        for needle in ("London", "UK ", "sleigh_id", "santa_", "santa-", "lakebase_tigers"):
            assert needle not in text, (path.name, needle)


def test_dictionaries_match_infra_copy():
    infra = ROOT.parent / "navidad-hack-infra" / "references" / "data-dictionary.md"
    assert DICT_PATH.read_text() == infra.read_text()


def test_new_features_go_through_dev_branch_and_approval():
    text = (ROOT / "SKILL.md").read_text()
    flow = (ROOT / "references" / "dev-branch-workflow.md").read_text()
    sql = (ROOT / "references" / "lakebase-sql.md").read_text()
    build = (ROOT / "references" / "hack-build.md").read_text()
    assert "## Branch-first development" in text
    assert "dev-branch-workflow.md" in text and "dev-branch-workflow.md" in sql and "dev-branch-workflow.md" in build
    assert "Wait for an explicit yes" in text
    for needle in ("dev-<feature-slug>-<MMDD-HHMM>", "create_branch", "ttl=Duration(seconds=24 * 3600)",
                   "migrations/NNN_<feature-slug>.sql", "idempotent", "has_table_privilege",
                   "Sync tables intact", "¿Migro estos cambios a production? (sí / ajustar / descartar)",
                   "Stop and wait for an explicit yes", "one transaction", "SELECT now()",
                   "delete_branch", "Lakebase has no branch merge"):
        assert needle in flow, needle
    # migration happens before the app deploy
    assert flow.index("Run the `-- no transaction` block") < flow.index("Deploy the app changes now")


def test_control_center_track_is_wired():
    text = (ROOT / "SKILL.md").read_text()
    cc = (ROOT / "references" / "hack-control-center.md").read_text()
    assert "hack-control-center.md" in text
    assert "hack-control-center.md" in (ROOT / "references" / "hack-build.md").read_text()
    assert "Centro de control en directo" in (ROOT / "references" / "hack-ideas.md").read_text()
    for needle in ("One writer per table", "AvailableNow", "upsertkey", "fixed catalogue", "Explore → Interpret → Build",
                   "lakebase_directo", "shuffle.partitions", "dev-branch-workflow.md"):
        assert needle in cc, needle
    infra = ROOT.parent / "navidad-hack-infra"
    assert (infra / "templates" / "control" / "control.py").exists() and (infra / "sql" / "live.sql").exists()


def test_search_track_is_wired():
    text = (ROOT / "SKILL.md").read_text()
    hs = (ROOT / "references" / "hack-search.md").read_text()
    assert "hack-search.md" in text and "hack-search.md" in (ROOT / "references" / "hack-build.md").read_text()
    for needle in ("Lakebase Search", "lakebase_bm25", "unaccent", "read replicas", "08_busqueda", "ReadRouter"):
        assert needle in hs, needle
    infra = ROOT.parent / "navidad-hack-infra"
    assert (infra / "templates" / "search" / "search.py").exists() and (infra / "sql" / "search.sql").exists()


def test_incident_track_is_wired_and_branch_only():
    text = (ROOT / "SKILL.md").read_text()
    inc = (ROOT / "references" / "hack-incidents.md").read_text()
    endpoints = (ROOT / "references" / "app-endpoints.md").read_text()
    assert "hack-incidents.md" in text
    assert "hack-incidents.md" in (ROOT / "references" / "hack-build.md").read_text()
    assert "Incidencias en rama" in (ROOT / "references" / "hack-ideas.md").read_text()
    for needle in ("filomena_madrid", "dana_valencia", "cortes_barcelona", "mount_incidents",
                   "Never activate zones", "applied_plan", "inspirados en", "OpenStreetMap",
                   "Explore → Interpret → Build", "pgr_TSP", "provision_incidents"):
        assert needle in inc, needle
    assert "/api/sim/{branch}/apply" in endpoints
    infra = ROOT.parent / "navidad-hack-infra" / "templates" / "incidents"
    assert (infra / "incidents.py").exists() and (infra / "static" / "planner.html").exists()
