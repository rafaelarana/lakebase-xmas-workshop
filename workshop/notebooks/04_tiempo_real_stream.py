# Databricks notebook source
# MAGIC %md
# MAGIC # 04 · Tiempo real (1/2): Delta → Spark Structured Streaming → **Lakebase sink**
# MAGIC
# MAGIC ```
# MAGIC 05_productor ──Zerobus──► Delta bronze ──Spark (este notebook)──► Lakebase navidad_live ──► app /control
# MAGIC ```
# MAGIC
# MAGIC 1. **Preparación** (idempotente): schema `navidad_live` en Lakebase, tabla bronze + volumen de checkpoints en UC, y la identidad de Zerobus (service principal + secreto en el scope `navidad-live`).
# MAGIC 2. **Streaming:** tres consultas que leen bronze y escriben en Lakebase con `format("postgresql")` + `upsertkey` (INSERT … ON CONFLICT DO UPDATE):
# MAGIC    - `delivery_status`: último estado de cada hogar
# MAGIC    - `helper_position`: posición de cada duende / paje
# MAGIC    - `exception_event`: excepciones, sin duplicados (Zerobus es *at-least-once*)
# MAGIC
# MAGIC En **serverless** el sink admite `Trigger.AvailableNow()`, así que este notebook repite ejecuciones cortas en bucle (~3–5 s cada una). En cómputo clásico se usaría `processingTime="1 second"` o `realTime` (con una fuente Kafka).
# MAGIC
# MAGIC Arranca este notebook **antes** de `05_tiempo_real_productor`. Para pararlo: *Interrupt*.
# MAGIC La preparación también crea el job `navidad-live-<slug>` (04 + 05 en paralelo) que lanzan los botones **▶ Iniciar noche / ■ Parar** del app.

# COMMAND ----------

# MAGIC %pip install psycopg2-binary "databricks-sdk==0.122.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("catalog", "", "Catálogo (el de 00)")
dbutils.widgets.text("suffix", "", "Sufijo usado en 00 (si lo pusiste)")
# widget key = the job parameter name: job parameters are pushed down to every task by key and win over
# base_parameters, so a widget called "minutes" would get the producer's 15 instead of stream_minutes.
dbutils.widgets.text("stream_minutes", "45", "Minutos que corre el bucle")

# COMMAND ----------

import json
import re
import sys
import time
from pathlib import Path

from databricks.sdk import WorkspaceClient
from pyspark.sql import functions as F

nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
sys.path.insert(0, str(Path("/Workspace" + nb_path).parents[2] / "navidad-hack-infra" / "scripts"))
from pg import Pg
from plan import slugify
from provision_live import BRONZE_TABLE, CHECKPOINT_VOLUME, ensure_ingest_identity, ensure_live_job, provision_live

w = WorkspaceClient()
me = w.current_user.me().user_name
catalog = dbutils.widgets.get("catalog").strip()
assert catalog, "Pon el catálogo del workbench en el widget «catalog»"
slug = slugify(me) + re.sub(r"[^a-z0-9]", "", dbutils.widgets.get("suffix").lower())
schema, project_id = f"navidad_{slug}", f"navidad-{slug}"
bronze = f"{catalog}.{schema}.{BRONZE_TABLE}"
chk_root = f"/Volumes/{catalog}/{schema}/{CHECKPOINT_VOLUME}"
print(f"bronze {bronze} · Lakebase {project_id}/production · schema navidad_live")

# COMMAND ----------

# MAGIC %md ## 1 · Preparación (idempotente)

# COMMAND ----------

db = Pg(w, me, f"projects/{project_id}/branches/production")
app_sp = w.apps.get(name=f"{project_id}-app").service_principal_client_id
ingest_app_id = ensure_ingest_identity(w)
provision_live(conn=db.conn, schema=schema, app_sp=app_sp, run_uc_sql=spark.sql, catalog=catalog,
               ingest_application_id=ingest_app_id)
# Job behind the app's ▶ / ■ buttons: this notebook + 05 side by side, runnable by the app's service principal
ensure_live_job(w, project_id=project_id, notebook_dir=str(Path("/Workspace" + nb_path).parent), catalog=catalog,
                suffix=dbutils.widgets.get("suffix"), app_sp=app_sp)

# COMMAND ----------

# MAGIC %md ## 2 · Las tres consultas de streaming
# MAGIC Cada una tiene su checkpoint en el volumen. `max_by(struct, event_ts)` se queda con el último evento por clave dentro de cada lote, y el sink hace el upsert por la clave primaria.

# COMMAND ----------

LB = {"endpoint": f"{project_id}.production.primary", "database": "databricks_postgres"}
# Few shuffle partitions: each AvailableNow run loads every state-store partition and each write task
# opens its own Lakebase connection. With the default 200 a run took 50–80 s; with 8, a few seconds.
# The partition count is fixed when a checkpoint is created, hence the versioned checkpoint folder.
SHUFFLE_PARTITIONS = 8
CHK_VERSION = "v2"
spark.conf.set("spark.sql.shuffle.partitions", str(SHUFFLE_PARTITIONS))


def sink(df, table: str, key: str):
    return (df.writeStream.format("postgresql").outputMode("update")
            .option("endpoint", LB["endpoint"]).option("database", LB["database"])
            .option("dbtable", f"navidad_live.{table}").option("upsertkey", key)
            .option("checkpointLocation", f"{chk_root}/{CHK_VERSION}/{table}")
            .queryName(table).trigger(availableNow=True))


def latest(df, keys, cols):
    """Last event per key (by simulated time) — at-least-once duplicates collapse here."""
    return (df.groupBy(*keys).agg(F.max_by(F.struct(*cols), "event_ts").alias("r"))
              .select(*keys, "r.*"))


def events():
    return spark.readStream.table(bronze).withWatermark("sent_ts", "5 minutes")


def q_deliveries():
    df = latest(events().where("event_type IN ('entregado', 'excepcion')").withColumn("status", F.col("event_type")),
                ["sim_run_id", "household_id"],
                ["city", "route_id", "stop_number", "bringer", "helper_id", "helper_kind", "status",
                 "exception_code", "gifts", "lat", "lon", "event_ts", "sent_ts"])
    return sink(df, "delivery_status", "sim_run_id,household_id")


def q_helpers():
    df = latest(events(), ["sim_run_id", "helper_id"],
                ["helper_name", "helper_kind", "bringer", "city", "route_id", "stop_number", "lat", "lon",
                 "event_ts", "sent_ts"])
    return sink(df, "helper_position", "sim_run_id,helper_id")


def q_exceptions():
    df = (events().where("event_type = 'excepcion'").dropDuplicatesWithinWatermark(["event_id"])
            .select("event_id", "sim_run_id", "household_id", "city", "route_id", "stop_number", "bringer",
                    "helper_id", "helper_name", "helper_kind", "exception_code", "lat", "lon", "event_ts", "sent_ts"))
    return sink(df, "exception_event", "event_id")


QUERIES = {"delivery_status": q_deliveries, "helper_position": q_helpers, "exception_event": q_exceptions}

# COMMAND ----------

# MAGIC %md ## 3 · Bucle: ejecuciones `AvailableNow` seguidas (≈ tiempo real en serverless)

# COMMAND ----------

# Each query restarts as soon as its previous AvailableNow run ends, so a slower one never holds the others.
deadline = time.time() + 60 * float(dbutils.widgets.get("stream_minutes"))
runs, started, it, last_log = {}, {}, 0, 0.0


def launch(name):
    started[name] = time.time()
    runs[name] = QUERIES[name]().start()


try:
    for name in QUERIES:
        launch(name)
    while time.time() < deadline:
        for name, q in list(runs.items()):
            if q.isActive:
                continue
            batches = q.recentProgress or []  # an AvailableNow run can have several batches; the last is often empty
            n_rows = sum(b.get("numInputRows") or 0 for b in batches)  # keys can be present with None
            ms = sum((b.get("durationMs") or {}).get("triggerExecution") or 0 for b in batches)
            secs = time.time() - started[name]
            db.q("INSERT INTO navidad_live.stream_progress (started_at, seconds, queries) VALUES (to_timestamp(%s), %s, %s)",
                 (started[name], secs, json.dumps({name: {"rows": n_rows, "ms": ms}})))
            it += 1
            if not n_rows:
                time.sleep(0.5)  # nothing new: don't spin
            launch(name)
        if time.time() - last_log > 15:
            last_log = time.time()
            lat, cyc = db.q("""SELECT round(percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM updated_at - sent_ts))::numeric, 1),
                                      (SELECT round(avg(seconds)::numeric, 1) FROM navidad_live.stream_progress
                                        WHERE started_at > now() - interval '60 seconds' AND queries ? 'delivery_status')
                               FROM navidad_live.delivery_status WHERE updated_at > now() - interval '30 seconds'""")[0]
            print(f"{it:>5} ejecuciones · ciclo delivery_status {cyc or '—'} s · latencia evento→Lakebase p50 (30 s): {lat or '—'} s")
        time.sleep(0.2)
except KeyboardInterrupt:
    print("Parado.")
finally:
    for q in spark.streams.active:
        q.stop()
print(f"{it} ejecuciones. El checkpoint guarda dónde se quedó: al volver a arrancar continúa sin duplicar.")
