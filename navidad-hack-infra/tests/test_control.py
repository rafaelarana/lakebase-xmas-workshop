import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "templates" / "control"))
pytest.importorskip("fastapi")
import control  # noqa: E402


def ctx(code="chimenea_bloqueada", nearby=True, state="abierta"):
    return {"exception": {"exception_code": code, "city": "Valencia", "event_id": "e1", "state": state},
            "nearby_helpers": [{"helper_id": "7-2", "helper_name": "Pinito 2", "helper_kind": "duende", "distance_m": 350}] if nearby else [],
            "similar_open_in_city": 3, "active_incident_zones": ["DANA"], "bringer_name": "Papá Noel",
            "code_name": control.CODE_NAMES[code]}


def test_every_exception_code_has_rules_from_the_catalogue():
    from live_sim import EXCEPTION_CODES
    assert set(control.RULES) == set(EXCEPTION_CODES) == set(control.CODE_NAMES)
    assert all(a in control.ACTIONS for acts in control.RULES.values() for a in acts)


def test_actions_are_validated_against_the_catalogue():
    c = ctx()
    assert control._action("borrar_hogar", c) is None                       # invented action → dropped
    a = control._action("reasignar_ayudante", c, {"helper_id": "no-existe"})  # unknown helper → nearest real one
    assert a["params"]["helper_id"] == "7-2" and a["label"] == "Reasignar a Pinito 2 (350 m)"
    assert control._action("reasignar_ayudante", ctx(nearby=False)) is None
    assert control._action("reintentar", c, {"minutos": 999})["params"] == {"minutos": 120}
    assert control._action("escalar", c)["label"] == "Escalar a Papá Noel"


def test_rule_fallback_explains_context():
    diag, acts = control.rules(ctx())
    assert "Chimenea bloqueada en Valencia" in diag and "3 casos" in diag and "DANA" in diag
    assert [a["code"] for a in acts] == ["reasignar_ayudante", "punto_recogida"]


class FakeDb:
    def __init__(self):
        self.calls = []

    def tx(self, fn):
        db = self

        class Cur:
            def __init__(self):
                self.n = 0

            def execute(self, sql, params=None):
                db.calls.append(sql.split()[0] + " " + sql.split()[2] if sql.split()[0] == "INSERT" else sql.split()[0])

            def fetchone(self):
                self.n += 1
                return {"review_id": 1, "action_id": self.n}
        return fn(Cur())


def test_review_falls_back_to_rules_when_the_model_fails(monkeypatch):
    monkeypatch.setattr(control, "context", lambda db, eid: ctx("nino_despierto"))
    def broken(prompt):
        raise RuntimeError("gateway down")
    out = control.review(FakeDb(), "e1", broken, "m")
    assert out["source"] == "reglas" and [a["code"] for a in out["actions"]] == ["reintentar", "reasignar_ayudante"]
    assert all(a["status"] == "propuesta" for a in out["actions"])


def test_review_uses_valid_model_actions_only(monkeypatch):
    monkeypatch.setattr(control, "context", lambda db, eid: ctx())
    reply = ('Claro: {"diagnostico": "El duende no puede entrar.", "acciones": ['
             '{"codigo": "reasignar_ayudante", "parametros": {"helper_id": "7-2"}, "efecto": "+4 min"},'
             '{"codigo": "hackear", "parametros": {}}, {"codigo": "punto_recogida", "parametros": {}}]}')
    out = control.review(FakeDb(), "e1", lambda p: reply, "m")
    assert out["source"] == "llm" and [a["code"] for a in out["actions"]] == ["reasignar_ayudante", "punto_recogida"]
    assert out["actions"][0]["effect"] == "+4 min"


def test_resolved_exceptions_are_not_reopened_by_a_review(monkeypatch):
    for st in ("resuelta", "descartada"):
        monkeypatch.setattr(control, "context", lambda db, eid, st=st: ctx(state=st))
        with pytest.raises(ValueError, match=st):
            control.review(FakeDb(), "e1", lambda p: "{}", "m")


def test_live_dots_tell_deliveries_from_exceptions():
    assert control.STATUS_CODE["entregado"] != control.STATUS_CODE["excepcion"]


def test_view_lets_a_retry_beat_an_unresolved_agent_state():
    from provision_live import live_sql
    sql = live_sql("navidad_x")
    view = sql[sql.index("CREATE OR REPLACE VIEW navidad_live.v_exception"):sql.index("CREATE OR REPLACE VIEW navidad_live.v_city_progress")]
    assert view.index("s.status IN ('resuelta', 'descartada')") < view.index("d.event_ts > e.event_ts THEN 'resuelta'") \
        < view.index("COALESCE(s.status, 'abierta')")
    assert "e.*" not in view and "DROP VIEW" not in sql  # explicit columns: CREATE OR REPLACE keeps grants


def test_wiring_patches_the_gateway_helper_and_is_idempotent(tmp_path):
    from provision_incidents import APP_TEMPLATE, add_control_center
    import shutil
    dest = tmp_path / "app"
    shutil.copytree(APP_TEMPLATE, dest)
    for _ in range(2):
        add_control_center(dest)
    src = (dest / "app.py").read_text()
    assert src.count("mount_control(") == 1 and "max_tokens=AGENT_MAX_TOKENS" in src
    assert "def complete_from_gateway(model: str, prompt: str, max_tokens: int = 64) -> str:" in src
    assert '"max_tokens": max_tokens,' in src and "max(256, max_tokens)" in src
    compile(src, "app.py", "exec")
    # upgrade from the first wiring
    old = src.replace(src[src.index("    # Real-time control center"):src.index("    app.mount(")],
                      "    # Real-time control center: x\n    from control import mount_control\n\n    mount_control(app, STATIC_DIR, run_complete)\n\n")
    (dest / "app.py").write_text(old)
    add_control_center(dest)
    assert "run_complete)" not in (dest / "app.py").read_text().split("# Real-time control center")[1]


def test_agent_model_env_is_optional(tmp_path):
    from deploy_app import _write_app_yaml
    _write_app_yaml(dest_dir=tmp_path, model="databricks-gpt-oss-120b", schema="s", project_id="p")
    assert "NAVIDAD_AGENT_MODEL" not in (tmp_path / "app.yaml").read_text()
    _write_app_yaml(dest_dir=tmp_path, model="m", schema="s", project_id="p", agent_model="system.ai.claude-haiku-5-5")
    assert "NAVIDAD_AGENT_MODEL\n    value: system.ai.claude-haiku-5-5" in (tmp_path / "app.yaml").read_text()


class StateDb:
    def __init__(self):
        self.sql = []

    def q(self, sql, params=None):
        self.sql.append(sql)
        if "FROM navidad_live.sim_run" in sql:
            return [{"sim_run_id": "r1", "planned": 10, "status": "en_curso"}]
        if "AS delivered" in sql and "p50" in sql:
            return [{"delivered": 3, "per_min": 1, "clock": None, "last_update": None, "total": 1, "open": 1,
                     "by_agent": 0, "by_retry": 0, "p50": 4.0, "p95": 9.0, "cycle": 8.0}]
        if "updated_at > %s" in sql:
            return [{"lat": 1, "lon": 2, "status": "excepcion", "updated_at": "t1"}]
        return []


def test_viewers_share_the_run_state_within_a_second():
    control.invalidate()
    db = StateDb()
    a = control.state(db, None, None)
    b = control.state(db, None, "t0")
    assert sum("AS delivered" in s for s in db.sql) == 1       # run-wide part computed once
    assert sum("updated_at > %s" in s for s in db.sql) == 2    # each viewer gets its own changes
    assert a["recent"] == [{"lat": 1, "lon": 2, "s": "x"}] and b["cursor"] == "t1"
    assert a["kpis"]["latency_p50"] == 4.0 and a["kpis"]["stream_cycle_s"] == 8.0
    control.invalidate()
    control.state(db, None, None)
    assert sum("AS delivered" in s for s in db.sql) == 2


from types import SimpleNamespace as NS  # noqa: E402


class FakeJobs:
    def __init__(self, active=False, exists=True):
        self.active, self.exists, self.started, self.cancelled = active, exists, [], []

    def list(self, name):
        return [NS(job_id=42)] if self.exists else []

    def list_runs(self, job_id, active_only, limit):
        if active_only and not self.active:
            return []
        state = NS(life_cycle_state=NS(value="RUNNING" if self.active else "TERMINATED"),
                   result_state=None if self.active else NS(value="SUCCESS"))
        return [NS(run_id=7, state=state, run_page_url="u", start_time=0,
                   job_parameters=[NS(name="campaign", value="reyes"), NS(name="minutes", value="15")])]

    def run_now(self, job_id, job_parameters):
        self.started.append((job_id, job_parameters))
        return NS(run_id=8)

    def cancel_run(self, run_id):
        self.cancelled.append(run_id)


class JobDb:
    def __init__(self, jobs):
        self.project_id, self.client, self.sql = "navidad-rafa", NS(jobs=jobs), []

    def q(self, sql, params=None):
        self.sql.append(sql)
        return []


def test_play_starts_the_job_with_the_chosen_night():
    jobs = FakeJobs()
    out = control.LiveJob(JobDb(jobs)).start(control.StartRequest(campaign="reyes", minutes=10, destino="zerobus"))
    assert out["run_id"] == 8 and jobs.started == [(42, {"campaign": "reyes", "minutes": "10", "stream_minutes": "16",
                                                          "destino": "zerobus", "incident": "ninguna"})]
    assert control.job_name("navidad-rafa") == "navidad-live-rafa"


def test_play_is_refused_while_a_night_runs_or_with_bad_parameters():
    with pytest.raises(ValueError, match="en marcha"):
        control.LiveJob(JobDb(FakeJobs(active=True))).start(control.StartRequest())
    for bad in ({"campaign": "halloween"}, {"minutes": 500}, {"destino": "kafka"}):
        with pytest.raises(ValueError):
            control.LiveJob(JobDb(FakeJobs())).start(control.StartRequest(**bad))


def test_stop_cancels_the_run_and_closes_the_night():
    jobs = FakeJobs(active=True)
    db = JobDb(jobs)
    assert control.LiveJob(db).stop() == {"cancelled": [7]} and jobs.cancelled == [7]
    assert any("SET status = 'cancelada'" in s for s in db.sql)


def test_status_without_the_job_explains_how_to_create_it():
    st = control.LiveJob(JobDb(FakeJobs(exists=False))).status()
    assert st["available"] is False and "04_tiempo_real_stream" in st["message"]
    st = control.LiveJob(JobDb(FakeJobs(active=True))).status()
    assert st["active"] and st["params"]["campaign"] == "reyes"


def test_live_job_runs_both_notebooks_with_job_parameters():
    from provision_live import live_job_name, live_job_settings
    s = live_job_settings(name=live_job_name("navidad-rafa"), notebook_dir="/Users/x/workshop/notebooks", catalog="c", suffix="")
    assert s["name"] == "navidad-live-rafa" and s["max_concurrent_runs"] == 1
    tasks = {t.task_key: t.notebook_task for t in s["tasks"]}
    assert tasks["stream"].notebook_path.endswith("04_tiempo_real_stream")
    assert tasks["stream"].base_parameters["stream_minutes"] == "{{job.parameters.stream_minutes}}"
    assert tasks["producer"].base_parameters["destino"] == "{{job.parameters.destino}}"
    assert {p.name: p.default for p in s["parameters"]}["catalog"] == "c"
    assert all(t.max_retries == 1 for t in s["tasks"])


def test_live_job_task_parameters_match_job_parameter_names():
    # job parameters are pushed down by key and win over base_parameters: a task parameter named like a
    # job parameter must read that same job parameter (stream «minutes» used to get the producer's 15)
    from provision_live import live_job_settings
    s = live_job_settings(name="navidad-live-x", notebook_dir="/n", catalog="c", suffix="")
    names = {p.name for p in s["parameters"]}
    for t in s["tasks"]:
        for k, v in t.notebook_task.base_parameters.items():
            assert v == f"{{{{job.parameters.{k}}}}}" and k in names, (t.task_key, k, v)
    nb = (Path(__file__).resolve().parents[2] / "workshop" / "notebooks" / "04_tiempo_real_stream.py").read_text()
    assert 'widgets.text("stream_minutes"' in nb and 'widgets.get("minutes")' not in nb


def test_play_passes_the_incident_and_validates_it():
    jobs = FakeJobs()
    control.LiveJob(JobDb(jobs)).start(control.StartRequest(incident="dana_valencia"))
    assert jobs.started[0][1]["incident"] == "dana_valencia"
    with pytest.raises(ValueError):
        control.LiveJob(JobDb(FakeJobs())).start(control.StartRequest(incident="tsunami"))


def test_metrics_bucket_and_zone_scenario_are_validated():
    with pytest.raises(ValueError):
        control.metrics(StateDb(), "r1", 7)
    with pytest.raises(ValueError):
        control.zones(StateDb(), "ninguna")


class EpDb:
    def __init__(self, name, fail=False):
        self.endpoint, self.fail, self.calls = name, fail, 0

    def q(self, sql, params=None):
        self.calls += 1
        if self.fail:
            import psycopg2
            raise psycopg2.OperationalError("down")
        return [{"from": self.endpoint}]


def _router(endpoints, monkeypatch, fail=False):
    primary = control.Db("navidad-x", client=NS(postgres=NS(list_endpoints=lambda parent: [
        NS(name=n, status=NS(endpoint_type="EndpointType.ENDPOINT_TYPE_READ_ONLY")) for n in endpoints])),
        endpoint="projects/navidad-x/branches/production/endpoints/primary")
    primary.q = lambda sql, params=None: [{"from": "primary"}]
    made = {}
    monkeypatch.setattr(control, "Db", lambda pid, client, endpoint=None, attempts=8: made.setdefault(endpoint, EpDb(endpoint, fail)))
    return control.ReadRouter(primary), made


def test_reads_use_the_replica_when_the_branch_has_one(monkeypatch):
    monkeypatch.delenv("LAKEBASE_READ_ENDPOINT", raising=False)
    r, _ = _router(["projects/navidad-x/branches/production/endpoints/replica"], monkeypatch)
    assert r.q("SELECT 1")[0]["from"].endswith("/replica") and r.target == "replica"
    r2, _ = _router([], monkeypatch)
    assert r2.q("SELECT 1") == [{"from": "primary"}] and r2.target == "primary"


def test_a_failing_replica_falls_back_to_the_primary(monkeypatch):
    monkeypatch.delenv("LAKEBASE_READ_ENDPOINT", raising=False)
    r, made = _router(["projects/navidad-x/branches/production/endpoints/replica"], monkeypatch, fail=True)
    assert r.q("SELECT 1") == [{"from": "primary"}] and r.target == "primary"
    calls = list(made.values())[0].calls
    r.q("SELECT 1")
    assert list(made.values())[0].calls == calls  # replica skipped for 30 s


def test_review_reads_context_from_the_reader_and_writes_on_the_primary(monkeypatch):
    seen = []
    monkeypatch.setattr(control, "context", lambda db, eid: seen.append(db) or ctx())
    reader, writer = object(), FakeDb()
    control.review(writer, "e1", None, "m", reader)
    assert seen == [reader] and writer.calls  # writes went to the primary


def test_left_rail_replaces_header_links_and_is_idempotent(tmp_path):
    from provision_incidents import _NAV_CSS, _NAV_JS, add_nav
    (tmp_path / "static").mkdir()
    page = ('<html><head><title>x</title></head><body class="a">\n<header class="h"><h1>Mapa</h1>'
            '<a href="/incidencias" style="margin-left:auto">Incidencias</a><span class="links"><a href="/plumbing">Diagnóstico</a></span>'
            '<a href="/control">Centro de control</a></header>\n<p><a href="https://www.openstreetmap.org/copyright">OSM</a></p></body></html>')
    (tmp_path / "static" / "map.html").write_text(page)
    for _ in range(2):
        add_nav(tmp_path)
    out = (tmp_path / "static" / "map.html").read_text()
    assert out.count(_NAV_CSS) == 1 and out.count(_NAV_JS) == 1
    assert out.index('<body class="a">') < out.index(_NAV_JS) < out.index("<header")
    header = out[out.index("<header"):out.index("</header>")]
    assert 'href="/' not in header and "links" not in header
    assert "openstreetmap.org/copyright" in out   # other links untouched
    assert (tmp_path / "static" / "nav.js").exists() and (tmp_path / "static" / "nav.css").exists()


def test_state_says_not_prepared_when_navidad_live_is_missing(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class UndefinedTable(Exception):
        pgcode = "42P01"

    def missing(*a, **k):
        raise UndefinedTable('relation "navidad_live.sim_run" does not exist')

    monkeypatch.setattr(control, "state", missing)
    app = FastAPI()
    control.mount_control(app, tmp_path, client=object())
    r = TestClient(app).get("/api/control/state")
    assert r.status_code == 503 and "00_workbench" in r.json()["detail"]
    assert control._not_prepared(Exception('schema "navidad_live" does not exist'))
    assert not control._not_prepared(Exception("connection refused"))


def test_workbench_prepares_the_control_center():
    nb = (Path(__file__).resolve().parents[2] / "workshop" / "notebooks" / "00_workbench.py").read_text()
    assert "provision_live(conn=db.conn, schema=schema, app_sp=app_sp" in nb and "ensure_live_job(" in nb
    assert "ensure_ingest_identity(w)" in nb  # Zerobus identity up front: the ▶ job never races for it


def test_stop_without_a_job_run_leaves_the_night_alone():
    db = JobDb(FakeJobs(active=False))
    with pytest.raises(ValueError, match="a mano"):
        control.LiveJob(db).stop()
    assert not any("SET status = 'cancelada'" in s for s in db.sql)


def test_stop_reports_a_failed_cancel_and_does_not_close_the_night():
    class Failing(FakeJobs):
        def cancel_run(self, run_id):
            raise RuntimeError("PERMISSION_DENIED")

    db = JobDb(Failing(active=True))
    with pytest.raises(RuntimeError, match="no se pudo parar"):
        control.LiveJob(db).stop()
    assert not any("SET status = 'cancelada'" in s for s in db.sql)


def test_not_prepared_only_for_navidad_live():
    class E(Exception):
        pgcode = "42P01"

    assert control._not_prepared(E('relation "navidad_live.sim_run" does not exist'))
    assert not control._not_prepared(E('relation "navidad_geo.zone" does not exist'))
    assert not control._not_prepared(E('relation "navidad_x.households_sync" does not exist'))
