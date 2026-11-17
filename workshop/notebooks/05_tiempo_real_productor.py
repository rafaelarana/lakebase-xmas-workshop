# Databricks notebook source
# MAGIC %md
# MAGIC # 05 · Tiempo real (2/2): el productor — Papá Noel y los duendes entregan
# MAGIC
# MAGIC Reproduce una noche completa (toda España) a cámara rápida: cada parada planificada (`journeys`) es un **evento** cuando el reloj simulado llega a su hora. Cada ruta se reparte entre varios ayudantes (**duendes** en Nochebuena, **pajes** en Reyes). Un pequeño porcentaje de paradas son **excepciones** (chimenea bloqueada, niño despierto…); algunas se reintentan solas más tarde y el resto las revisa el agente en el app.
# MAGIC
# MAGIC | `destino` | Cómo se escribe | Para qué |
# MAGIC |---|---|---|
# MAGIC | `zerobus` | SDK de Zerobus (gRPC) → Delta bronze → `04_tiempo_real_stream` → Lakebase | El camino real: ingesta sin Kafka + streaming con Lakebase sink |
# MAGIC | `lakebase_directo` | `INSERT … ON CONFLICT` directo en Lakebase (psycopg) | Ensayo del app sin streaming; muestra la escritura OLTP directa |
# MAGIC
# MAGIC Abre el app en **/control** mientras corre. Para pararlo: *Interrupt*.

# COMMAND ----------

# MAGIC %pip install databricks-zerobus-ingest-sdk psycopg2-binary "databricks-sdk==0.122.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("catalog", "", "Catálogo (el de 00)")
dbutils.widgets.text("suffix", "", "Sufijo usado en 00 (si lo pusiste)")
dbutils.widgets.dropdown("campaign", "nochebuena", ["nochebuena", "reyes"], "Noche")
dbutils.widgets.text("year", "", "Campaña (vacío = la última)")
dbutils.widgets.text("minutes", "15", "Minutos reales que dura la noche")
dbutils.widgets.text("helpers", "4", "Ayudantes por ruta")
dbutils.widgets.text("exception_rate", "0.015", "Tasa de excepciones")
dbutils.widgets.dropdown("destino", "zerobus", ["zerobus", "lakebase_directo"], "Destino")
dbutils.widgets.dropdown("incident", "ninguna", ["ninguna", "dana_valencia", "cortes_barcelona", "filomena_madrid"],
                         "Incidencia (ráfaga de excepciones en su zona)")

# COMMAND ----------

import datetime as dt
import re
import sys
import time
from collections import Counter
from pathlib import Path

from databricks.sdk import WorkspaceClient

nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
sys.path.insert(0, str(Path("/Workspace" + nb_path).parents[2] / "navidad-hack-infra" / "scripts"))
from live_sim import BURST_WINDOW, INCIDENTS, Clock, Stop, build_events, burst_window, due, to_record
from pg import Pg
from plan import slugify
from provision_live import BRONZE_TABLE, SECRET_SCOPE, zerobus_endpoint

w = WorkspaceClient()
me = w.current_user.me().user_name
catalog = dbutils.widgets.get("catalog").strip()
assert catalog, "Pon el catálogo del workbench en el widget «catalog»"
slug = slugify(me) + re.sub(r"[^a-z0-9]", "", dbutils.widgets.get("suffix").lower())
schema, project_id = f"navidad_{slug}", f"navidad-{slug}"
campaign, destino = dbutils.widgets.get("campaign"), dbutils.widgets.get("destino")
year = int(dbutils.widgets.get("year") or spark.sql(f"SELECT max(year) FROM {catalog}.{schema}.journeys").first()[0])
minutes = float(dbutils.widgets.get("minutes"))
db = Pg(w, me, f"projects/{project_id}/branches/production")

# COMMAND ----------

# MAGIC %md ## 1 · El plan de la noche: todas las paradas de España

# COMMAND ----------

rows = spark.sql(f"""
  SELECT j.route_id, j.stop_number, j.household_id, j.bringer, h.city, j.arrived_at_utc, h.lat, h.lon
  FROM {catalog}.{schema}.journeys j JOIN {catalog}.{schema}.households h USING (household_id)
  WHERE j.year = {year} AND j.campaign = '{campaign}'""").collect()
stops = [Stop(int(r.route_id), int(r.stop_number), int(r.household_id), r.bringer, r.city,
              r.arrived_at_utc.replace(tzinfo=dt.timezone.utc), float(r.lat), float(r.lon)) for r in rows]
assert stops, f"No hay paradas para {campaign} {year} en {catalog}.{schema}.journeys: revisa la campaña y el año"
incident = dbutils.widgets.get("incident")
incident = incident if incident in INCIDENTS else None
in_zone: set[int] = set()
if incident:  # households inside the scenario's zones (navidad_geo, from the incidents track)
    try:
        in_zone = {r[0] for r in db.q(f"""SELECT h.household_id FROM navidad_geo.zone z JOIN {schema}.households_sync h
                                           ON ST_Contains(z.geom, ST_SetSRID(ST_MakePoint(h.lon, h.lat), 4326))
                                           WHERE z.scenario_id = %s""", (incident,))}
    except Exception as exc:
        print("⚠ sin zonas de incidencias (¿falta la pista de incidencias?):", type(exc).__name__)
    win = burst_window(stops, in_zone)
    franja = f"{win[0]:%H:%M}–{win[1]:%H:%M} UTC" if win else "sin paradas en la zona"
    print(f"Incidencia {INCIDENTS[incident]['label']}: {len(in_zone):,} hogares en la zona · ráfaga en el "
          f"{BURST_WINDOW[0]:.0%}–{BURST_WINDOW[1]:.0%} central de las visitas a la zona ({franja})".replace(",", "."))
events = build_events(stops, helpers=int(dbutils.widgets.get("helpers")),
                      exception_rate=float(dbutils.widgets.get("exception_rate")),
                      incident=incident, incident_households=in_zone)
night_start, night_end = events[0].event_ts, events[-1].event_ts
clock = Clock(night_start, night_end, minutes * 60)
kinds = Counter(e.event_type for e in events)
helpers_n = len({(e.helper_id) for e in events})
print(f"{campaign} {year}: {len(stops):,} paradas · {len({s.city for s in stops})} ciudades · {helpers_n} ayudantes · "
      f"eventos {dict(kinds)} · ×{clock.speed:.0f} ({minutes:g} min reales)".replace(",", "."))

# COMMAND ----------

sim_run_id = f"{campaign[:4]}-{year}-{dt.datetime.now(dt.timezone.utc):%m%d-%H%M%S}"
# One night at a time: close nights left «en_curso» by a crashed or retried producer
db.q("UPDATE navidad_live.sim_run SET status = 'cancelada', ended_at = now() WHERE status = 'en_curso'")
by_city: dict[str, list[Stop]] = {}
for s in stops:
    by_city.setdefault(s.city, []).append(s)
db.q("""INSERT INTO navidad_live.sim_run (sim_run_id, year, campaign, mode, speed, night_start, night_end, planned, helpers,
                                         exception_rate, incident)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
     (sim_run_id, year, campaign, destino, clock.speed, night_start, night_end, len(stops), helpers_n,
      float(dbutils.widgets.get("exception_rate")), incident if in_zone else None))
for city, ss in by_city.items():
    db.q("INSERT INTO navidad_live.run_plan VALUES (%s, %s, %s, %s, %s)",
         (sim_run_id, city, sum(s.lat for s in ss) / len(ss), sum(s.lon for s in ss) / len(ss), len(ss)))
print("Ejecución:", sim_run_id, "→ ábrela en el app: /control")

# COMMAND ----------

# MAGIC %md ## 2 · ¡A repartir! (Interrupt para parar)

# COMMAND ----------


class ZerobusSink:
    """Producer → Zerobus (gRPC) → Delta bronze. Records are dicts = bronze rows (JSON mode)."""

    def __init__(self):
        from zerobus.sdk.shared import RecordType, StreamConfigurationOptions, TableProperties
        from zerobus.sdk.sync import ZerobusSdk
        host = w.postgres.get_endpoint(name=f"projects/{project_id}/branches/production/endpoints/primary").status.hosts.host
        self.endpoint = zerobus_endpoint(w.get_workspace_id(), host, cloud="azure" if w.config.is_azure else "aws")
        self.sdk = ZerobusSdk(self.endpoint, w.config.host)
        # In the ▶ job, 04 (which creates the Zerobus identity on a fresh workspace) starts at the same time:
        # wait for its secret instead of failing with «Secret does not exist».
        def identity_ready() -> bool:
            if not any(sc.name == SECRET_SCOPE for sc in w.secrets.list_scopes()):
                return False
            return {"client_id", "client_secret"} <= {k.key for k in w.secrets.list_secrets(scope=SECRET_SCOPE)}

        for i in range(30):
            if identity_ready():
                break
            if i == 0:
                print("esperando a la identidad de Zerobus (la prepara 00 paso 5 o 04_tiempo_real_stream)…")
            time.sleep(10)
        else:
            raise RuntimeError(f"Falta el secreto {SECRET_SCOPE}/client_id: ejecuta 00 (paso 5) o 04_tiempo_real_stream una vez")
        self.args = (dbutils.secrets.get(SECRET_SCOPE, "client_id"), dbutils.secrets.get(SECRET_SCOPE, "client_secret"),
                     TableProperties(f"{catalog}.{schema}.{BRONZE_TABLE}"),
                     StreamConfigurationOptions(record_type=RecordType.JSON))
        self.stream = self.sdk.create_stream(*self.args)
        print("Zerobus:", self.endpoint)

    def send(self, records):
        for attempt in (1, 2):
            try:
                for r in records:
                    self.stream.ingest_record_nowait(r)
                self.stream.flush()  # durable in Delta when this returns
                return
            except Exception as exc:  # stream dropped: reopen once and resend (at-least-once; the sink dedupes)
                if attempt == 2:
                    raise
                print("reabriendo el stream de Zerobus…", type(exc).__name__)
                self.stream = self.sdk.create_stream(*self.args)

    def close(self):
        self.stream.close()


class DirectSink:
    """Ensayo: the same rows straight into Lakebase (what the streaming sink would write)."""

    D = ("sim_run_id", "household_id", "city", "route_id", "stop_number", "bringer", "helper_id", "helper_kind",
         "status", "exception_code", "gifts", "lat", "lon", "event_ts", "sent_ts")
    H = ("sim_run_id", "helper_id", "helper_name", "helper_kind", "bringer", "city", "route_id", "stop_number",
         "lat", "lon", "event_ts", "sent_ts")
    X = ("event_id", "sim_run_id", "household_id", "city", "route_id", "stop_number", "bringer", "helper_id",
         "helper_name", "helper_kind", "exception_code", "lat", "lon", "event_ts", "sent_ts")

    @staticmethod
    def _upsert(cur, table, cols, key, rows):
        from psycopg2.extras import execute_values
        if not rows:
            return
        upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in key)
        execute_values(cur, f"INSERT INTO navidad_live.{table} ({', '.join(cols)}) VALUES %s "
                            f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {upd}", rows, page_size=1000)

    def send(self, records):
        ts = lambda us: dt.datetime.fromtimestamp(us / 1e6, dt.timezone.utc)
        deliveries, helpers, exceptions = {}, {}, []
        for r in records:  # records are in event order: the last one per key wins
            r = {**r, "event_ts": ts(r["event_ts"]), "sent_ts": ts(r["sent_ts"]), "status": r["event_type"]}
            deliveries[(r["sim_run_id"], r["household_id"])] = tuple(r[c] for c in self.D)
            helpers[(r["sim_run_id"], r["helper_id"])] = tuple(r[c] for c in self.H)
            if r["event_type"] == "excepcion":
                exceptions.append(tuple(r[c] for c in self.X))

        def write(cur):
            self._upsert(cur, "delivery_status", self.D, ("sim_run_id", "household_id"), list(deliveries.values()))
            self._upsert(cur, "helper_position", self.H, ("sim_run_id", "helper_id"), list(helpers.values()))
            self._upsert(cur, "exception_event", self.X, ("event_id",), exceptions)
        db.transaction(write)

    def close(self):
        pass


out = ZerobusSink() if destino == "zerobus" else DirectSink()
if destino == "zerobus":
    # Launched by the app's ▶ button, 04 starts at the same time: wait (≤ 4 min) until its stream is running,
    # so the night is not half over when the first events reach Lakebase.
    t_wait = time.time()
    while time.time() - t_wait < 240:
        if db.q("SELECT count(*) FROM navidad_live.stream_progress WHERE started_at > now() - interval '2 minutes'")[0][0]:
            break
        if int(time.time() - t_wait) % 30 == 0:
            print("esperando a que arranque 04_tiempo_real_stream…")
        time.sleep(3)
    else:
        print("⚠ no veo el stream (04): envío igualmente; los eventos esperan en bronze")
clock.real_start = dt.datetime.now(dt.timezone.utc)
i, sent, last_log, status = 0, 0, 0.0, "terminada"
try:
    while i < len(events):
        now = dt.datetime.now(dt.timezone.utc)
        idx = list(due(events, i, clock.sim_now(now)))
        if idx:
            out.send([to_record(events[k], sim_run_id=sim_run_id, year=year, campaign=campaign, sent=now) for k in idx])
            i, sent = idx[-1] + 1, sent + len(idx)
        if time.time() - last_log > 15:
            last_log = time.time()
            local = clock.sim_now().astimezone(dt.timezone(dt.timedelta(hours=1)))
            print(f"{local:%H:%M} (hora peninsular) · {sent:,} eventos · {100 * i / len(events):4.1f} %".replace(",", "."))
        time.sleep(0.5)
except KeyboardInterrupt:
    status = "cancelada"
finally:
    out.close()
    db.q("UPDATE navidad_live.sim_run SET status = %s, ended_at = now() WHERE sim_run_id = %s", (status, sim_run_id))
print(f"Noche {status}: {sent:,} eventos enviados por {destino}.".replace(",", "."))
