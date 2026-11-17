"""Bonus «Búsqueda»: build the Lakebase Search index over families and duende/paje crews.

Needs Lakebase Search enabled on the project for BM25 (Project → Settings → Lakebase Search; irreversible,
restarts the project's computes). Without it, the same table works with Postgres full-text (GIN).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from live_sim import DUENDES, PAJES, helper_kind

SKILL_ROOT = Path(__file__).resolve().parents[1]
SQL_FILE = SKILL_ROOT / "sql" / "search.sql"


def _ident(name: str) -> str:
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError(f"unsafe identifier {name!r}")
    return name


def search_sql(schema: str) -> str:
    return SQL_FILE.read_text().replace("__SCHEMA__", _ident(schema))


def crew_segments(route_id: int, n_stops: int, first_stop: int, helpers: int, bringer: str) -> list[dict[str, Any]]:
    """Same split and names as live_sim.assign_helpers: contiguous segments of ceil(n/k) stops."""
    k = max(1, min(helpers, n_stops))
    size = -(-n_stops // k)
    kind = helper_kind(bringer)
    names = PAJES if kind == "paje" else DUENDES
    out = []
    for seg in range(k):
        lo = first_stop + seg * size
        hi = min(first_stop + n_stops - 1, lo + size - 1)
        if lo > hi:
            break
        out.append({"helper_id": f"{route_id}-{seg + 1}", "name": f"{names[(route_id * 7 + seg) % len(names)]} {seg + 1}",
                    "kind": kind, "stop_from": lo, "stop_to": hi})
    return out


BRINGERS = {"papa_noel": "Papá Noel", "olentzero": "Olentzero", "tio": "Tió de Nadal", "apalpador": "Apalpador",
            "melchor": "Melchor", "gaspar": "Gaspar", "baltasar": "Baltasar"}


def load_crews(cur: Any, schema: str, helpers: int = 4, year: int | None = None) -> int:
    sch = _ident(schema)
    if year is None:
        cur.execute(f"SELECT max(year) FROM {sch}.journeys_sync")
        year = cur.fetchone()[0]
    cur.execute(f"""SELECT j.campaign, j.route_id, min(j.bringer), min(h.city), count(*), min(j.stop_number)
                    FROM {sch}.journeys_sync j JOIN {sch}.households_sync h USING (household_id)
                    WHERE j.year = %s GROUP BY 1, 2""", (year,))
    rows = []
    for campaign, route_id, bringer, city, n, first in cur.fetchall():
        night = f"Reyes {year + 1}" if campaign == "reyes" else f"Nochebuena {year}"  # Reyes falls in January
        for c in crew_segments(int(route_id), int(n), int(first), helpers, bringer):
            who = "Paje" if c["kind"] == "paje" else "Duende"
            title = f"{who} {c['name']}"
            sub = f"{BRINGERS.get(bringer, bringer)} · {city} · {night} · paradas {c['stop_from']:,}–{c['stop_to']:,}".replace(",", ".")
            body = (f"{who.lower()} {c['name']} {'pajes' if who == 'Paje' else 'duendes'} equipo {BRINGERS.get(bringer, bringer)} "
                    f"{city} {night.split()[0]} ruta {route_id}")
            rows.append((f"c:{campaign}:{year}:{c['helper_id']}", c["kind"], title, sub, city, campaign, year, int(route_id),
                         c["stop_from"], c["stop_to"], bringer, body, body))
    cur.execute("DELETE FROM navidad_search.doc WHERE kind IN ('duende', 'paje')")
    from psycopg2.extras import execute_values
    execute_values(cur, """INSERT INTO navidad_search.doc (doc_id, kind, title, subtitle, city, campaign, year, route_id,
                                                         stop_from, stop_to, bringer, body, search_vector)
                           SELECT v.doc_id, v.kind, v.title, v.subtitle, v.city, v.campaign, v.year, v.route_id,
                                  v.stop_from, v.stop_to, v.bringer, v.body,
                                  to_tsvector('simple', navidad_search.norm(v.vec))
                           FROM (VALUES %s) AS v(doc_id, kind, title, subtitle, city, campaign, year, route_id,
                                                 stop_from, stop_to, bringer, body, vec)""", rows,
                   template="(%s, %s, %s, %s, %s, %s, %s::int, %s::bigint, %s::bigint, %s::bigint, %s, %s, %s)")
    # where the crew works: centre of its segment
    cur.execute(f"""UPDATE navidad_search.doc d SET lat = s.lat, lon = s.lon
                    FROM (SELECT d2.doc_id, avg(h.lat) AS lat, avg(h.lon) AS lon
                          FROM navidad_search.doc d2
                          JOIN {sch}.journeys_sync j ON j.year = d2.year AND j.campaign = d2.campaign AND j.route_id = d2.route_id
                                                     AND j.stop_number BETWEEN d2.stop_from AND d2.stop_to
                          JOIN {sch}.households_sync h ON h.household_id = j.household_id
                          WHERE d2.kind IN ('duende', 'paje') GROUP BY d2.doc_id) s
                    WHERE d.doc_id = s.doc_id""")
    return len(rows)


def provision_search(*, conn: Any, schema: str, app_sp: str | None = None, helpers: int = 4,
                     log: Callable[[str], None] = print) -> dict[str, Any]:
    """conn: autocommit psycopg2 connection to the production branch as the workbench owner."""
    out: dict[str, Any] = {}
    with conn.cursor() as cur:
        t0 = time.time()
        cur.execute(search_sql(schema))
        cur.execute("SELECT navidad_search.load_families()")
        out["families"] = cur.fetchone()[0]
        out["families_s"] = round(time.time() - t0, 1)
        log(f"{out['families']:,} familias indexadas en {out['families_s']} s".replace(",", "."))
        t0 = time.time()
        out["crews"] = load_crews(cur, schema, helpers)
        log(f"{out['crews']} equipos de duendes / pajes indexados en {time.time() - t0:.1f} s")
        t0 = time.time()
        cur.execute("SELECT navidad_search.build_bm25()")
        out["engine"] = cur.fetchone()[0]
        out["index_s"] = round(time.time() - t0, 1)
        log(f"índice {out['engine']} construido en {out['index_s']} s")
        if app_sp:
            sp = '"' + app_sp.replace('"', "") + '"'
            cur.execute(f"GRANT USAGE ON SCHEMA navidad_search TO {sp}")
            cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA navidad_search TO {sp}")
            cur.execute(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA navidad_search TO {sp}")
            log(f"permisos de lectura para {app_sp}")
    return out
