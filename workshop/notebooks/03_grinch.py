# Databricks notebook source
# MAGIC %md
# MAGIC # 03 · El Grinch ataca (opcional): backup, PITR y recuperación en Lakebase
# MAGIC
# MAGIC La noche de Reyes, el Grinch (o un humano con prisa) borra datos operacionales que **solo viven en Lakebase**: las cartas a los Reyes.
# MAGIC
# MAGIC | Paso | Qué hacemos | Herramienta de Lakebase |
# MAGIC |---|---|---|
# MAGIC | 1 | Crear las cartas (datos operacionales escritos por el app) | Postgres |
# MAGIC | 2 | Backup antes de la noche | **Snapshot** manual (y el horario automático) |
# MAGIC | 3 | 😈 El Grinch borra el 80 % de las cartas y cambia el resto a «carbón» | — |
# MAGIC | 4 | Investigar qué ha pasado, sin tocar producción | **Rama desde un momento pasado** (PITR) |
# MAGIC | 5 | Recuperar solo lo dañado, con el app funcionando | Copiar filas de la rama a producción |
# MAGIC | 6 | (Opcional) restauración completa | **Restaurar desde el snapshot** → nueva rama raíz |
# MAGIC | 7 | Prevenir | Mínimo privilegio, horario de snapshots, ventana de restauración |
# MAGIC
# MAGIC Requiere haber ejecutado `00_workbench`. Tarda ~2 min. Las ramas y el snapshot que crea caducan solos (4 h) y se borran al final.

# COMMAND ----------

# MAGIC %pip install psycopg2-binary "databricks-sdk==0.122.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("suffix", "", "Sufijo usado en 00 (si lo pusiste)")
dbutils.widgets.dropdown("full_restore", "no", ["no", "sí"], "¿Probar también la restauración completa (paso 6)?")

# COMMAND ----------

import datetime as dt
import io
import re
import sys
import time
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.postgres import Branch, BranchSpec
from google.protobuf.duration_pb2 import Duration
from google.protobuf.timestamp_pb2 import Timestamp

nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
sys.path.insert(0, str(Path("/Workspace" + nb_path).parents[2] / "navidad-hack-infra" / "scripts"))
from pg import Pg
from plan import slugify

w = WorkspaceClient()
me = w.current_user.me().user_name
slug = slugify(me) + re.sub(r"[^a-z0-9]", "", dbutils.widgets.get("suffix").lower())
schema, project_id = f"navidad_{slug}", f"navidad-{slug}"
project = f"projects/{project_id}"
stamp = f"{dt.datetime.utcnow():%m%d-%H%M%S}"
TTL = 4 * 3600


def connect(branch: str = "production") -> Pg:
    """Reconnects by itself if the compute auto-suspends between cells (60 s idle)."""
    pg = Pg(w, me, f"{project}/branches/{branch}")
    pg.conn  # open now (and wait for the compute) so timings and errors show up here
    return pg


def api(method: str, path: str, **kw):
    """Lakebase REST (snapshots are not in the pinned SDK yet)."""
    return w.api_client.do(method, f"/api/2.0/postgres/{path}", **kw)


def wait_branch(branch_id: str, timeout_s: int = 300):
    for _ in range(timeout_s // 2):
        b = w.postgres.get_branch(name=f"{project}/branches/{branch_id}")
        if b.status and str(b.status.current_state).endswith("READY"):
            return b
        time.sleep(2)
    raise TimeoutError(branch_id)


CARTAS_SQL = f"""
CREATE SCHEMA IF NOT EXISTS navidad_ops;
DROP TABLE IF EXISTS navidad_ops.cartas_reyes;
CREATE TABLE navidad_ops.cartas_reyes (
  carta_id     BIGINT PRIMARY KEY,          -- = person_id del niño
  household_id BIGINT NOT NULL,
  nombre       TEXT NOT NULL,
  edad         INT NOT NULL,
  rey          TEXT NOT NULL,               -- melchor | gaspar | baltasar
  regalo       TEXT NOT NULL,               -- juguetes, libros, …  (o 'carbon')
  estado       TEXT NOT NULL DEFAULT 'recibida',
  escrita_en   TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO navidad_ops.cartas_reyes (carta_id, household_id, nombre, edad, rey, regalo)
SELECT p.person_id, p.household_id, p.given_name, p.age_years, pr.favourite_king,
       CASE WHEN pr.naughty_nice_score < 15 THEN 'carbon' ELSE pr.gift_affinity END
FROM {schema}.people_sync p JOIN {schema}.preferences_sync pr USING (person_id)
WHERE p.role = 'child';
"""
STATS_SQL = """SELECT count(*) AS cartas, count(*) FILTER (WHERE regalo = 'carbon') AS carbon,
                      count(DISTINCT household_id) AS hogares, md5(string_agg(carta_id || regalo, ',' ORDER BY carta_id)) AS huella
               FROM navidad_ops.cartas_reyes"""


def stats(conn, label):
    n, coal, hh, fp = conn.q(STATS_SQL)[0]
    print(f"{label:>34}: {n:>7,} cartas · {coal:>6,} con carbón · {hh:>6,} hogares · huella {fp[:10]}".replace(",", "."))
    return n, coal, fp

# Leftovers from an earlier, interrupted run (keeps the 3-root-branch limit free)
for b in w.postgres.list_branches(parent=project):
    bid = b.name.rsplit("/", 1)[-1]
    if bid.startswith(("grinch-pitr-", "grinch-restore-")):
        w.postgres.delete_branch(name=b.name).wait()
        print("limpiada rama anterior:", bid)
for sn in api("GET", f"{project}/snapshots").get("snapshots", []):
    if sn["name"].rsplit("/", 1)[-1].startswith("antes-del-grinch-"):
        api("DELETE", sn["name"])
        print("limpiado snapshot anterior:", sn["name"].rsplit("/", 1)[-1])

# COMMAND ----------

# MAGIC %md ## 1 · Las cartas a los Reyes (datos operacionales: solo viven en Lakebase)

# COMMAND ----------

prod = connect("production")
prod.q(CARTAS_SQL)
before = stats(prod, "production (antes de la noche)")

# COMMAND ----------

# MAGIC %md ## 2 · Backup antes de la noche: snapshot manual
# MAGIC Instantáneo, inmutable, con caducidad. Además, cada rama raíz puede tener un **horario de snapshots** automáticos (Lakebase → Backup & Restore → Edit schedule).

# COMMAND ----------

snap_id = f"antes-del-grinch-{stamp}"
t0 = time.time()
api("POST", f"{project}/snapshots", query={"snapshot_id": snap_id},
    body={"spec": {"source_branch": f"{project}/branches/production", "ttl": f"{TTL}s"}})
for _ in range(60):
    try:
        snap = api("GET", f"{project}/snapshots/{snap_id}")
        break
    except Exception as exc:
        last = exc
        time.sleep(1)
else:
    raise RuntimeError(f"el snapshot {snap_id} no aparece tras 60 s: {last}")
print(f"Snapshot {snap_id} creado en {time.time() - t0:.1f} s · caduca {snap.get('status', {}).get('expire_time')}")
schedule = api("GET", f"{project}/branches/production/snapshot-schedule")
print("Horario de snapshots automáticos:", schedule.get("schedule") or "NINGUNO ⚠ (configúralo en Backup & Restore)")

# Safe moment = database clock, a few seconds before the Grinch
t_safe = prod.q("SELECT now()")[0][0]
time.sleep(5)
print("Momento seguro (reloj de la base):", t_safe.isoformat())

# COMMAND ----------

# MAGIC %md ## 3 · 😈 El Grinch ataca

# COMMAND ----------

prod.q("DELETE FROM navidad_ops.cartas_reyes WHERE carta_id % 5 <> 0")
prod.q("UPDATE navidad_ops.cartas_reyes SET regalo = 'carbon'")
t_grinch = prod.q("SELECT now()")[0][0]
after = stats(prod, "production (después del Grinch)")
print(f"El Grinch ha borrado {before[0] - after[0]:,} cartas y ha cambiado {after[0]:,} regalos a carbón.".replace(",", "."))

# COMMAND ----------

# MAGIC %md ## 4 · Investigar: una rama de producción tal como estaba en el momento seguro
# MAGIC Copy-on-write desde el historial (ventana de restauración del proyecto). Producción sigue funcionando y no se toca.

# COMMAND ----------

pitr_id = f"grinch-pitr-{stamp}"
ts = Timestamp()
ts.FromDatetime(t_safe.astimezone(dt.timezone.utc).replace(tzinfo=None))
t0 = time.time()
w.postgres.create_branch(
    parent=project, branch_id=pitr_id,
    branch=Branch(spec=BranchSpec(source_branch=f"{project}/branches/production", source_branch_time=ts,
                                  ttl=Duration(seconds=TTL))),
).wait()
print(f"Rama {pitr_id} (estado a {t_safe:%H:%M:%S} UTC) creada en {time.time() - t0:.1f} s")
pitr = connect(pitr_id)
seen = stats(pitr, f"{pitr_id} (momento seguro)")
assert seen[2] == before[2], "la rama no coincide con el estado previo"
print("✓ La rama tiene exactamente las cartas de antes del Grinch")

# COMMAND ----------

# MAGIC %md ## 5 · Recuperar solo lo dañado, con el app funcionando
# MAGIC Copiamos las cartas desde la rama y las fusionamos en producción en **una transacción**: vuelven las borradas y se deshace el carbón. Sin repuntar el app.

# COMMAND ----------

t0 = time.time()
rescate = pitr.copy_out("COPY navidad_ops.cartas_reyes TO STDOUT")


def merge(cur):
    """Idempotent: safe to retry on a fresh connection if this one drops mid-way."""
    cur.execute("CREATE TEMP TABLE cartas_rescate (LIKE navidad_ops.cartas_reyes INCLUDING ALL) ON COMMIT DROP")
    cur.copy_expert("COPY cartas_rescate FROM STDIN", io.StringIO(rescate))
    cur.execute("""INSERT INTO navidad_ops.cartas_reyes SELECT * FROM cartas_rescate
                   ON CONFLICT (carta_id) DO UPDATE SET regalo = EXCLUDED.regalo, estado = EXCLUDED.estado
                   WHERE navidad_ops.cartas_reyes.regalo IS DISTINCT FROM EXCLUDED.regalo""")
    return cur.rowcount


fixed = prod.transaction(merge)
print(f"Recuperadas/corregidas {fixed:,} cartas en {time.time() - t0:.1f} s".replace(",", "."))
restored = stats(prod, "production (recuperada)")
assert restored[2] == before[2]
print("✓ Producción vuelve a estar exactamente como antes del Grinch")

# COMMAND ----------

# MAGIC %md ## 6 · (Opcional) Restauración completa desde el snapshot
# MAGIC Crea una **nueva rama raíz** con los datos del snapshot. Para usarla, el app se repunta a esa rama. Máximo 3 ramas raíz por proyecto: se borra al terminar.

# COMMAND ----------

restore_id = None
if dbutils.widgets.get("full_restore") == "sí":
    restore_id = f"grinch-restore-{stamp}"
    t0 = time.time()
    api("POST", f"{project}/branches", query={"branch_id": restore_id},
        body={"spec": {"source_snapshot": f"{project}/snapshots/{snap_id}", "ttl": f"{TTL}s"}})
    wait_branch(restore_id)
    print(f"Rama raíz {restore_id} restaurada desde el snapshot en {time.time() - t0:.1f} s")
    rc = connect(restore_id)
    stats(rc, f"{restore_id} (snapshot)")
    rc.close()
else:
    print("Saltado (widget full_restore = no)")

# COMMAND ----------

# MAGIC %md ## 7 · Prevenir
# MAGIC - **Mínimo privilegio:** el service principal del app no tiene `DELETE` sobre las cartas (abajo lo comprobamos).
# MAGIC - **Horario de snapshots** en la rama raíz de producción (Backup & Restore → Edit schedule).
# MAGIC - **Ventana de restauración** de 2 a 30 días (por defecto 7) en los ajustes del proyecto.
# MAGIC - **Rama protegida** (`is_protected`) para `production`.
# MAGIC - Los datos que vienen del lakehouse (`*_sync`) se pueden volver a sincronizar desde Unity Catalog.

# COMMAND ----------

app_sp = w.apps.get(name=f"{project_id}-app").service_principal_client_id
can_delete = prod.q("SELECT has_table_privilege(%s, 'navidad_ops.cartas_reyes', 'DELETE')", (app_sp,))[0][0]
info = api("GET", project)
print("¿El SP del app puede borrar cartas?", "sí ⚠" if can_delete else "no ✓")
print("Ventana de restauración del proyecto:", info.get("status", {}).get("history_retention_duration") or info.get("spec", {}).get("history_retention_duration"))

# COMMAND ----------

# MAGIC %md ## Limpieza

# COMMAND ----------

pitr.close(); prod.close()
for b in [pitr_id, restore_id]:
    if b:
        w.postgres.delete_branch(name=f"{project}/branches/{b}").wait()
        print("rama borrada:", b)
api("DELETE", f"{project}/snapshots/{snap_id}")
print("snapshot borrado:", snap_id)
print(f"Grinch: {t_grinch:%H:%M:%S} UTC · recuperado sin parar el app ✓")
