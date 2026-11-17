"""Real-time control center («Centro de control»): /control + /api/control/*.

Reads the live state that the streaming sink (or the producer in «lakebase_directo» mode)
writes into Lakebase ``navidad_live``; the browser polls ``/api/control/state`` every ~1.5 s
with a cursor (``updated_at``), so each call returns only what changed.

Exceptions are reviewed by an agent: it gathers context with SQL (household, nearby helpers,
similar exceptions, active incident zones), asks the model to pick 2–3 actions **from a fixed
catalogue**, stores them as ``propuesta`` and changes nothing until a person confirms.
If the model is unavailable or answers badly, deterministic rules propose the actions.

Mount with ``mount_control(app, static_dir, complete)``; needs NAVIDAD_PROJECT_ID and
NAVIDAD_SCHEMA, and the grants from provision_live.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

import psycopg2
from databricks.sdk import WorkspaceClient
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from psycopg2.extras import Json, RealDictCursor
from pydantic import BaseModel

PG_DATABASE = "databricks_postgres"
AGENT_MAX_TOKENS = 900
RECENT_LIMIT = 3000
STATUS_CODE = {"entregado": "e", "excepcion": "x"}  # compact codes for the live dots (both words start with «e»)
OPEN_LIMIT = 200

ACTIONS = {  # code: Spanish label template
    "reasignar_ayudante": "Reasignar a {helper_name} ({distance_m} m)",
    "reintentar": "Reintentar dentro de {minutos} min",
    "punto_recogida": "Dejar los regalos en un punto de recogida cercano",
    "avisar_familia": "Avisar a la familia",
    "escalar": "Escalar a {a}",
    "cerrar": "Cerrar sin acción",
}
RULES = {  # exception_code → default actions (fallback, and a hint for the model)
    "chimenea_bloqueada": ["reasignar_ayudante", "punto_recogida"],
    "nino_despierto": ["reintentar", "reasignar_ayudante"],
    "perro_guardian": ["reasignar_ayudante", "reintentar"],
    "direccion_no_encontrada": ["avisar_familia", "punto_recogida"],
    "regalo_equivocado": ["escalar", "reasignar_ayudante"],
    "retraso_transporte": ["reasignar_ayudante", "reintentar"],
    "clima": ["punto_recogida", "reintentar"],
}
CODE_NAMES = {"chimenea_bloqueada": "Chimenea bloqueada", "nino_despierto": "Niño despierto",
              "perro_guardian": "Perro guardián", "direccion_no_encontrada": "Dirección no encontrada",
              "regalo_equivocado": "Regalo equivocado", "retraso_transporte": "Retraso del transporte",
              "clima": "Clima"}
BRINGER_NAMES = {"papa_noel": "Papá Noel", "olentzero": "Olentzero", "tio": "Tió de Nadal", "apalpador": "Apalpador",
                 "melchor": "Melchor", "gaspar": "Gaspar", "baltasar": "Baltasar"}

DIST_SQL = ("round(6371000 * 2 * asin(sqrt(power(sin(radians(lat - %(lat)s) / 2), 2) + "
            "cos(radians(%(lat)s)) * cos(radians(lat)) * power(sin(radians(lon - %(lon)s) / 2), 2))))::int")


class Db:
    """One connection as the app's service principal, reopened when the compute drops it.

    (The app is deployed standalone, so this is a small local copy of scripts/pg.py's Pg.)
    LAKEBASE_ENDPOINT (set by deploy_app) picks the branch; a suspended compute is woken with retries."""

    def __init__(self, project_id: str, client: WorkspaceClient | None = None, endpoint: str | None = None,
                 attempts: int = 8):
        self.project_id, self._client, self._conn = project_id, client, None
        self.endpoint = endpoint or os.environ.get("LAKEBASE_ENDPOINT") or \
            f"projects/{project_id}/branches/production/endpoints/primary"
        self.attempts = attempts
        self.lock = threading.Lock()          # guards the pool, not the queries
        self._idle: list[Any] = []            # small pool: concurrent viewers don't queue behind one connection
        self.max_idle = 4

    @property
    def client(self) -> WorkspaceClient:
        if self._client is None:
            self._client = WorkspaceClient()
        return self._client

    def _open(self) -> Any:
        last: Exception | None = None
        for i in range(self.attempts):
            try:
                host = self.client.postgres.get_endpoint(name=self.endpoint).status.hosts.host
                me = self.client.current_user.me()
                token = self.client.postgres.generate_database_credential(endpoint=self.endpoint).token
                conn = psycopg2.connect(host=host, user=me.user_name or me.application_id, password=token,
                                        dbname=PG_DATABASE, sslmode="require", connect_timeout=30,
                                        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
                conn.autocommit = True
                return conn
            except Exception as exc:  # compute waking up from scale-to-zero
                last = exc
                time.sleep(min(1 + i, 5))
        raise RuntimeError(f"cannot connect to {self.endpoint}: {last}")

    def _get(self) -> Any:
        with self.lock:
            while self._idle:
                conn = self._idle.pop()
                if not conn.closed:
                    return conn
        return self._open()

    def _put(self, conn: Any) -> None:
        with self.lock:
            if not conn.closed and len(self._idle) < self.max_idle:
                self._idle.append(conn)
                return
        try:
            conn.close()
        except Exception:
            pass

    def run(self, fn: Callable[[Any], Any]) -> Any:
        for attempt in (1, 2):
            conn = self._get()
            try:
                out = fn(conn)
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                self._put(conn)  # closed connections are dropped
                if attempt == 2 or not conn.closed:
                    raise
                continue
            except Exception:
                self._put(conn)
                raise
            self._put(conn)
            return out

    def q(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        def go(conn):
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()] if cur.description else []
        return self.run(go)

    def tx(self, fn: Callable[[Any], Any]) -> Any:
        def go(conn):
            conn.autocommit = False
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    out = fn(cur)
                conn.commit()
                return out
            except Exception:
                if not conn.closed:
                    conn.rollback()
                raise
            finally:
                if not conn.closed:
                    conn.autocommit = True
        return self.run(go)


class ReadRouter:
    """Reads go to a read replica when the branch has one (bonus «Réplica de lectura»), writes stay on the
    primary. The replica is LAKEBASE_READ_ENDPOINT or the branch's first READ_ONLY endpoint, re-checked
    every minute; if it is missing or failing, reads fall back to the primary."""

    RECHECK_S = 60.0

    def __init__(self, primary: Db):
        self.primary, self._replica, self._checked, self._down_until = primary, None, 0.0, 0.0
        self.lock = threading.Lock()

    def _resolve(self) -> Db | None:
        with self.lock:
            if time.time() - self._checked < self.RECHECK_S:
                return self._replica
            self._checked = time.time()
        name = os.environ.get("LAKEBASE_READ_ENDPOINT")
        if not name:
            branch = self.primary.endpoint.rsplit("/endpoints/", 1)[0]
            try:
                ro = [e.name for e in self.primary.client.postgres.list_endpoints(parent=branch)
                      if e.status and "READ_ONLY" in str(e.status.endpoint_type)]
            except Exception:
                ro = []
            name = ro[0] if ro else None
        with self.lock:
            if not name:
                self._replica = None
            elif not self._replica or self._replica.endpoint != name:
                self._replica = Db(self.primary.project_id, self.primary._client, endpoint=name, attempts=2)
            return self._replica

    @property
    def target(self) -> str:
        r = self._resolve()
        return "replica" if r is not None and time.time() >= self._down_until else "primary"

    @property
    def endpoint(self) -> str:
        r = self._resolve()
        return r.endpoint if r is not None and time.time() >= self._down_until else self.primary.endpoint

    def q(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        r = self._resolve()
        if r is not None and time.time() >= self._down_until:
            try:
                return r.q(sql, params)
            except (psycopg2.OperationalError, psycopg2.InterfaceError, RuntimeError):
                self._down_until = time.time() + 30  # replica unavailable: primary for 30 s
        return self.primary.q(sql, params)

    @property
    def client(self) -> Any:
        return self.primary.client

    @property
    def project_id(self) -> str:
        return self.primary.project_id


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _clean(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: _iso(v) for k, v in r.items()} for r in rows]


# ------------------------------------------------------------------ state
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
CACHE_S = 1.0


def state(db: Db, run: str | None, since: str | None) -> dict[str, Any]:
    """Run-wide part (KPIs, cities, helpers, open exceptions) is computed at most once per second and shared
    by every viewer; only the «changed since your cursor» rows are per request."""
    key = run or ""
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_S:
        base = hit[1]
    else:
        base = _run_state(db, run)
        with _CACHE_LOCK:
            _CACHE[key] = (time.time(), base)
    if not base.get("run"):
        return base
    since_ts = since or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=15)).isoformat()
    recent = db.q("""SELECT lat, lon, status, updated_at FROM navidad_live.delivery_status
                     WHERE sim_run_id = %s AND updated_at > %s ORDER BY updated_at LIMIT %s""",
                  (base["run"]["sim_run_id"], since_ts, RECENT_LIMIT))
    reads = {"target": getattr(db, "target", "primary"), "endpoint": getattr(db, "endpoint", "").rsplit("/", 1)[-1]}
    return {**base, "recent": [{"lat": r["lat"], "lon": r["lon"], "s": STATUS_CODE.get(r["status"], "?")} for r in recent],
            "cursor": _iso(recent[-1]["updated_at"]) if recent else since_ts, "reads": reads}


def invalidate() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _run_state(db: Db, run: str | None) -> dict[str, Any]:
    runs = db.q("SELECT * FROM navidad_live.sim_run ORDER BY started_at DESC LIMIT 10")
    if not runs:
        return {"run": None, "runs": []}
    cur = next((r for r in runs if r["sim_run_id"] == run), runs[0])
    rid = cur["sim_run_id"]
    k = db.q("""
        SELECT d.delivered, d.per_min, d.clock, d.last_update, x.total, x.open, x.by_agent, x.by_retry, l.p50, l.p95, c.s AS cycle
        FROM (SELECT count(*) FILTER (WHERE status = 'entregado') AS delivered,
                     count(*) FILTER (WHERE status = 'entregado' AND updated_at > now() - interval '60 seconds') AS per_min,
                     max(event_ts) AS clock, max(updated_at) AS last_update
              FROM navidad_live.delivery_status WHERE sim_run_id = %(r)s) d,
             (SELECT count(*) AS total, count(*) FILTER (WHERE state IN ('abierta', 'en_revision')) AS open,
                     count(*) FILTER (WHERE state = 'resuelta' AND resolved_by IS NOT NULL) AS by_agent,
                     count(*) FILTER (WHERE state = 'resuelta' AND resolved_by IS NULL) AS by_retry
              FROM navidad_live.v_exception WHERE sim_run_id = %(r)s) x,
             (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM updated_at - sent_ts)) AS p50,
                     percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM updated_at - sent_ts)) AS p95
              FROM navidad_live.delivery_status WHERE sim_run_id = %(r)s AND updated_at > now() - interval '30 seconds') l,
             (SELECT round(avg(seconds)::numeric, 1) AS s FROM navidad_live.stream_progress
              WHERE started_at > now() - interval '60 seconds' AND queries ? 'delivery_status') c""", {"r": rid})[0]
    cities = db.q("SELECT city, lat, lon, planned, delivered, exceptions_open, processed, exceptions FROM navidad_live.v_city_progress "
                  "WHERE sim_run_id = %s ORDER BY planned DESC", (rid,))
    helpers = db.q("""SELECT helper_id, helper_name, helper_kind, bringer, city, lat, lon, updated_at
                      FROM navidad_live.helper_position WHERE sim_run_id = %s""", (rid,))
    opened = db.q("""SELECT event_id, household_id, city, bringer, helper_id, helper_name, helper_kind, exception_code, lat, lon,
                            event_ts, state FROM navidad_live.v_exception
                     WHERE sim_run_id = %s AND state IN ('abierta', 'en_revision')
                     ORDER BY event_ts DESC LIMIT %s""", (rid, OPEN_LIMIT))
    return {
        "run": _clean([cur])[0], "runs": _clean(runs),
        "kpis": {"planned": cur["planned"], "delivered": k["delivered"], "per_min": k["per_min"],
                 "clock": _iso(k["clock"]), "last_update": _iso(k["last_update"]),
                 "exceptions": k["total"], "exceptions_open": k["open"], "resolved_agent": k["by_agent"],
                 "resolved_retry": k["by_retry"],
                 "latency_p50": round(float(k["p50"]), 3) if k["p50"] is not None else None,
                 "latency_p95": round(float(k["p95"]), 3) if k["p95"] is not None else None,
                 "stream_cycle_s": float(k["cycle"]) if k["cycle"] is not None else None},
        "cities": _clean(cities), "helpers": _clean(helpers), "exceptions": _clean(opened),
    }


# ------------------------------------------------------------------ trends (KPIs every 5 or 30 s)
METRIC_BUCKETS = {5: 120, 30: 40}  # bucket seconds → buckets kept (10 min at 5 s, 20 min at 30 s)


def metrics(db: Db, run: str, bucket: int) -> dict[str, Any]:
    """Per-bucket series for the KPI sparklines, bucketed on when rows landed in Lakebase (updated_at):
    deliveries, new exceptions, latency p50/p95 (updated_at − sent_ts). Shared by viewers for 2 s."""
    if bucket not in METRIC_BUCKETS:
        raise ValueError("bucket must be 5 or 30")
    key = f"m|{run}|{bucket}"
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < 2.0:
        return hit[1]
    p = {"r": run, "b": f"{bucket} seconds", "w": f"{bucket * METRIC_BUCKETS[bucket]} seconds"}
    rows = db.q("""
        WITH d AS (
          SELECT date_bin(%(b)s::interval, updated_at, '2000-01-01') AS t,
                 count(*) FILTER (WHERE status = 'entregado') AS delivered,
                 percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM updated_at - sent_ts)) AS p50,
                 percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM updated_at - sent_ts)) AS p95
          FROM navidad_live.delivery_status
          WHERE sim_run_id = %(r)s AND updated_at > now() - %(w)s::interval GROUP BY 1),
        x AS (
          SELECT date_bin(%(b)s::interval, updated_at, '2000-01-01') AS t, count(*) AS exceptions
          FROM navidad_live.exception_event
          WHERE sim_run_id = %(r)s AND updated_at > now() - %(w)s::interval GROUP BY 1)
        SELECT t, COALESCE(d.delivered, 0) AS delivered, COALESCE(x.exceptions, 0) AS exceptions, d.p50, d.p95
        FROM d FULL JOIN x USING (t) ORDER BY t""", p)
    out = {"bucket": bucket, "series": [{"t": _iso(r["t"]), "delivered": r["delivered"], "exceptions": r["exceptions"],
                                         "p50": round(float(r["p50"]), 3) if r["p50"] is not None else None,
                                         "p95": round(float(r["p95"]), 3) if r["p95"] is not None else None}
                                        for r in rows]}
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), out)
    return out


_ZONES: dict[str, Any] = {}


def zones(db: Db, scenario: str) -> dict[str, Any]:
    """GeoJSON of an incident scenario's zones (navidad_geo, incidents track) — drawn while its run is shown."""
    if scenario not in INCIDENTS - {"ninguna"}:
        raise ValueError("escenario no válido")
    if scenario not in _ZONES:
        try:
            rows = db.q("""SELECT label, kind, ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.0005))::json AS g
                           FROM navidad_geo.zone WHERE scenario_id = %s""", (scenario,))
        except Exception:
            rows = []
        _ZONES[scenario] = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": r["g"], "properties": {"label": r["label"], "kind": r["kind"]}} for r in rows]}
    return _ZONES[scenario]


# ------------------------------------------------------------------ agent
def context(db: Db, exception_id: str) -> dict[str, Any]:
    rows = db.q("SELECT * FROM navidad_live.v_exception WHERE event_id = %s", (exception_id,))
    if not rows:
        raise KeyError(exception_id)
    e = rows[0]
    hh = db.q("SELECT label, household_size, children, window_policy, timezone FROM navidad_live.v_household "
              "WHERE household_id = %s", (e["household_id"],))
    p = {"lat": e["lat"], "lon": e["lon"], "run": e["sim_run_id"], "city": e["city"], "helper": e["helper_id"]}
    nearby = db.q(f"""SELECT helper_id, helper_name, helper_kind, {DIST_SQL} AS distance_m
                      FROM navidad_live.helper_position
                      WHERE sim_run_id = %(run)s AND city = %(city)s AND helper_id <> %(helper)s
                      ORDER BY distance_m LIMIT 3""", p)
    similar = db.q("""SELECT count(*) AS n FROM navidad_live.v_exception
                      WHERE sim_run_id = %s AND city = %s AND exception_code = %s AND state IN ('abierta', 'en_revision')""",
                   (e["sim_run_id"], e["city"], e["exception_code"]))[0]["n"]
    try:
        zones = [z["label"] for z in db.q(
            "SELECT label FROM navidad_geo.zone WHERE active AND ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))",
            (e["lon"], e["lat"]))]
    except Exception:  # incident track not installed
        zones = []
    return {"exception": _clean([e])[0], "household": _clean(hh)[0] if hh else {}, "nearby_helpers": nearby,
            "similar_open_in_city": similar, "active_incident_zones": zones,
            "bringer_name": BRINGER_NAMES.get(e["bringer"], e["bringer"]),
            "code_name": CODE_NAMES.get(e["exception_code"], e["exception_code"])}


def _action(code: str, ctx: dict[str, Any], params: dict[str, Any] | None = None, effect: str | None = None) -> dict | None:
    params = dict(params or {})
    if code == "reasignar_ayudante":
        helpers = {h["helper_id"]: h for h in ctx["nearby_helpers"]}
        h = helpers.get(params.get("helper_id")) or (ctx["nearby_helpers"][0] if ctx["nearby_helpers"] else None)
        if not h:
            return None
        params = {"helper_id": h["helper_id"], "helper_name": h["helper_name"], "distance_m": h["distance_m"]}
    elif code == "reintentar":
        params = {"minutos": max(5, min(120, int(params.get("minutos") or 30)))}
    elif code == "escalar":
        params = {"a": ctx["bringer_name"]}
    elif code == "avisar_familia":
        params = {"mensaje": str(params.get("mensaje") or "Hemos tenido un pequeño contratiempo; volvemos en un rato.")[:200]}
    elif code not in ACTIONS:
        return None
    else:
        params = {}
    return {"code": code, "params": params, "label": ACTIONS[code].format(**params),
            "effect": (effect or "")[:200] or None}


def rules(ctx: dict[str, Any]) -> tuple[str, list[dict]]:
    code = ctx["exception"]["exception_code"]
    acts = [a for a in (_action(c, ctx) for c in RULES.get(code, ["reintentar", "cerrar"])) if a]
    diag = f"{ctx['code_name']} en {ctx['exception']['city']}."
    if ctx["similar_open_in_city"] > 1:
        diag += f" Hay {ctx['similar_open_in_city']} casos parecidos abiertos en la ciudad."
    if ctx["active_incident_zones"]:
        diag += f" El hogar está dentro de la zona «{ctx['active_incident_zones'][0]}»."
    return diag, acts


PROMPT = """Eres el coordinador de la noche de reparto de regalos ({bringer}). Revisa esta incidencia y propone entre 2 y 3 acciones.
Usa SOLO códigos del catálogo, rellena sus parámetros y explica el efecto en una frase. Responde SOLO con JSON:
{{"diagnostico": "<1-2 frases en español>", "acciones": [{{"codigo": "...", "parametros": {{}}, "efecto": "..."}}]}}

Catálogo: reasignar_ayudante{{helper_id}} (uno de los ayudantes cercanos), reintentar{{minutos}}, punto_recogida{{}},
avisar_familia{{mensaje}}, escalar{{}}, cerrar{{}}. Acciones típicas para «{code}»: {hint}.

Contexto (consultado en Lakebase):
{ctx}"""


def review(db: Db, exception_id: str, complete: Callable[[str], str] | None, model: str,
           reader: Any = None) -> dict[str, Any]:
    """Context is read from ``reader`` (the read replica when there is one); decisions are written on ``db``."""
    t0 = time.time()
    ctx = context(reader or db, exception_id)
    if ctx["exception"]["state"] in ("resuelta", "descartada"):
        raise ValueError(f"la incidencia ya está {ctx['exception']['state']}")
    source, diag, acts = "reglas", None, []
    if complete:
        try:
            raw = complete(PROMPT.format(bringer=ctx["bringer_name"], code=ctx["code_name"],
                                         hint=", ".join(RULES.get(ctx["exception"]["exception_code"], [])),
                                         ctx=json.dumps(ctx, ensure_ascii=False, default=str)))
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0)) if m else {}
            acts = [a for a in (_action(str(x.get("codigo")), ctx, x.get("parametros"), x.get("efecto"))
                                for x in (data.get("acciones") or [])[:3]) if a]
            diag = str(data.get("diagnostico") or "")[:400]
            if acts and diag:
                source = "llm"
        except Exception:
            acts = []
    if source == "reglas":
        diag, acts = rules(ctx)
    latency = int((time.time() - t0) * 1000)

    def write(cur):
        cur.execute("""INSERT INTO navidad_live.agent_review (exception_id, model, context, diagnosis, source, latency_ms)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING review_id""",
                    (exception_id, model if source == "llm" else None, Json(ctx), diag, source, latency))
        rid = cur.fetchone()["review_id"]
        cur.execute("UPDATE navidad_live.agent_action SET status = 'descartada' WHERE exception_id = %s AND status = 'propuesta'",
                    (exception_id,))
        out = []
        for a in acts:
            cur.execute("""INSERT INTO navidad_live.agent_action (review_id, exception_id, action_code, params, label, effect)
                           VALUES (%s, %s, %s, %s, %s, %s) RETURNING action_id""",
                        (rid, exception_id, a["code"], Json(a["params"]), a["label"], a["effect"]))
            out.append({**a, "action_id": cur.fetchone()["action_id"], "status": "propuesta"})
        cur.execute("""INSERT INTO navidad_live.exception_state (exception_id, status, updated_by)
                       VALUES (%s, 'en_revision', 'agente') ON CONFLICT (exception_id)
                       DO UPDATE SET status = 'en_revision', updated_at = now()
                       WHERE navidad_live.exception_state.status <> 'resuelta'""", (exception_id,))
        return rid, out
    rid, out = db.tx(write)
    return {"review_id": rid, "source": source, "diagnosis": diag, "actions": out, "latency_ms": latency,
            "context": ctx}


def decide(db: Db, action_id: int, confirm: bool, user: str) -> dict[str, Any]:
    def write(cur):
        cur.execute("SELECT * FROM navidad_live.agent_action WHERE action_id = %s FOR UPDATE", (action_id,))
        a = cur.fetchone()
        if not a:
            raise KeyError(action_id)
        if a["status"] != "propuesta":
            raise ValueError(f"la acción ya está {a['status']}")
        new = "confirmada" if confirm else "rechazada"
        if confirm:
            cur.execute("SELECT state FROM navidad_live.v_exception WHERE event_id = %s", (a["exception_id"],))
            st = (cur.fetchone() or {}).get("state")
            if st in ("resuelta", "descartada"):  # e.g. a retry delivered it while the proposal was pending
                cur.execute("UPDATE navidad_live.agent_action SET status = 'descartada' WHERE review_id = %s AND status = 'propuesta'",
                            (a["review_id"],))
                return {"action_id": action_id, "status": "descartada", "exception_id": a["exception_id"],
                        "reason": f"la incidencia ya estaba {st}"}
        cur.execute("UPDATE navidad_live.agent_action SET status = %s, decided_by = %s, decided_at = now() WHERE action_id = %s",
                    (new, user, action_id))
        if confirm:
            cur.execute("""UPDATE navidad_live.agent_action SET status = 'descartada'
                           WHERE review_id = %s AND action_id <> %s AND status = 'propuesta'""", (a["review_id"], action_id))
            cur.execute("""INSERT INTO navidad_live.exception_state (exception_id, status, resolution, updated_by)
                           VALUES (%s, 'resuelta', %s, %s) ON CONFLICT (exception_id)
                           DO UPDATE SET status = 'resuelta', resolution = EXCLUDED.resolution,
                                         updated_by = EXCLUDED.updated_by, updated_at = now()""",
                        (a["exception_id"], a["label"], user))
        else:
            cur.execute("SELECT count(*) AS n FROM navidad_live.agent_action WHERE review_id = %s AND status = 'propuesta'",
                        (a["review_id"],))
            if cur.fetchone()["n"] == 0:
                cur.execute("UPDATE navidad_live.exception_state SET status = 'abierta', updated_at = now() "
                            "WHERE exception_id = %s AND status = 'en_revision'", (a["exception_id"],))
        return {"action_id": action_id, "status": new, "exception_id": a["exception_id"]}
    return db.tx(write)


def exception_detail(db: Db, exception_id: str) -> dict[str, Any]:
    ctx = context(db, exception_id)
    reviews = db.q("SELECT review_id, diagnosis, source, latency_ms, created_at FROM navidad_live.agent_review "
                   "WHERE exception_id = %s ORDER BY created_at DESC LIMIT 5", (exception_id,))
    actions = db.q("SELECT action_id, review_id, action_code, params, label, effect, status, decided_by, decided_at "
                   "FROM navidad_live.agent_action WHERE exception_id = %s ORDER BY action_id", (exception_id,))
    return {**ctx, "reviews": _clean(reviews), "actions": _clean(actions)}


# ------------------------------------------------------------------ ▶ / ■ : the navidad-live-<slug> job
ACTIVE_STATES = {"QUEUED", "PENDING", "RUNNING", "TERMINATING", "BLOCKED", "WAITING_FOR_RETRY"}
CAMPAIGNS = {"nochebuena", "reyes"}
DESTINOS = {"zerobus", "lakebase_directo"}
INCIDENTS = {"ninguna", "dana_valencia", "cortes_barcelona", "filomena_madrid"}


class StartRequest(BaseModel):
    campaign: str = "nochebuena"
    minutes: int = 15
    destino: str = "zerobus"
    incident: str = "ninguna"


def job_name(project_id: str) -> str:
    return "navidad-live-" + project_id.removeprefix("navidad-")


class LiveJob:
    """Start/stop the control-center job (notebooks 04 + 05). The app's service principal has CAN_MANAGE_RUN;
    runs execute as the job owner. Created by notebook 04 (ensure_live_job)."""

    def __init__(self, db: Db):
        self.db, self._id = db, None

    def job_id(self) -> int:
        if self._id is None:
            name = job_name(self.db.project_id)
            found = list(self.db.client.jobs.list(name=name))
            if not found:
                raise KeyError(f"job {name}: ejecuta una vez el notebook 04_tiempo_real_stream para crearlo")
            self._id = found[0].job_id
        return self._id

    def _runs(self, active_only: bool) -> list[Any]:
        return list(self.db.client.jobs.list_runs(job_id=self.job_id(), active_only=active_only, limit=1))

    def status(self) -> dict[str, Any]:
        try:
            runs = self._runs(active_only=False)
        except KeyError as exc:
            return {"available": False, "message": str(exc)}
        if not runs:
            return {"available": True, "active": False}
        r = runs[0]
        life = r.state.life_cycle_state.value if r.state and r.state.life_cycle_state else None
        result = r.state.result_state.value if r.state and r.state.result_state else None
        params = {p.name: p.value for p in (r.job_parameters or []) if p.name}
        return {"available": True, "active": life in ACTIVE_STATES, "run_id": r.run_id, "state": life,
                "result": result, "url": r.run_page_url,
                "started": dt.datetime.fromtimestamp(r.start_time / 1000, dt.timezone.utc).isoformat() if r.start_time else None,
                "params": {k: params.get(k) for k in ("campaign", "minutes", "destino", "incident")}}

    def start(self, req: StartRequest) -> dict[str, Any]:
        if req.campaign not in CAMPAIGNS or req.destino not in DESTINOS or req.incident not in INCIDENTS \
                or not 2 <= req.minutes <= 60:
            raise ValueError("parámetros no válidos")
        if self._runs(active_only=True):
            raise ValueError("ya hay una noche en marcha: párala antes de empezar otra")
        run = self.db.client.jobs.run_now(job_id=self.job_id(), job_parameters={
            "campaign": req.campaign, "minutes": str(req.minutes), "stream_minutes": str(req.minutes + 6),
            "destino": req.destino, "incident": req.incident})
        return {"run_id": run.run_id, "state": "QUEUED"}

    def stop(self) -> dict[str, Any]:
        runs = self._runs(active_only=True)
        if not runs:
            # nothing launched from the app is running (e.g. 05 started by hand): leave its night alone
            raise ValueError("no hay ninguna noche del job en marcha; si lanzaste 05 a mano, páralo en el notebook")
        cancelled, failed = [], []
        for r in runs:
            try:
                self.db.client.jobs.cancel_run(run_id=r.run_id)
                cancelled.append(r.run_id)
            except Exception as exc:  # keep going, report below
                failed.append(f"{r.run_id}: {exc}")
        if failed:
            raise RuntimeError("no se pudo parar el job: " + "; ".join(failed))
        # the producer may be killed before its finally: block, so close the night here
        self.db.q("UPDATE navidad_live.sim_run SET status = 'cancelada', ended_at = now() WHERE status = 'en_curso'")
        return {"cancelled": cancelled}


NOT_PREPARED = ("El centro de control aún no está preparado en este workbench: ejecuta el paso 5 de 00_workbench "
                "(o 04_tiempo_real_stream una vez) y recarga.")


def _not_prepared(exc: BaseException) -> bool:
    """Only the control center's own schema missing (navidad_live tables/views): 42P01 undefined_table /
    3F000 invalid_schema_name *about navidad_live*. Other missing relations (navidad_geo, *_sync) keep
    their real error instead of pointing the user at 00 step 5."""
    msg = str(exc)
    if "navidad_live" not in msg:
        return False
    return getattr(exc, "pgcode", None) in ("42P01", "3F000") or "does not exist" in msg


def mount_control(app: FastAPI, static_dir: Path, complete: Callable[[str], str] | None = None,
                  client: WorkspaceClient | None = None) -> Db:
    """``complete(prompt) -> text``: the app's gateway helper, wired by add_control_center with the agent
    model (NAVIDAD_AGENT_MODEL, else NAVIDAD_MODEL) and a token budget big enough for the JSON answer."""
    db = Db(os.environ.get("NAVIDAD_PROJECT_ID", ""), client)      # primary: writes (+ reads inside transactions)
    reader = ReadRouter(db)                                         # read replica if the branch has one
    model = os.environ.get("NAVIDAD_AGENT_MODEL") or os.environ.get("NAVIDAD_MODEL", "")

    def user(request: Request) -> str:
        return request.headers.get("x-forwarded-email") or request.headers.get("x-forwarded-user") or "usuario"

    def guard(fn, *a):
        try:
            return fn(*a)
        except HTTPException:
            raise
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no existe: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            if _not_prepared(exc):  # 00 step 5 / 04 not run yet in this workbench
                raise HTTPException(status_code=503, detail=NOT_PREPARED) from exc
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/control")
    def control_page() -> FileResponse:
        return FileResponse(static_dir / "control.html")

    @app.get("/api/control/state")
    def control_state(run: str | None = None, since: str | None = None) -> dict[str, Any]:
        return guard(state, reader, run, since)

    job = LiveJob(db)

    @app.get("/api/control/job")
    def control_job() -> dict[str, Any]:
        return guard(job.status)

    @app.post("/api/control/job/start")
    def control_job_start(req: StartRequest) -> dict[str, Any]:
        out = guard(job.start, req)
        invalidate()
        return out

    @app.post("/api/control/job/stop")
    def control_job_stop() -> dict[str, Any]:
        out = guard(job.stop)
        invalidate()
        return out

    @app.get("/api/control/metrics")
    def control_metrics(run: str, bucket: int = 5) -> dict[str, Any]:
        return guard(metrics, reader, run, bucket)

    @app.get("/api/control/zones")
    def control_zones(scenario: str) -> dict[str, Any]:
        return guard(zones, reader, scenario)

    @app.get("/api/control/exceptions/{exception_id}")
    def control_exception(exception_id: str) -> dict[str, Any]:
        return guard(exception_detail, reader, exception_id)

    @app.post("/api/control/exceptions/{exception_id}/review")
    def control_review(exception_id: str) -> dict[str, Any]:
        out = guard(review, db, exception_id, complete, model, reader)
        invalidate()
        return out

    @app.post("/api/control/actions/{action_id}/confirm")
    def control_confirm(action_id: int, request: Request) -> dict[str, Any]:
        out = guard(decide, db, action_id, True, user(request))
        invalidate()
        return out

    @app.post("/api/control/actions/{action_id}/reject")
    def control_reject(action_id: int, request: Request) -> dict[str, Any]:
        out = guard(decide, db, action_id, False, user(request))
        invalidate()
        return out

    return db
