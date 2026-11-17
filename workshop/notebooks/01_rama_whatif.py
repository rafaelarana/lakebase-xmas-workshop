# Databricks notebook source
# MAGIC %md
# MAGIC # 01 · Branching: un what-if en una rama de Lakebase
# MAGIC
# MAGIC Lo mismo que hace el botón **Simular en rama** del app, paso a paso y sin app:
# MAGIC
# MAGIC 1. Crear una rama hija de `production` (copy-on-write: instantánea, con su propio cómputo y TTL).
# MAGIC 2. Conectarse a la rama y activar una incidencia (DANA en Valencia o cortes en Barcelona).
# MAGIC 3. pgRouting: tiempos con y sin incidencia, sugerencias (desvíos, puntos de recogida, refuerzos, TSP).
# MAGIC 4. Comprobar que **producción no ha cambiado**.
# MAGIC 5. Borrar la rama.
# MAGIC
# MAGIC Requiere haber ejecutado `00_workbench` con la pista de incidencias.

# COMMAND ----------

# MAGIC %pip install psycopg2-binary "databricks-sdk==0.122.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("suffix", "", "Sufijo usado en 00 (si lo pusiste)")
dbutils.widgets.dropdown("scenario", "dana_valencia", ["dana_valencia", "cortes_barcelona", "filomena_madrid"], "Escenario")
dbutils.widgets.dropdown("keep_branch", "no", ["no", "sí"], "¿Conservar la rama al final?")

# COMMAND ----------

import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.postgres import Branch, BranchSpec
from google.protobuf.duration_pb2 import Duration

nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
sys.path.insert(0, str(Path("/Workspace" + nb_path).parents[2] / "navidad-hack-infra" / "scripts"))
from pg import Pg
from plan import slugify

w = WorkspaceClient()
me = w.current_user.me().user_name
slug = slugify(me) + re.sub(r"[^a-z0-9]", "", dbutils.widgets.get("suffix").lower())
schema, project = f"navidad_{slug}", f"projects/navidad-{slug}"
scenario = dbutils.widgets.get("scenario")
branch_id = f"sim-nb-{dt.datetime.utcnow():%m%d-%H%M%S}"


def connect(branch: str) -> Pg:
    """Reconnects by itself if the compute auto-suspends between cells (60 s idle)."""
    pg = Pg(w, me, f"{project}/branches/{branch}")
    pg.conn  # open now (and wait for the compute) so timings and errors show up here
    return pg

# COMMAND ----------

# MAGIC %md ## 1 · Crear la rama

# COMMAND ----------

t0 = time.time()
w.postgres.create_branch(
    parent=project, branch_id=branch_id,
    branch=Branch(spec=BranchSpec(source_branch=f"{project}/branches/production", ttl=Duration(seconds=4 * 3600))),
).wait()
print(f"Rama {branch_id} creada en {time.time() - t0:.1f} s (TTL 4 h)")
print([b.name.rsplit('/', 1)[-1] for b in w.postgres.list_branches(parent=project)])

# COMMAND ----------

# MAGIC %md ## 2–3 · Incidencia + pgRouting en la rama

# COMMAND ----------

t0 = time.time()
sim = connect(branch_id)
print(f"Conectado a la rama en {time.time() - t0:.1f} s")
sim.q("SELECT navidad_geo.set_minute(%s, 10000)", (scenario,))
kpi = sim.q("SELECT navidad_geo.simulate(%s, %s)", (schema, scenario))[0][0]
sug = sim.q("SELECT navidad_geo.suggest(%s, %s)", (schema, scenario))[0][0]
print(json.dumps({k: v for k, v in kpi.items() if k != "routes"}, indent=2, ensure_ascii=False))
print(json.dumps(sug, indent=2, ensure_ascii=False))

# COMMAND ----------

# MAGIC %md ## 4 · Producción no ha cambiado

# COMMAND ----------

prod = connect("production")
for label, conn in (("production", prod), (branch_id, sim)):
    zones, modified, legs = conn.q("""SELECT (SELECT count(*) FROM navidad_geo.zone WHERE active),
                                              (SELECT count(*) FROM navidad_geo.ways WHERE cost <> cost_base),
                                              (SELECT count(*) FROM navidad_geo.sim_leg)""")[0]
    print(f"{label:>24}: zonas activas {zones:>3} · tramos de carretera modificados {modified:>6} · tramos simulados {legs:>6}")

# COMMAND ----------

# MAGIC %md ## 5 · Borrar la rama

# COMMAND ----------

sim.close(); prod.close()
if dbutils.widgets.get("keep_branch") == "no":
    t0 = time.time()
    w.postgres.delete_branch(name=f"{project}/branches/{branch_id}").wait()
    print(f"Rama {branch_id} borrada en {time.time() - t0:.1f} s")
else:
    print(f"Rama {branch_id} conservada (caduca en 4 h)")
