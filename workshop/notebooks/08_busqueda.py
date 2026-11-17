# Databricks notebook source
# MAGIC %md
# MAGIC # 08 · Bonus: búsqueda con Lakebase Search
# MAGIC
# MAGIC Añade una barra de búsqueda a todas las páginas del app: familias (nombre, ciudad, nombres de quienes viven en el hogar) y equipos de duendes / pajes (los mismos nombres que el centro de control).
# MAGIC
# MAGIC | Paso | Qué hace |
# MAGIC |---|---|
# MAGIC | 0 | **Activar Lakebase Search** en el proyecto: Lakebase → proyecto → *Settings* → *Lakebase Search*. Es irreversible y **reinicia los cómputos** del proyecto: hazlo antes de la sesión |
# MAGIC | 1 | Tabla `navidad_search.doc` desde las synced tables (`households_sync`, `people_sync`, `journeys_sync`) |
# MAGIC | 2 | `tsvector` con el analizador `simple` sobre `lower(unaccent(...))`: sin tablas de stop words, así funciona también en la réplica de lectura |
# MAGIC | 3 | Índice **BM25** (`lakebase_bm25`, se construye después de cargar: calcula las estadísticas del corpus) + GIN para prefijos |
# MAGIC | 4 | Pruebas: BM25 y prefijo, con tiempos |
# MAGIC
# MAGIC Sin Lakebase Search el índice se crea igual con búsqueda de texto de Postgres (GIN) y el app lo indica.

# COMMAND ----------

# MAGIC %pip install psycopg2-binary "databricks-sdk==0.122.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("suffix", "", "Sufijo usado en 00 (si lo pusiste)")
dbutils.widgets.text("helpers", "4", "Ayudantes por ruta (como en el centro de control)")

# COMMAND ----------

import re
import sys
import time
from pathlib import Path

from databricks.sdk import WorkspaceClient

nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
sys.path.insert(0, str(Path("/Workspace" + nb_path).parents[2] / "navidad-hack-infra" / "scripts"))
from pg import Pg
from plan import slugify
from provision_search import provision_search

w = WorkspaceClient()
me = w.current_user.me().user_name
slug = slugify(me) + re.sub(r"[^a-z0-9]", "", dbutils.widgets.get("suffix").lower())
schema, project_id = f"navidad_{slug}", f"navidad-{slug}"
db = Pg(w, me, f"projects/{project_id}/branches/production")

# COMMAND ----------

# MAGIC %md ## 0 · ¿Está activado Lakebase Search?

# COMMAND ----------

try:
    db.q("CREATE EXTENSION IF NOT EXISTS lakebase_text CASCADE")
    print("✓ Lakebase Search activado: el índice usará BM25")
except Exception as exc:
    print("⚠ Lakebase Search no está activado en el proyecto:", str(exc).strip().splitlines()[0])
    print("  Actívalo en Lakebase → proyecto → Settings → Lakebase Search (reinicia los cómputos) y vuelve a ejecutar.")
    print("  Mientras tanto, el índice se crea con búsqueda de texto de Postgres (GIN).")

# COMMAND ----------

# MAGIC %md ## 1–3 · Documentos + índices

# COMMAND ----------

app_sp = w.apps.get(name=f"{project_id}-app").service_principal_client_id
report = provision_search(conn=db.conn, schema=schema, app_sp=app_sp, helpers=int(dbutils.widgets.get("helpers")))

# COMMAND ----------

# MAGIC %md ## 4 · Pruebas

# COMMAND ----------

BM25 = """SELECT title, subtitle FROM navidad_search.doc
          ORDER BY search_vector <@> to_bm25query(to_tsvector('simple', navidad_search.norm(%s)), 'navidad_search.doc_bm25') LIMIT 3"""
PREFIX = """SELECT title, subtitle FROM navidad_search.doc
            WHERE search_vector @@ to_tsquery('simple', %s) LIMIT 3"""
for q in ["garcia ortega", "duende pinito", "pajes melchor sevilla", "antia"]:
    t0 = time.time()
    rows = db.q(BM25, (q,)) if report["engine"] == "bm25" else db.q(PREFIX, (" & ".join(t + ":*" for t in q.split()),))
    print(f"{q!r:26} {(time.time() - t0) * 1000:5.0f} ms · {[r[0] for r in rows]}")
print("\nEn el app: pulsa «/» (o la barra de búsqueda) en cualquier página.")
dbutils.notebook.exit(str(report))
