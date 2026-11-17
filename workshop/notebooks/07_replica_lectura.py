# Databricks notebook source
# MAGIC %md
# MAGIC # 07 · Bonus: réplica de lectura para el centro de control
# MAGIC
# MAGIC ```
# MAGIC entregas en tiempo real (sink / escritura directa) ─┐
# MAGIC synced tables desde Unity Catalog (batch) ─────────┼─► cómputo PRIMARIO (lectura-escritura) ─┐
# MAGIC agente y ▶/■ (decisiones) ─────────────────────────┘                                        │ mismo almacenamiento
# MAGIC                                                                                             ▼
# MAGIC                                   app /control (KPIs, mapa, excepciones) ◄── RÉPLICA de lectura (READ_ONLY)
# MAGIC ```
# MAGIC
# MAGIC Una réplica de lectura de Lakebase es **otro cómputo sobre el mismo almacenamiento** de la rama: no copia datos, tiene su propio autoscaling y solo admite lecturas. El app la detecta sola (primer endpoint `READ_ONLY` de la rama, o `LAKEBASE_READ_ENDPOINT`) y lee de ella; si no hay réplica o falla, vuelve a la primaria.
# MAGIC
# MAGIC Este notebook: (1) crea la réplica, (2) comprueba que es de solo lectura, (3) mide su retraso desde la región, (4) opcionalmente la borra.

# COMMAND ----------

# MAGIC %pip install psycopg2-binary "databricks-sdk==0.122.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("suffix", "", "Sufijo usado en 00 (si lo pusiste)")
dbutils.widgets.dropdown("accion", "crear", ["crear", "borrar"], "Acción")
dbutils.widgets.text("max_cu", "4", "CU máximas de la réplica")

# COMMAND ----------

import re
import sys
import time
from pathlib import Path

import psycopg2
from databricks.sdk import WorkspaceClient

nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
sys.path.insert(0, str(Path("/Workspace" + nb_path).parents[2] / "navidad-hack-infra" / "scripts"))
from pg import Pg
from plan import slugify
from provision_live import REPLICA_ID, ensure_read_replica, measure_replica_lag

w = WorkspaceClient()
me = w.current_user.me().user_name
slug = slugify(me) + re.sub(r"[^a-z0-9]", "", dbutils.widgets.get("suffix").lower())
project_id = f"navidad-{slug}"
branch = f"projects/{project_id}/branches/production"
replica_name = f"{branch}/endpoints/{REPLICA_ID}"

if dbutils.widgets.get("accion") == "borrar":
    t0 = time.time()
    w.postgres.delete_endpoint(name=replica_name).wait()
    print(f"Réplica borrada en {time.time() - t0:.1f} s. El app vuelve a leer de la primaria en ≤ 1 min.")
    dbutils.notebook.exit("borrada")

# COMMAND ----------

# MAGIC %md ## 1 · Crear la réplica (idempotente)

# COMMAND ----------

t0 = time.time()
ep = ensure_read_replica(w, project_id=project_id, max_cu=float(dbutils.widgets.get("max_cu")))
print(f"{ep.name}\n  tipo {ep.status.endpoint_type.value} · estado {ep.status.current_state.value} · host {ep.status.hosts.host} "
      f"· listo en {time.time() - t0:.1f} s")
for e in w.postgres.list_endpoints(parent=branch):
    print(f"  · {e.name.rsplit('/', 1)[-1]:<10} {e.status.endpoint_type.value}")

# COMMAND ----------

# MAGIC %md ## 2 · Solo lectura, mismos datos

# COMMAND ----------

primary = Pg(w, me, branch)
replica = Pg(w, me, branch)
replica.rw_endpoint = lambda: replica_name   # connect to the replica's host instead of the read-write endpoint
print("¿réplica en modo recuperación (solo lectura)?", replica.q("SELECT pg_is_in_recovery()")[0][0])
try:
    replica.q("INSERT INTO navidad_live.replica_probe (probe_id) VALUES (-1)")
    print("⚠ la réplica aceptó una escritura")
except psycopg2.Error as exc:
    print("✓ escritura rechazada en la réplica:", str(exc).strip().splitlines()[0])
q = "SELECT count(*) FROM navidad_live.delivery_status"
print(f"filas en delivery_status · primaria {primary.q(q)[0][0]:,} · réplica {replica.q(q)[0][0]:,}".replace(",", "."))

# COMMAND ----------

# MAGIC %md ## 3 · Retraso de la réplica (escribir en la primaria → visible en la réplica)

# COMMAND ----------

lag = measure_replica_lag(primary.conn, replica.conn, probes=30)
print(f"retraso p50 {lag['p50_ms']} ms · p95 {lag['p95_ms']} ms · máx {lag['max_ms']} ms ({lag['probes']} sondas)")
print("El app detecta la réplica en ≤ 1 min: la insignia de /control pasa a «Lecturas: réplica».")
dbutils.notebook.exit(str(lag))
