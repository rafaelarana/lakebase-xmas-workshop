"""Provision the real-time control center («Centro de control»).

  1. Lakebase: schema navidad_live (sql/live.sql) + grants for the app's service principal
  2. Unity Catalog: bronze Delta table for Zerobus + a volume for streaming checkpoints
  3. Zerobus identity: a service principal with an OAuth secret in a secret scope, and the
     explicit table grants Zerobus needs (MODIFY + SELECT on the bronze table)

Everything is idempotent. Runs from a notebook (needs `spark` for the UC DDL) or from a
script with a SQL runner. Nothing here touches the *_sync tables.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

SKILL_ROOT = Path(__file__).resolve().parents[1]
SQL_FILE = SKILL_ROOT / "sql" / "live.sql"
PG_DATABASE = "databricks_postgres"
SECRET_SCOPE = "navidad-live"
INGEST_SP = "navidad-live-ingest"
BRONZE_TABLE = "delivery_events_bronze"
CHECKPOINT_VOLUME = "live_checkpoints"

# Bronze = the Zerobus target. No column defaults: Zerobus rejects tables with
# delta.feature.allowColumnDefaults (error 1015). Timestamps arrive as epoch microseconds.
BRONZE_COLUMNS = (
    "event_id STRING, sent_ts TIMESTAMP, event_ts TIMESTAMP, sim_run_id STRING, year INT, campaign STRING, "
    "city STRING, route_id BIGINT, stop_number BIGINT, household_id BIGINT, bringer STRING, helper_id STRING, "
    "helper_name STRING, helper_kind STRING, event_type STRING, exception_code STRING, gifts INT, "
    "lat DOUBLE, lon DOUBLE"
)

APP_READ = ("sim_run", "run_plan", "delivery_status", "helper_position", "exception_event", "stream_progress",
            "v_exception", "v_city_progress", "v_household")
APP_WRITE = ("exception_state", "agent_review", "agent_action")


def _ident(name: str) -> str:
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError(f"unsafe identifier {name!r}")
    return name


def live_sql(schema: str) -> str:
    return SQL_FILE.read_text().replace("__SCHEMA__", _ident(schema))


def uc_statements(catalog: str, schema: str) -> list[str]:
    fq = f"{_ident(catalog)}.{_ident(schema)}"
    return [
        f"CREATE TABLE IF NOT EXISTS {fq}.{BRONZE_TABLE} ({BRONZE_COLUMNS}) "
        "COMMENT 'Centro de control: eventos de entrega vía Zerobus (append-only, at-least-once)'",
        f"CREATE VOLUME IF NOT EXISTS {fq}.{CHECKPOINT_VOLUME}",
    ]


def zerobus_grants(catalog: str, schema: str, application_id: str) -> list[str]:
    fq = f"{_ident(catalog)}.{_ident(schema)}"
    p = f"`{application_id}`"
    return [
        f"GRANT USE CATALOG ON CATALOG {_ident(catalog)} TO {p}",
        f"GRANT USE SCHEMA ON SCHEMA {fq} TO {p}",
        f"GRANT MODIFY, SELECT ON TABLE {fq}.{BRONZE_TABLE} TO {p}",  # explicit table grant (error 4024 otherwise)
    ]


def zerobus_endpoint(workspace_id: int | str, lakebase_host: str, cloud: str = "aws") -> str:
    """<workspace-id>.zerobus.<region>.cloud.databricks.com, region taken from the Lakebase host."""
    parts = lakebase_host.split(".")
    region = parts[parts.index("database") + 1] if "database" in parts else None
    if not region:
        raise ValueError(f"cannot derive the region from {lakebase_host!r}")
    suffix = "azuredatabricks.net" if cloud == "azure" else "cloud.databricks.com"
    return f"{workspace_id}.zerobus.{region}.{suffix}"


def app_grants(app_sp: str) -> list[str]:
    sp = '"' + app_sp.replace('"', "") + '"'
    out = [f"GRANT USAGE ON SCHEMA navidad_live TO {sp}"]
    out += [f"GRANT SELECT ON navidad_live.{t} TO {sp}" for t in APP_READ]
    out += [f"GRANT SELECT, INSERT, UPDATE ON navidad_live.{t} TO {sp}" for t in APP_WRITE]
    # ■ Parar closes the night itself (the producer may be killed before its finally: block)
    out.append(f"GRANT UPDATE (status, ended_at) ON navidad_live.sim_run TO {sp}")
    out.append(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA navidad_live TO {sp}")
    return out


def _create_sp_secret(client: Any, sp_id: int, attempts: int, wait_s: float, log: Callable[[str], None],
                      sleep: Callable[[float], None]) -> str:
    """A service principal created seconds ago is not yet known to the OAuth secrets service: the first
    calls fail with PermissionDenied / NotFound for a minute or two (seen on a fresh workspace). Retry."""
    for i in range(1, attempts + 1):
        try:
            return client.service_principal_secrets_proxy.create(service_principal_id=sp_id).secret
        except Exception as exc:
            transient = type(exc).__name__ in ("PermissionDenied", "NotFound") or "not authorized" in str(exc)
            if not transient or i == attempts:
                raise RuntimeError(
                    f"No se pudo crear el secreto OAuth del service principal de Zerobus ({exc}). Hace falta ser "
                    "admin del workspace; si acabas de crearlo, espera un par de minutos y vuelve a ejecutar.") from exc
            log(f"Zerobus identity: service principal not ready yet ({type(exc).__name__}), retry {i}/{attempts - 1} in {wait_s:.0f} s")
            sleep(wait_s)
    raise AssertionError("unreachable")


def ensure_ingest_identity(client: Any, scope: str = SECRET_SCOPE, name: str = INGEST_SP,
                           log: Callable[[str], None] = print, attempts: int = 10, wait_s: float = 20,
                           sleep: Callable[[float], None] = time.sleep) -> str:
    """Service principal + OAuth secret stored as <scope>/client_id and <scope>/client_secret.
    Returns the application id. Reuses an existing secret scope entry if both keys exist."""
    existing = [s for s in client.service_principals.list(filter=f'displayName eq "{name}"')]
    sp = existing[0] if existing else client.service_principals.create(display_name=name)
    if not any(s.name == scope for s in client.secrets.list_scopes()):
        client.secrets.create_scope(scope=scope)
    keys = {k.key for k in client.secrets.list_secrets(scope=scope)}
    if {"client_id", "client_secret"} <= keys:
        log(f"Zerobus identity: {name} ({sp.application_id}) · secrets already in scope {scope}")
        return sp.application_id
    secret = _create_sp_secret(client, int(sp.id), attempts, wait_s, log, sleep)
    client.secrets.put_secret(scope=scope, key="client_id", string_value=sp.application_id)
    client.secrets.put_secret(scope=scope, key="client_secret", string_value=secret)  # never printed
    log(f"Zerobus identity: {name} ({sp.application_id}) · new OAuth secret stored in scope {scope}")
    return sp.application_id


def remove_ingest_identity(client: Any, scope: str = SECRET_SCOPE, name: str = INGEST_SP,
                           log: Callable[[str], None] = print) -> None:
    """Delete the Zerobus service principal (and its OAuth secrets) and the secret scope. Shared by every
    workbench in the workspace; 04_tiempo_real_stream recreates them on its next run."""
    for sp in client.service_principals.list(filter=f'displayName eq "{name}"'):
        client.service_principals.delete(id=sp.id)
        log(f"service principal {name} ({sp.application_id}) deleted")
    if any(s.name == scope for s in client.secrets.list_scopes()):
        client.secrets.delete_scope(scope=scope)
        log(f"secret scope {scope} deleted")


LIVE_JOB_PREFIX = "navidad-live-"
JOB_PARAMS = {  # name: default — the app's ▶ button overrides campaign, minutes, stream_minutes, destino
    "catalog": "", "suffix": "", "campaign": "nochebuena", "year": "", "minutes": "15", "stream_minutes": "21",
    "helpers": "4", "exception_rate": "0.015", "destino": "zerobus", "incident": "ninguna",
}


def live_job_name(project_id: str) -> str:
    """navidad-<slug> → navidad-live-<slug> (the app derives the same name from NAVIDAD_PROJECT_ID)."""
    return LIVE_JOB_PREFIX + project_id.removeprefix("navidad-")


def live_job_settings(*, name: str, notebook_dir: str, catalog: str, suffix: str) -> dict[str, Any]:
    """Serverless job: stream (04) and producer (05) side by side; the producer waits for the stream."""
    from databricks.sdk.service import jobs

    ref = lambda k: f"{{{{job.parameters.{k}}}}}"  # noqa: E731
    # Job parameters are pushed down to every task by key and override base_parameters, so each task
    # parameter must be named like the job parameter it reads (04's widget is stream_minutes, not minutes).
    stream = {k: ref(k) for k in ("catalog", "suffix", "stream_minutes")}
    producer = {k: ref(k) for k in ("catalog", "suffix", "campaign", "year", "minutes", "helpers", "exception_rate", "destino",
                                    "incident")}
    defaults = {**JOB_PARAMS, "catalog": catalog, "suffix": suffix}
    return dict(
        name=name,
        max_concurrent_runs=1,
        timeout_seconds=2 * 3600,
        parameters=[jobs.JobParameterDefinition(name=k, default=v) for k, v in defaults.items()],
        tasks=[
            # one quick retry: serverless notebooks occasionally abort (SIGABRT) while loading native
            # libraries right after %pip; a second attempt starts clean. Both notebooks are safe to rerun.
            jobs.Task(task_key="stream", max_retries=1, min_retry_interval_millis=5_000, notebook_task=jobs.NotebookTask(
                notebook_path=f"{notebook_dir}/04_tiempo_real_stream", base_parameters=stream)),
            jobs.Task(task_key="producer", max_retries=1, min_retry_interval_millis=5_000, notebook_task=jobs.NotebookTask(
                notebook_path=f"{notebook_dir}/05_tiempo_real_productor", base_parameters=producer)),
        ],
        tags={"project": "navidad-live"},
    )


def ensure_live_job(client: Any, *, project_id: str, notebook_dir: str, catalog: str, suffix: str = "",
                    app_sp: str | None = None, log: Callable[[str], None] = print) -> int:
    """Create or update the control-center job and let the app's service principal run and cancel it
    (CAN_MANAGE_RUN). Runs execute as the job owner, so the notebooks keep the owner's identity."""
    from databricks.sdk.service import jobs

    name = live_job_name(project_id)
    settings = live_job_settings(name=name, notebook_dir=notebook_dir.removeprefix("/Workspace"), catalog=catalog,
                                 suffix=suffix)
    existing = [j for j in client.jobs.list(name=name)]
    if existing:
        job_id = existing[0].job_id
        client.jobs.reset(job_id=job_id, new_settings=jobs.JobSettings(**settings))
        log(f"job {name} ({job_id}) updated")
    else:
        job_id = client.jobs.create(**settings).job_id
        log(f"job {name} ({job_id}) created")
    if app_sp:
        client.jobs.update_permissions(job_id=str(job_id), access_control_list=[jobs.JobAccessControlRequest(
            service_principal_name=app_sp, permission_level=jobs.JobPermissionLevel.CAN_MANAGE_RUN)])
        log(f"app service principal {app_sp} can run and stop {name}")
    return job_id


REPLICA_ID = "replica"


def ensure_read_replica(client: Any, *, project_id: str, endpoint_id: str = REPLICA_ID, min_cu: float = 0.5,
                        max_cu: float = 4.0, log: Callable[[str], None] = print) -> Any:
    """Read-only compute endpoint on the production branch: same storage as the primary (no data copy),
    its own autoscaling. Idempotent; returns the endpoint (with its host)."""
    from databricks.sdk.service.postgres import Endpoint, EndpointSpec, EndpointType

    branch = f"projects/{project_id}/branches/production"
    name = f"{branch}/endpoints/{endpoint_id}"
    existing = [e for e in client.postgres.list_endpoints(parent=branch) if e.name == name]
    if not existing:
        t0 = time.time()

        def create(lo: float, hi: float) -> None:
            client.postgres.create_endpoint(parent=branch, endpoint_id=endpoint_id, endpoint=Endpoint(spec=EndpointSpec(
                endpoint_type=EndpointType.ENDPOINT_TYPE_READ_ONLY, autoscaling_limit_min_cu=lo,
                autoscaling_limit_max_cu=hi))).wait()

        try:
            create(min_cu, max_cu)
        except Exception as exc:
            # Free Edition: «Requested compute size (4.0 CU) exceeds the maximum allowed (1.0 CU) for this workspace
            # tier» → retry at the tier's cap
            cap = re.search(r"maximum allowed \(([\d.]+) CU\)", str(exc))
            if not cap:
                raise
            hi = float(cap.group(1))
            log(f"read replica capped at {hi:g} CU by the workspace tier")
            create(min(min_cu, hi), hi)
        log(f"read replica {endpoint_id} created in {time.time() - t0:.0f} s")
    else:
        log(f"read replica {endpoint_id} already exists")
    return client.postgres.get_endpoint(name=name)


def measure_replica_lag(write_conn: Any, read_conn: Any, probes: int = 20, timeout_s: float = 5.0) -> dict[str, Any]:
    """Insert a row on the primary and time until the replica sees it (autocommit connections)."""
    lags = []
    base = int(time.time() * 1000) * 1000
    for i in range(probes):
        pid = base + i
        t0 = time.perf_counter()
        with write_conn.cursor() as cur:
            cur.execute("INSERT INTO navidad_live.replica_probe (probe_id) VALUES (%s)", (pid,))
        t_commit = time.perf_counter()
        while time.perf_counter() - t_commit < timeout_s:
            with read_conn.cursor() as cur:
                cur.execute("SELECT 1 FROM navidad_live.replica_probe WHERE probe_id = %s", (pid,))
                if cur.fetchone():
                    break
        lags.append((time.perf_counter() - t_commit) * 1000)
        time.sleep(0.2)
    with write_conn.cursor() as cur:
        cur.execute("DELETE FROM navidad_live.replica_probe WHERE probe_id >= %s", (base,))
    lags.sort()
    pct = lambda q: round(lags[min(len(lags) - 1, int(q * len(lags)))], 1)  # noqa: E731
    return {"probes": probes, "p50_ms": pct(0.5), "p95_ms": pct(0.95), "max_ms": round(lags[-1], 1),
            "note": "includes one replica round trip per poll"}


def provision_live(*, conn: Any, schema: str, app_sp: str | None = None,
                   run_uc_sql: Callable[[str], Any] | None = None, catalog: str | None = None,
                   ingest_application_id: str | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    """conn: psycopg2 connection to the production branch (autocommit) as the workbench owner."""
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(live_sql(schema))
        log("navidad_live schema ready")
        if app_sp:
            for stmt in app_grants(app_sp):
                cur.execute(stmt)
            log(f"grants for the app service principal {app_sp} done")
    if run_uc_sql and catalog:
        for stmt in uc_statements(catalog, schema):
            run_uc_sql(stmt)
        log(f"bronze {catalog}.{schema}.{BRONZE_TABLE} + volume {CHECKPOINT_VOLUME} ready")
        if ingest_application_id:
            for stmt in zerobus_grants(catalog, schema, ingest_application_id):
                run_uc_sql(stmt)
            log("Zerobus grants (MODIFY + SELECT on bronze) done")
    return {"seconds": round(time.time() - t0, 1)}
