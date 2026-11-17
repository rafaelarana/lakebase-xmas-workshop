"""Incident simulation on Lakebase branches (hack track: Filomena · DANA · cortes).

Each simulation runs on its own copy-on-write child branch of the production branch:
  1. create branch  sim-<scenario>-<utc stamp>  (TTL 24 h; at most MAX_SIM_BRANCHES kept)
  2. wait for the branch compute, connect as the app's service principal
  3. activate the scenario's incident zones (navidad_geo.set_minute)
  4. navidad_geo.simulate  → pgRouting base vs incident travel time per affected leg
  5. navidad_geo.suggest   → detours, pickup points, helpers, TSP re-sequencing
  6. navidad_geo.sim_geojson → map payload
Production is never modified, except by an explicit "apply plan", which copies the
suggestions into navidad_geo.applied_plan.

Mount with ``mount_incidents(app, static_dir)``. Needs navidad_geo (provision_incidents),
NAVIDAD_PROJECT_ID and NAVIDAD_SCHEMA env vars, and project permission for the app's
service principal to create/delete branches.
"""

from __future__ import annotations

import datetime as dt
import os
import threading
import time
from pathlib import Path
from typing import Any

import psycopg2
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.postgres import Branch, BranchSpec
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from google.protobuf.duration_pb2 import Duration
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel

SIM_PREFIX = "sim-"
MAX_SIM_BRANCHES = 3
SIM_TTL_S = 24 * 3600
PG_DATABASE = "databricks_postgres"
ALL_ZONES_MINUTE = 10_000
SCENARIOS = {
    "filomena_madrid": {"short": "filomena", "replay": False},
    "dana_valencia": {"short": "dana", "replay": False},
    "cortes_barcelona": {"short": "bcn", "replay": True},
}
PHASES = ["rama", "computo", "incidencia", "rutas", "parametros", "sugerencias", "mapa"]
MAX_HELPERS, MAX_BOOST = 4, 1.5


class SimRequest(BaseModel):
    scenario: str
    minute: int | None = None
    helpers: int = 0          # extra duendes / pajes per route
    boost: float = 1.0        # reindeer / camel speed-up of the travel share (1.0–1.5)


class PlanRequest(BaseModel):
    city: str
    campaign: str = "nochebuena"
    year: int | None = None
    helpers: int = 0
    boost: float = 1.0


HELPER_CAPACITY = 0.6        # same as navidad_geo.apply_params
TRAVEL_SHARE_DEFAULT = 0.62  # travel share of the night measured on the DANA Valencia simulation
PLAN_SAMPLE = 1500           # stops drawn on the map


class ParamsRequest(BaseModel):
    helpers: int = 0
    boost: float = 1.0


def _params(helpers: int, boost: float) -> tuple[int, float]:
    if not 0 <= helpers <= MAX_HELPERS or not 1.0 <= boost <= MAX_BOOST:
        raise HTTPException(400, f"parámetros fuera de rango: ayudantes 0–{MAX_HELPERS}, impulso 1,0–{MAX_BOOST}")
    return int(helpers), round(float(boost), 2)


class MinuteRequest(BaseModel):
    minute: int


def _ident(name: str) -> str:
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError(f"unsafe identifier {name!r}")
    return name


class Lakebase:
    """Branch lifecycle + connections as the app's service principal."""

    def __init__(self, project_id: str, client: WorkspaceClient | None = None):
        self.project_id = project_id
        self._client = client
        self._user: str | None = None

    @property
    def client(self) -> WorkspaceClient:
        if self._client is None:  # lazy: no credentials needed until the first call
            self._client = WorkspaceClient()
        return self._client

    @property
    def project(self) -> str:
        return f"projects/{self.project_id}"

    def branch_path(self, branch_id: str) -> str:
        return f"{self.project}/branches/{branch_id}"

    def user(self) -> str:
        if not self._user:
            me = self.client.current_user.me()
            self._user = me.user_name or me.application_id or ""
        return self._user

    def connect(self, branch_id: str = "production", attempts: int = 8) -> Any:
        endpoint = f"{self.branch_path(branch_id)}/endpoints/primary"
        last: Exception | None = None
        for i in range(attempts):  # a fresh branch compute can take a few seconds to accept connections
            try:
                ep = self.client.postgres.get_endpoint(name=endpoint)
                host = ep.status.hosts.host if ep.status and ep.status.hosts else None
                if not host:
                    raise RuntimeError("endpoint has no host yet")
                token = self.client.postgres.generate_database_credential(endpoint=endpoint).token
                conn = psycopg2.connect(host=host, user=self.user(), password=token, dbname=PG_DATABASE,
                                        sslmode="require", connect_timeout=30)
                conn.autocommit = True
                return conn
            except Exception as exc:  # retry: compute waking up / DNS not ready
                last = exc
                time.sleep(min(2 + 2 * i, 10))
        raise RuntimeError(f"cannot connect to branch {branch_id}: {last}")

    def create_branch(self, branch_id: str) -> None:
        spec = BranchSpec(source_branch=self.branch_path("production"), ttl=Duration(seconds=SIM_TTL_S))
        self.client.postgres.create_branch(parent=self.project, branch=Branch(spec=spec), branch_id=branch_id).wait()

    def delete_branch(self, branch_id: str) -> None:
        if not branch_id.startswith(SIM_PREFIX):
            raise ValueError("only simulation branches can be deleted")
        self.client.postgres.delete_branch(name=self.branch_path(branch_id)).wait()

    def sim_branches(self) -> list[dict[str, Any]]:
        out = []
        for b in self.client.postgres.list_branches(parent=self.project):
            bid = (b.name or "").rsplit("/", 1)[-1]
            if bid.startswith(SIM_PREFIX):
                created = b.create_time.ToDatetime() if b.create_time else None
                out.append({"branch": bid, "created": created.isoformat() + "Z" if created else None})
        return sorted(out, key=lambda x: x["created"] or "")


def _query(conn: Any, sql: str, params: tuple | None = None) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []


class Simulations:
    def __init__(self, lakebase: Lakebase, schema: str):
        self.lb = lakebase
        self.schema = _ident(schema)
        self.state: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    # --- lifecycle -----------------------------------------------------------
    def start(self, scenario: str, minute: int | None, helpers: int = 0, boost: float = 1.0) -> str:
        helpers, boost = _params(helpers, boost)
        if scenario not in SCENARIOS:
            raise HTTPException(400, f"escenario desconocido: {scenario}")
        stamp = dt.datetime.utcnow().strftime("%m%d-%H%M%S")
        branch = f"{SIM_PREFIX}{SCENARIOS[scenario]['short']}-{stamp}"
        if minute is None:
            minute = 0 if SCENARIOS[scenario]["replay"] else ALL_ZONES_MINUTE
        with self.lock:
            busy = [s for s in self.state.values() if s["status"] == "running"]
            if busy:
                raise HTTPException(409, f"ya hay una simulación en curso: {busy[0]['branch']}")
            self.state[branch] = {"branch": branch, "scenario": scenario, "minute": minute, "status": "running",
                                  "helpers": helpers, "boost": boost,
                                  "phases": [], "kpi": None, "suggest": None, "geojson": None, "error": None,
                                  "applied": None, "created": dt.datetime.utcnow().isoformat() + "Z"}
        threading.Thread(target=self._run, args=(branch, True), daemon=True).start()
        return branch

    def rerun(self, branch: str, minute: int) -> None:
        s = self._get(branch)
        if s["status"] == "running":
            raise HTTPException(409, "la simulación sigue en curso")
        with self.lock:
            s.update(status="running", minute=minute, phases=[p for p in s["phases"] if p["phase"] in ("rama", "computo")],
                     error=None, applied=None)
        threading.Thread(target=self._run, args=(branch, False), daemon=True).start()

    def replan(self, branch: str, helpers: int, boost: float) -> None:
        """New planner parameters on an existing branch: no new simulation, only apply_params + suggest + map."""
        helpers, boost = _params(helpers, boost)
        s = self._get(branch)
        if s["status"] == "running":
            raise HTTPException(409, "la simulación sigue en curso")
        if not s.get("kpi"):
            raise HTTPException(409, "primero simula el escenario")
        with self.lock:
            s.update(status="running", helpers=helpers, boost=boost, error=None, applied=None,
                     phases=[p for p in s["phases"] if p["phase"] not in ("parametros", "sugerencias", "mapa")])
        threading.Thread(target=self._run, args=(branch, False, True), daemon=True).start()

    def _phase(self, branch: str, name: str, fn: Any) -> Any:
        t0 = time.time()
        with self.lock:
            self.state[branch]["phases"].append({"phase": name, "status": "running", "s": None})
        out = fn()
        with self.lock:
            self.state[branch]["phases"][-1].update(status="done", s=round(time.time() - t0, 1))
        return out

    def _run(self, branch: str, create: bool, params_only: bool = False) -> None:
        s = self.state[branch]
        conn = None
        try:
            if create:
                self._enforce_cap(keep=branch)
                self._phase(branch, "rama", lambda: self.lb.create_branch(branch))
            conn = self._phase(branch, "computo", lambda: self.lb.connect(branch)) if create else self.lb.connect(branch)
            sc, sch = s["scenario"], self.schema
            if not params_only:
                self._phase(branch, "incidencia", lambda: _query(conn, "SELECT navidad_geo.set_minute(%s, %s) AS n", (sc, s["minute"])))
                self._phase(branch, "rutas", lambda: _query(conn, "SELECT navidad_geo.simulate(%s, %s) AS k", (sch, sc))[0]["k"])
            kpi = self._phase(branch, "parametros", lambda: _query(
                conn, "SELECT navidad_geo.apply_params(%s, %s) AS k", (s.get("helpers", 0), s.get("boost", 1.0)))[0]["k"])
            sug = self._phase(branch, "sugerencias", lambda: _query(conn, "SELECT navidad_geo.suggest(%s, %s) AS k", (sch, sc))[0]["k"])
            geo = self._phase(branch, "mapa", lambda: _query(conn, "SELECT navidad_geo.sim_geojson(%s) AS g", (sch,))[0]["g"])
            with self.lock:
                s.update(status="done", kpi=kpi, suggest=sug, geojson=geo)
        except Exception as exc:
            with self.lock:
                s.update(status="error", error=str(exc)[:500])
                if s["phases"] and s["phases"][-1]["status"] == "running":
                    s["phases"][-1]["status"] = "error"
        finally:
            if conn is not None:
                conn.close()

    def _enforce_cap(self, keep: str) -> None:
        existing = [b["branch"] for b in self.lb.sim_branches() if b["branch"] != keep]
        for old in existing[: max(0, len(existing) - (MAX_SIM_BRANCHES - 1))]:
            self.lb.delete_branch(old)
            with self.lock:
                self.state.pop(old, None)

    def _get(self, branch: str) -> dict[str, Any]:
        s = self.state.get(branch)
        if s is None:
            raise HTTPException(404, f"simulación no encontrada: {branch}")
        return s

    # --- views -----------------------------------------------------------------
    def view(self, branch: str) -> dict[str, Any]:
        with self.lock:
            return dict(self._get(branch))

    def listing(self) -> list[dict[str, Any]]:
        live = {b["branch"]: b for b in self.lb.sim_branches()}
        with self.lock:
            for bid, b in live.items():
                self.state.setdefault(bid, {"branch": bid, "scenario": None, "minute": None, "status": "huérfana",
                                            "phases": [], "kpi": None, "suggest": None, "geojson": None,
                                            "error": None, "applied": None, "created": b["created"]})
            for bid in [k for k in self.state if k not in live and self.state[k]["status"] != "running"]:
                self.state.pop(bid)
            return [{k: v for k, v in s.items() if k not in ("geojson",)} for s in self.state.values()]

    def discard(self, branch: str) -> None:
        s = self._get(branch)
        if s["status"] == "running":
            raise HTTPException(409, "la simulación sigue en curso")
        self.lb.delete_branch(branch)
        with self.lock:
            self.state.pop(branch, None)

    def apply(self, branch: str) -> dict[str, Any]:
        s = self._get(branch)
        if s["status"] != "done":
            raise HTTPException(409, "solo se puede aplicar una simulación terminada")
        src = self.lb.connect(branch)
        try:
            rows = _query(src, "SELECT kind, route_id, household_id, from_bringer, to_bringer, detail::text AS detail "
                               "FROM navidad_geo.sim_suggestion")
        finally:
            src.close()
        dst = self.lb.connect("production")
        try:
            with dst.cursor() as cur:
                cur.execute("DELETE FROM navidad_geo.applied_plan WHERE plan_id = %s", (branch,))
                cur.executemany(
                    "INSERT INTO navidad_geo.applied_plan (plan_id, scenario_id, branch, applied_by, kind, route_id, "
                    "household_id, from_bringer, to_bringer, detail) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                    [(branch, s["scenario"], branch, self.lb.user(), r["kind"], r["route_id"], r["household_id"],
                      r["from_bringer"], r["to_bringer"], r["detail"]) for r in rows])
        finally:
            dst.close()
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        with self.lock:
            s["applied"] = {"at": dt.datetime.utcnow().isoformat() + "Z", "rows": len(rows), "by_kind": counts}
        return s["applied"]


def plan_options(lb: Lakebase, schema: str) -> dict[str, Any]:
    conn = lb.connect("production")
    try:
        years = _query(conn, f"SELECT DISTINCT year FROM {_ident(schema)}.journeys_sync ORDER BY year DESC")
        cities = _query(conn, f"""SELECT city, count(*) AS households, avg(lat) AS lat, avg(lon) AS lon
                                  FROM {_ident(schema)}.households_sync GROUP BY city ORDER BY count(*) DESC""")
    finally:
        conn.close()
    return {"years": [y["year"] for y in years],
            "cities": [{**c, "lat": float(c["lat"]), "lon": float(c["lon"])} for c in cities]}


def plan_night(lb: Lakebase, schema: str, req: PlanRequest) -> dict[str, Any]:
    """Night plan without an incident (read-only, no branch): every route fits the night by construction, so
    the parameters move the END of the night — extra helpers add HELPER_CAPACITY of a bringer each and the
    boost speeds up the travel share (TRAVEL_SHARE_DEFAULT, measured on a routed simulation)."""
    helpers, boost = _params(req.helpers, req.boost)
    if req.campaign not in ("nochebuena", "reyes"):
        raise HTTPException(400, "noche no válida")
    sch = _ident(schema)
    conn = lb.connect("production")
    try:
        year = req.year or _query(conn, f"SELECT max(year) AS y FROM {sch}.journeys_sync")[0]["y"]
        routes = _query(conn, f"""
            SELECT j.route_id, j.bringer, count(*) AS stops, min(j.arrived_at_utc) AS start_utc, max(j.arrived_at_utc) AS end_utc,
                   sum(j.dwell_minutes) AS dwell_min, min(h.timezone) AS timezone
            FROM {sch}.journeys_sync j JOIN {sch}.households_sync h USING (household_id)
            WHERE j.year = %s AND j.campaign = %s AND h.city = %s
            GROUP BY j.route_id, j.bringer ORDER BY j.bringer""", (year, req.campaign, req.city))
        sample = _query(conn, f"""
            SELECT h.lat, h.lon, j.bringer FROM {sch}.journeys_sync j JOIN {sch}.households_sync h USING (household_id)
            WHERE j.year = %s AND j.campaign = %s AND h.city = %s
            ORDER BY hashint8(j.household_id) LIMIT %s""", (year, req.campaign, req.city, PLAN_SAMPLE))
    finally:
        conn.close()
    if not routes:
        raise HTTPException(404, f"no hay paradas para {req.city} en {req.campaign} {year}")
    tf = TRAVEL_SHARE_DEFAULT
    load = (1 - tf + tf / boost) / (1 + HELPER_CAPACITY * helpers)
    out = []
    for r in routes:
        dur = r["end_utc"] - r["start_utc"]
        out.append({"route_id": int(r["route_id"]), "bringer": r["bringer"], "stops": int(r["stops"]),
                    "start_utc": r["start_utc"].isoformat(), "end_utc": r["end_utc"].isoformat(),
                    "end_new_utc": (r["start_utc"] + dur * load).isoformat(), "timezone": r["timezone"],
                    "hours": round(dur.total_seconds() / 3600, 2), "hours_new": round(dur.total_seconds() * load / 3600, 2),
                    "stops_per_carrier": round(int(r["stops"]) / (1 + helpers))})
    end_now = max(r["end_utc"] for r in routes)
    start = min(r["start_utc"] for r in routes)
    end_new = start + max(r["end_utc"] - r["start_utc"] for r in routes) * load
    return {"mode": "plan", "city": req.city, "campaign": req.campaign, "year": year,
            "params": {"helpers": helpers, "boost": boost, "travel_share": tf, "helper_capacity": HELPER_CAPACITY},
            "load": round(load, 3), "stops": sum(int(r["stops"]) for r in routes), "routes": out,
            "start_utc": start.isoformat(), "end_utc": end_now.isoformat(), "end_new_utc": end_new.isoformat(),
            "hours_saved": round((end_now - end_new).total_seconds() / 3600, 2),
            "sample": [{"lat": float(x["lat"]), "lon": float(x["lon"]), "b": x["bringer"]} for x in sample]}


def scenarios_preview(lb: Lakebase) -> list[dict[str, Any]]:
    conn = lb.connect("production")
    try:
        return _query(conn, """
          SELECT s.scenario_id, s.city, s.campaign, s.year, s.title, s.story,
                 jsonb_agg(jsonb_build_object('type', 'Feature', 'geometry', ST_AsGeoJSON(z.geom, 5)::jsonb,
                   'properties', jsonb_build_object('zone_id', z.zone_id, 'label', z.label, 'kind', z.kind,
                     'closed', z.closed, 'main_only', z.main_only, 'factor_local', z.factor_local,
                     'appears_at_min', z.appears_at_min)) ORDER BY z.appears_at_min, z.zone_id) AS zones
          FROM navidad_geo.scenario s JOIN navidad_geo.zone z USING (scenario_id)
          GROUP BY 1, 2, 3, 4, 5, 6 ORDER BY s.city""")
    finally:
        conn.close()


def mount_incidents(app: FastAPI, static_dir: Path, client: WorkspaceClient | None = None) -> Simulations:
    project_id = os.environ.get("NAVIDAD_PROJECT_ID", "")
    schema = os.environ.get("NAVIDAD_SCHEMA", "public")
    lb = Lakebase(project_id, client)
    sims = Simulations(lb, schema)

    @app.get("/planner")
    def planner_page() -> FileResponse:
        return FileResponse(static_dir / "planner.html")

    @app.get("/incidencias")
    def incidents_page() -> RedirectResponse:  # old name: the page is now the Planificador
        return RedirectResponse("/planner", status_code=307)

    @app.get("/api/incidents/scenarios")
    def incidents_scenarios() -> dict[str, Any]:
        try:
            return {"scenarios": scenarios_preview(lb)}
        except Exception as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.get("/api/plan/options")
    def plan_opts() -> dict[str, Any]:
        try:
            return plan_options(lb, schema)
        except Exception as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.post("/api/plan")
    def plan(req: PlanRequest) -> dict[str, Any]:
        try:
            return plan_night(lb, schema, req)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.get("/api/sim")
    def sim_list() -> dict[str, Any]:
        return {"simulations": sims.listing(), "max_branches": MAX_SIM_BRANCHES}

    @app.post("/api/sim")
    def sim_start(req: SimRequest) -> dict[str, str]:
        return {"branch": sims.start(req.scenario, req.minute, req.helpers, req.boost)}

    @app.get("/api/sim/{branch}")
    def sim_get(branch: str) -> dict[str, Any]:
        return sims.view(branch)

    @app.post("/api/sim/{branch}/minute")
    def sim_minute(branch: str, req: MinuteRequest) -> dict[str, str]:
        sims.rerun(branch, req.minute)
        return {"branch": branch}

    @app.post("/api/sim/{branch}/params")
    def sim_params(branch: str, req: ParamsRequest) -> dict[str, str]:
        sims.replan(branch, req.helpers, req.boost)
        return {"branch": branch}

    @app.post("/api/sim/{branch}/apply")
    def sim_apply(branch: str) -> dict[str, Any]:
        try:
            return sims.apply(branch)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.delete("/api/sim/{branch}")
    def sim_delete(branch: str) -> dict[str, str]:
        try:
            sims.discard(branch)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, str(exc)) from exc
        return {"deleted": branch}

    return sims
