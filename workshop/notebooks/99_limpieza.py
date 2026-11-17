# Databricks notebook source
# MAGIC %md
# MAGIC # 99 · Limpieza
# MAGIC
# MAGIC Borra **todo** lo que creó `00_workbench`: el app, el proyecto Lakebase (con todas sus ramas, **purgado**: no se puede recuperar) y el esquema de Unity Catalog (incluida la tabla bronze y los checkpoints del centro de control).
# MAGIC Opcional: la identidad de Zerobus (service principal + scope `navidad-live`), compartida por todo el workspace.
# MAGIC Escribe `BORRAR` en el widget `confirm` para ejecutarlo.

# COMMAND ----------

# MAGIC %pip install "databricks-sdk==0.122.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("catalog", "", "Catálogo")
dbutils.widgets.text("suffix", "", "Sufijo usado en 00")
dbutils.widgets.text("confirm", "", "Escribe BORRAR para confirmar")
dbutils.widgets.dropdown("zerobus_identity", "no", ["no", "sí"], "¿Borrar también la identidad de Zerobus (compartida)?")

# COMMAND ----------

import re
import sys
from pathlib import Path

from databricks.sdk import WorkspaceClient

nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
sys.path.insert(0, str(Path("/Workspace" + nb_path).parents[2] / "navidad-hack-infra" / "scripts"))
from plan import slugify, workspace_source_path
from provision_live import live_job_name, remove_ingest_identity

w = WorkspaceClient()
slug = slugify(w.current_user.me().user_name) + re.sub(r"[^a-z0-9]", "", dbutils.widgets.get("suffix").lower())
catalog = dbutils.widgets.get("catalog").strip()
schema, project_id, app_name = f"navidad_{slug}", f"navidad-{slug}", f"navidad-{slug}-app"
print(f"Se borrará: job {live_job_name(project_id)} · app {app_name} · proyecto Lakebase {project_id} · esquema {catalog}.{schema}")
assert dbutils.widgets.get("confirm") == "BORRAR", "Escribe BORRAR en el widget «confirm» para continuar."

# COMMAND ----------

def attempt(label, fn):
    try:
        fn()
        print("✓", label)
    except Exception as exc:
        print("·", label, "→", str(exc)[:160])

def delete_live_job():
    # job navidad-live-<slug> (04 + 05, ▶/■ in /control): cancel its runs, then delete it
    for job in w.jobs.list(name=live_job_name(project_id)):
        w.jobs.cancel_all_runs(job_id=job.job_id)
        w.jobs.delete(job_id=job.job_id)

def delete_synced(t):
    fqn = f"{catalog}.{schema}.{t}_sync"
    name = f"synced_tables/{fqn}"
    pipeline_id = None
    try:
        pipeline_id = w.postgres.get_synced_table(name=name).status.pipeline_id
    except Exception:
        pass  # already gone, or the project was purged first
    try:
        w.postgres.delete_synced_table(name=name)
        return
    except Exception as exc:
        error = exc
    # The delete failed: remove the orphaned sync pipeline. Its name is «Synced table: <fqn> <suffix>»; match the
    # prefix exactly in Python (in LIKE, «_» would be a wildcard).
    prefix = f"Synced table: {fqn}"
    try:
        ids = {pipeline_id} if pipeline_id else set()
        ids |= {p.pipeline_id for p in w.pipelines.list_pipelines(filter=f"name LIKE '{prefix}%'")
                if p.name == prefix or (p.name or "").startswith(prefix + " ")}
        for pid in ids:
            w.pipelines.delete(pipeline_id=pid)
        if ids:
            print(f"  · pipeline huérfano de {t}_sync borrado ({', '.join(ids)})")
    except Exception as cleanup_exc:
        print(f"  · no se pudo limpiar el pipeline de {t}_sync: {str(cleanup_exc)[:120]}")
    if "dependent objects exist" in str(error):
        # workshop views (navidad_live / navidad_search) read this table: it goes away with the project purge below
        raise RuntimeError("tiene vistas del taller que dependen de ella; se borra al purgar el proyecto (siguiente paso)") from error
    raise error

attempt(f"job {live_job_name(project_id)}", delete_live_job)
attempt(f"app {app_name}", lambda: w.apps.delete(name=app_name))
# the app's generated source code (uploaded by 00 for the deployment)
app_src = workspace_source_path(w.current_user.me().user_name, project_id)
attempt(f"código del app {app_src}", lambda: w.workspace.delete(app_src, recursive=True))
for table in ("households", "people", "preferences", "journeys"):
    attempt(f"synced table {table}_sync", lambda t=table: delete_synced(t))
# purge=True: no 7-day soft-delete, so 00_workbench can recreate the project with the same name
attempt(f"proyecto Lakebase {project_id} (purga)", lambda: w.postgres.delete_project(name=f"projects/{project_id}", purge=True).wait())
if catalog:
    # includes the control center's bronze table and checkpoint volume
    attempt(f"esquema {catalog}.{schema}", lambda: spark.sql(f"DROP SCHEMA IF EXISTS `{catalog}`.`{schema}` CASCADE"))
# Centro de control: the Zerobus service principal + secret scope `navidad-live` are shared by every workbench in
# this workspace (04_tiempo_real_stream recreates them). Only delete them when nobody else is using them.
if dbutils.widgets.get("zerobus_identity") == "sí":
    attempt("identidad de Zerobus (navidad-live-ingest + scope navidad-live)", lambda: remove_ingest_identity(w))
else:
    print("· identidad de Zerobus conservada (widget zerobus_identity = no)")
