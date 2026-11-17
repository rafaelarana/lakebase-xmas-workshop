# Databricks notebook source
# MAGIC %md
# MAGIC # 06 · Tiempo real sin broker ni Delta (comparativa de latencia)
# MAGIC
# MAGIC El camino del bloque 3 tiene tres saltos antes de Lakebase: **Zerobus** (ack ~0,2 s) → **Delta** (commit cada ~5 s) → **Spark serverless** (`AvailableNow` en bucle). Aquí quitamos los dos primeros:
# MAGIC
# MAGIC ```
# MAGIC Spark «rate» (genera eventos, ~350/s) ─join con el plan de la noche─► Lakebase sink (navidad_live.delivery_status)
# MAGIC ```
# MAGIC
# MAGIC Misma noche, mismo sink y mismo bucle `AvailableNow` en serverless: la diferencia con el bloque 3 es el coste de Zerobus + Delta. La latencia se mide igual: `updated_at` (Lakebase) − `sent_ts` (= marca de tiempo del evento generado).
# MAGIC
# MAGIC Futuro: en cómputo clásico, `processingTime` o **Real-Time Mode** (`trigger(realTime=…)`) quitarían también el coste fijo de cada ejecución `AvailableNow`.

# COMMAND ----------

# MAGIC %pip install psycopg2-binary "databricks-sdk==0.122.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("catalog", "", "Catálogo (el de 00)")
dbutils.widgets.text("suffix", "", "Sufijo usado en 00 (si lo pusiste)")
dbutils.widgets.text("rows_per_second", "350", "Eventos por segundo")
dbutils.widgets.text("minutes", "3", "Minutos de prueba")

# COMMAND ----------

import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

from databricks.sdk import WorkspaceClient
from pyspark.sql import Window
from pyspark.sql import functions as F

nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
sys.path.insert(0, str(Path("/Workspace" + nb_path).parents[2] / "navidad-hack-infra" / "scripts"))
from pg import Pg
from plan import slugify
from provision_live import CHECKPOINT_VOLUME

w = WorkspaceClient()
me = w.current_user.me().user_name
catalog = dbutils.widgets.get("catalog").strip()
assert catalog, "Pon el catálogo del workbench en el widget «catalog»"
slug = slugify(me) + re.sub(r"[^a-z0-9]", "", dbutils.widgets.get("suffix").lower())
schema, project_id = f"navidad_{slug}", f"navidad-{slug}"
rps, minutes = int(dbutils.widgets.get("rows_per_second")), float(dbutils.widgets.get("minutes"))
db = Pg(w, me, f"projects/{project_id}/branches/production")
spark.conf.set("spark.sql.shuffle.partitions", "8")

# COMMAND ----------

# MAGIC %md ## 1 · El plan de la noche (estático) y la ejecución

# COMMAND ----------

year = spark.sql(f"SELECT max(year) FROM {catalog}.{schema}.journeys").first()[0]
plan = (spark.sql(f"""
    SELECT j.route_id, j.stop_number, j.household_id, j.bringer, h.city, h.lat, h.lon, j.arrived_at_utc
    FROM {catalog}.{schema}.journeys j JOIN {catalog}.{schema}.households h USING (household_id)
    WHERE j.year = {year} AND j.campaign = 'nochebuena'""")
        .withColumn("idx", F.row_number().over(Window.orderBy("arrived_at_utc", "route_id", "stop_number")) - 1))
# Serverless has no .cache(): collect the plan once and use it as a local (broadcast) relation in every run
plan = spark.createDataFrame(plan.toPandas())
n_plan = plan.count()
sim_run_id = f"sinbroker-{dt.datetime.now(dt.timezone.utc):%m%d-%H%M%S}"
db.q("UPDATE navidad_live.sim_run SET status = 'cancelada', ended_at = now() WHERE status = 'en_curso'")
db.q("""INSERT INTO navidad_live.sim_run (sim_run_id, year, campaign, mode, speed, night_start, night_end, planned, helpers, exception_rate)
        SELECT %s, %s, 'nochebuena', 'spark_sin_broker', %s, now(), now() + make_interval(mins => %s), %s, 0, 0""",
     (sim_run_id, year, rps * 1.0, int(minutes), n_plan))
for r in plan.groupBy("city").agg(F.avg("lat").alias("lat"), F.avg("lon").alias("lon"), F.count("*").alias("n")).collect():
    db.q("INSERT INTO navidad_live.run_plan VALUES (%s, %s, %s, %s, %s)", (sim_run_id, r.city, r.lat, r.lon, r.n))
print(f"{sim_run_id}: {n_plan:,} paradas · {rps} eventos/s · {minutes:g} min".replace(",", "."))

# COMMAND ----------

# MAGIC %md ## 2 · `rate` → join con el plan → Lakebase sink, en bucle `AvailableNow`

# COMMAND ----------

chk = f"/Volumes/{catalog}/{schema}/{CHECKPOINT_VOLUME}/sinbroker/{sim_run_id}"
plan_b = F.broadcast(plan.select("idx", "household_id", "city", "route_id", "stop_number", "bringer", "lat", "lon",
                                 "arrived_at_utc"))


def query():
    events = (spark.readStream.format("rate").option("rowsPerSecond", rps).load()      # columns: timestamp, value
              .withColumn("idx", F.col("value") % F.lit(n_plan)))
    out = (events.join(plan_b, "idx")
           .select(F.lit(sim_run_id).alias("sim_run_id"), "household_id", "city", "route_id", "stop_number", "bringer",
                   F.concat_ws("-", "route_id", F.lit("1")).alias("helper_id"), F.lit("duende").alias("helper_kind"),
                   F.lit("entregado").alias("status"), F.lit(None).cast("string").alias("exception_code"),
                   F.lit(1).alias("gifts"), "lat", "lon", F.col("arrived_at_utc").alias("event_ts"),
                   F.col("timestamp").alias("sent_ts")))                               # when the event was generated
    return (out.writeStream.format("postgresql").outputMode("update")
            .option("endpoint", f"{project_id}.production.primary").option("database", "databricks_postgres")
            .option("dbtable", "navidad_live.delivery_status").option("upsertkey", "sim_run_id,household_id")
            .option("checkpointLocation", chk).queryName("sin_broker").trigger(availableNow=True))


deadline, runs, t_all = time.time() + 60 * minutes, [], time.time()
try:
    while time.time() < deadline:
        t0 = time.time()
        q = query().start()
        q.awaitTermination()
        rows = sum(b.get("numInputRows") or 0 for b in (q.recentProgress or []))
        runs.append({"s": round(time.time() - t0, 1), "rows": rows})
        db.q("INSERT INTO navidad_live.stream_progress (started_at, seconds, queries) VALUES (to_timestamp(%s), %s, %s)",
             (t0, time.time() - t0, json.dumps({"sin_broker": {"rows": rows}})))
finally:
    for s in spark.streams.active:
        s.stop()
    db.q("UPDATE navidad_live.sim_run SET status = 'terminada', ended_at = now() WHERE sim_run_id = %s", (sim_run_id,))

# COMMAND ----------

# MAGIC %md ## 3 · Resultado

# COMMAND ----------

steady = db.q("""SELECT count(*),
                        round(percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM updated_at - sent_ts))::numeric, 1),
                        round(percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM updated_at - sent_ts))::numeric, 1),
                        round(max(extract(epoch FROM updated_at - sent_ts))::numeric, 1)
                 FROM navidad_live.delivery_status
                 WHERE sim_run_id = %s AND updated_at > (SELECT min(updated_at) + interval '45 seconds'
                                                         FROM navidad_live.delivery_status WHERE sim_run_id = %s)""",
              (sim_run_id, sim_run_id))[0]
cycles = [r["s"] for r in runs[1:]] or [0]
result = {"sim_run_id": sim_run_id, "rows": int(steady[0]), "p50_s": float(steady[1] or 0), "p95_s": float(steady[2] or 0),
          "max_s": float(steady[3] or 0), "runs": len(runs), "cycle_avg_s": round(sum(cycles) / len(cycles), 1),
          "cycle_min_s": min(cycles), "first_run_s": runs[0]["s"] if runs else None}
print(json.dumps(result, indent=1))
dbutils.notebook.exit(json.dumps(result))
