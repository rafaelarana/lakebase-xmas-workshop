# Databricks notebook source
# MAGIC %md
# MAGIC # 00 · Taller de Navidad desde cero
# MAGIC
# MAGIC Este notebook construye, solo con un workspace, una aplicación completa sobre **Lakebase**:
# MAGIC
# MAGIC | Paso | Qué crea | Dónde |
# MAGIC |---|---|---|
# MAGIC | 1 | Datos sintéticos de España (hogares, personas, preferencias, rutas de Papá Noel y los Reyes) | Unity Catalog (Delta) |
# MAGIC | 2 | Proyecto Lakebase Autoscaling + 4 *synced tables* (Delta → Postgres) | Lakebase |
# MAGIC | 3 | App de Databricks (FastAPI + React) conectada a Postgres y al AI Gateway, con la página **Incidencias** | Databricks Apps |
# MAGIC | 4 | Red de carreteras (OpenStreetMap) de Madrid, Valencia y Barcelona + escenarios Filomena, DANA y cortes de calle | Lakebase (PostGIS + pgRouting) |
# MAGIC | 5 | Centro de control: esquema `navidad_live` + job `navidad-live-<slug>` de los botones ▶ / ■ | Lakebase + Jobs |
# MAGIC
# MAGIC **Requisitos:** un workspace con Lakebase y Databricks Apps, cómputo serverless y permiso para crear un esquema en un catálogo.
# MAGIC
# MAGIC **Uso:** rellena `catalog` arriba y pulsa **Run all** (~5 min a escala 0,1). Al acabar, la última celda imprime el enlace al app.
# MAGIC
# MAGIC Limpieza al terminar: `99_limpieza`.

# COMMAND ----------

# MAGIC %pip install psycopg2-binary "databricks-sdk==0.122.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("catalog", "", "Catálogo (obligatorio)")
dbutils.widgets.text("suffix", "", "Sufijo de nombres (opcional, p. ej. 2)")
dbutils.widgets.dropdown("scale", "0.1", ["0.05", "0.1", "0.25", "0.5", "0.75", "1.0"], "Escala de datos")
dbutils.widgets.dropdown("sync_mode", "SNAPSHOT", ["SNAPSHOT", "TRIGGERED", "CONTINUOUS"], "Modo de synced tables")
dbutils.widgets.dropdown("incidents", "sí", ["sí", "no"], "Pista de incidencias")
dbutils.widgets.dropdown("edicion", "estándar", ["estándar", "Free Edition"], "Edición del workspace")
dbutils.widgets.text("model", "system.ai.claude-opus-5", "Modelo (AI Gateway o serving endpoint)")

# COMMAND ----------

import re
import sys
import tempfile
import time
from pathlib import Path

from databricks.sdk import WorkspaceClient

nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
REPO = Path("/Workspace" + nb_path).parents[2]
INFRA = REPO / "navidad-hack-infra"
sys.path.insert(0, str(INFRA / "scripts"))

catalog = dbutils.widgets.get("catalog").strip()
assert catalog, "Rellena el widget «catalog» (un catálogo existente donde puedas crear un esquema)."
suffix = re.sub(r"[^a-z0-9]", "", dbutils.widgets.get("suffix").lower())
scale = float(dbutils.widgets.get("scale"))
sync_mode = dbutils.widgets.get("sync_mode")
with_incidents = dbutils.widgets.get("incidents") == "sí"
model = dbutils.widgets.get("model").strip()
free_edition = dbutils.widgets.get("edicion") == "Free Edition"
if free_edition:
    # Free Edition: no system.ai models in the AI Gateway, 1 Lakebase project at 1 CU, 300 s auto-suspend minimum
    if model.startswith("system.ai."):
        print(f"Free Edition: {model} no está en el AI Gateway → uso el serving endpoint databricks-gpt-oss-120b")
        model = "databricks-gpt-oss-120b"
    if scale > 0.1:
        print(f"⚠ Free Edition: escala {scale} puede agotar la cuota diaria de cómputo; se recomienda 0.1")

from plan import slugify

w = WorkspaceClient()
me = w.current_user.me().user_name
slug = slugify(me) + suffix
schema, prefix = f"navidad_{slug}", f"navidad-{slug}"
project_id, app_name = prefix, f"{prefix}-app"
timings = {}
print(f"Repo: {REPO}\nCatálogo: {catalog} · esquema: {schema}\nLakebase: {project_id} · app: {app_name}\nEscala {scale} · synced {sync_mode} · incidencias {with_incidents} · edición {dbutils.widgets.get('edicion')} · modelo {model}")

# COMMAND ----------

# MAGIC %md ## 1 · Datos sintéticos → Unity Catalog

# COMMAND ----------

from generate_data import GenerateConfig
from write_uc import write_uc

t0 = time.time()
write_uc(spark=spark, catalog=catalog, schema=schema, cfg=GenerateConfig(scale=scale, seed=20260106))
timings["1_uc"] = round(time.time() - t0)
display(spark.sql(f"SELECT city, count(*) AS hogares FROM {catalog}.{schema}.households GROUP BY city ORDER BY hogares DESC LIMIT 8"))

# COMMAND ----------

# MAGIC %md ## 2 · Lakebase Autoscaling + synced tables
# MAGIC Un proyecto Postgres serverless (rama `production`, cómputo 0,5–8 CU con scale-to-zero) y cuatro *synced tables* gestionadas desde Unity Catalog.

# COMMAND ----------

from provision_lakebase import provision_lakebase

t0 = time.time()
lb = provision_lakebase(catalog=catalog, schema=schema, project_id=project_id, client=w, sync_mode=sync_mode,
                        free_edition=free_edition)
timings["2_lakebase"] = round(time.time() - t0)
print(lb)

# COMMAND ----------

# MAGIC %md ## 3 · App de Databricks (con la página Incidencias)

# COMMAND ----------

from deploy_app import deploy_app

source_template = INFRA / "templates" / "app"
if with_incidents:  # home page = «Mapa de rutas», plus /incidencias and /plumbing
    from provision_incidents import app_template_for_workshop
    source_template = app_template_for_workshop(Path(tempfile.mkdtemp()) / "template")

t0 = time.time()
app = deploy_app(prefix=prefix, catalog=catalog, schema=schema, project_id=project_id, model=model,
                 source_template=source_template, dest_dir=Path(tempfile.mkdtemp()) / prefix,
                 client=w, synced_ready=True)
timings["3_app"] = round(time.time() - t0)
app_url = app.get("url") or w.apps.get(name=app_name).url
print(app_url)

# COMMAND ----------

# MAGIC %md ## 4 · Pista de incidencias: red de carreteras + escenarios (PostGIS + pgRouting)
# MAGIC Grafo precargado de Madrid, Valencia y Barcelona (© OpenStreetMap contributors, ODbL; sin red saliente). Escenarios **inspirados en** hechos reales; geometría ilustrativa.

# COMMAND ----------

if with_incidents:
    from provision_incidents import provision_incidents

    t0 = time.time()
    report = provision_incidents(project_id=project_id, schema=schema, client=w, app_name=app_name,
                                 cities=("Madrid", "Valencia", "Barcelona"))
    timings["4_incidencias"] = round(time.time() - t0)
    print(report)

# COMMAND ----------

# MAGIC %md ## 5 · Centro de control: tablas `navidad_live` y job ▶ / ■
# MAGIC Deja `/control` listo («No hay ejecuciones») y crea el job `navidad-live-<slug>` (04 + 05) que lanzan los botones ▶ / ■ del app.
# MAGIC También crea la identidad de Zerobus (service principal + secreto OAuth, requiere admin del workspace) y la tabla bronze; `04` lo vuelve a comprobar al arrancar.

# COMMAND ----------

if with_incidents:  # the control center ships with the workshop app template
    from pg import Pg
    from provision_live import ensure_ingest_identity, ensure_live_job, provision_live

    t0 = time.time()
    app_sp = w.apps.get(name=app_name).service_principal_client_id
    # Zerobus identity (service principal + OAuth secret) now, so the ▶ job never races for it.
    # Not fatal: without it «Directo a Lakebase» still works and 04 retries.
    try:
        ingest_app_id = ensure_ingest_identity(w)
    except Exception as exc:
        ingest_app_id = None
        print("⚠ identidad de Zerobus no creada:", str(exc)[:300])
    db = Pg(w, me, f"projects/{project_id}/branches/production")
    try:
        provision_live(conn=db.conn, schema=schema, app_sp=app_sp, run_uc_sql=spark.sql, catalog=catalog,
                       ingest_application_id=ingest_app_id)
    finally:
        db.close()  # do not keep the compute awake (Free Edition: 1 CU)
    ensure_live_job(w, project_id=project_id, notebook_dir=str(Path("/Workspace" + nb_path).parent), catalog=catalog,
                    suffix=suffix, app_sp=app_sp)
    timings["5_control"] = round(time.time() - t0)

# COMMAND ----------

# MAGIC %md ## Listo

# COMMAND ----------

total = sum(timings.values())
print("Tiempos (s):", timings, "· total", total)
extra = (f'<p>Planificador: <a href="{app_url}/planner" target="_blank">{app_url}/planner</a> · '
         f'Centro de control: <a href="{app_url}/control" target="_blank">{app_url}/control</a></p>') if with_incidents else \
        "<p>Pista de incidencias y centro de control no instalados (widget <code>incidents</code> = no).</p>"
displayHTML(f"""
<h3>Taller de Navidad listo en {total // 60} min {total % 60} s</h3>
<p>Mapa de rutas: <a href="{app_url}" target="_blank">{app_url}</a></p>
{extra}
<p>Lakebase: proyecto <code>{project_id}</code> · esquema <code>{schema}</code></p>
""")
