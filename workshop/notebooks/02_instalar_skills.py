# Databricks notebook source
# MAGIC %md
# MAGIC # 02 · Instalar las skills para Genie Code (sin CLI)
# MAGIC
# MAGIC Copia `navidad-hack-infra` y `navidad-hack-build-assistant` a tu carpeta de skills de Genie Code
# MAGIC (`/Workspace/Users/<tú>/.assistant/skills/`). Después, en Genie Code: *«Ayúdame a construir sobre el Taller de Navidad»*.

# COMMAND ----------

# MAGIC %pip install "databricks-sdk==0.122.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import io
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.workspace import ImportFormat

nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
repo = Path("/Workspace" + nb_path).parents[2]
me = dbutils.notebook.entry_point.getDbutils().notebook().getContext().userName().get()
w = WorkspaceClient()
dest = f"/Users/{me}/.assistant/skills"
SKIP = {"tests", "tools", "__pycache__", ".pytest_cache", "copied-apps"}

# Everything goes through the Workspace API (server-side), never through the /Workspace file view: on a
# re-install that view can still show the folder that was just deleted (shutil.rmtree → «Directory not empty»).
errors = []
for skill in ("navidad-hack-infra", "navidad-hack-build-assistant"):
    target = f"{dest}/{skill}"
    try:
        try:
            w.workspace.delete(target, recursive=True)
        except NotFound:
            pass
        src = repo / skill
        files = [f for f in src.rglob("*") if f.is_file() and not SKIP & set(f.relative_to(src).parts)]
        for d in sorted({f.relative_to(src).parent for f in files}):
            w.workspace.mkdirs(f"{target}/{d}".rstrip("/."))
        for f in files:
            w.workspace.upload(f"{target}/{f.relative_to(src)}", io.BytesIO(f.read_bytes()), format=ImportFormat.AUTO,
                               overwrite=True)
        print(f"instalada: /Workspace{target} ({len(files)} ficheros)")
    except Exception as exc:  # keep going with the other skill, report at the end
        errors.append(f"{skill}: {exc}")
assert not errors, "No se pudo instalar: " + " · ".join(errors)
