"""Bonus «Búsqueda»: search bar on every page, backed by Lakebase Search (BM25).

Index: navidad_search.doc (provision_search.py / notebook 08_busqueda) — families and duende/paje crews.
Ranking: the lakebase_bm25 index when Lakebase Search is enabled on the project, merged (RRF) with a
prefix match on the GIN index so the box works as typeahead; without Search, the GIN prefix match alone.
Reads go through the control center's ReadRouter (read replica when there is one).

Mount with ``mount_search(app)`` (needs control.py in the same app).
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

from fastapi import FastAPI, HTTPException

from control import Db, ReadRouter

BM25_INDEX = "navidad_search.doc_bm25"
MAX_PATH_POINTS = 1500
_TOKEN = re.compile(r"[a-z0-9]+")


def _norm_tokens(q: str) -> list[str]:
    """Same normalisation as navidad_search.norm (lower + unaccent, so «Núñez» → «nunez»)."""
    import unicodedata
    q = "".join(c for c in unicodedata.normalize("NFD", q.lower()) if unicodedata.category(c) != "Mn")
    return _TOKEN.findall(q)[:6]


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _clean(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: _iso(v) for k, v in r.items()} for r in rows]


class Search:
    def __init__(self, reader: Any):
        self.reader, self._engine, self._checked = reader, None, 0.0
        self._sug: tuple[float, dict[str, Any]] | None = None

    def engine(self) -> str | None:
        """'bm25' | 'fts' | None (index not built). Re-checked every 5 minutes."""
        if self._engine is None or time.time() - self._checked > 300:
            rows = self.reader.q("""SELECT to_regclass('navidad_search.doc') IS NOT NULL AS doc,
                                           to_regclass(%s) IS NOT NULL AS bm25""", (BM25_INDEX,))[0]
            self._engine = ("bm25" if rows["bm25"] else "fts") if rows["doc"] else None
            self._checked = time.time()
        return self._engine

    def search(self, q: str, limit: int = 8) -> dict[str, Any]:
        eng = self.engine()
        if not eng:
            raise HTTPException(409, "la búsqueda no está preparada: ejecuta el notebook 08_busqueda")
        toks = _norm_tokens(q)
        if not toks or len("".join(toks)) < 2:
            return {"engine": eng, "ms": 0, "results": []}
        t0 = time.time()
        cols = "doc_id, kind, title, subtitle, city, lat, lon"
        # typeahead candidates: prefix match on the GIN index, unranked (ranking every prefix match costs
        # ~150 ms on 500K docs; BM25 ranks in ~3 ms) — BM25 order wins in the fusion below
        prefix = self.reader.q(f"""SELECT {cols} FROM navidad_search.doc
                                   WHERE search_vector @@ to_tsquery('simple', %s) LIMIT %s""",
                               (" & ".join(t + ":*" for t in toks), limit * 3))
        bm25 = []
        if eng == "bm25":
            bm25 = self.reader.q(f"""SELECT {cols} FROM navidad_search.doc
                                     ORDER BY search_vector <@> to_bm25query(to_tsvector('simple', %s), %s)
                                     LIMIT %s""", (" ".join(toks), BM25_INDEX, limit * 3))
        score: dict[str, float] = {}
        rows: dict[str, dict[str, Any]] = {}
        for lst, w in ((bm25, 1.0), (prefix, 0.5)):     # reciprocal rank fusion (BM25 weighs more)
            for rk, r in enumerate(lst):
                score[r["doc_id"]] = score.get(r["doc_id"], 0) + w / (60 + rk)
                rows[r["doc_id"]] = r
        best = sorted(score, key=lambda k: -score[k])[:limit]
        return {"engine": eng, "ms": round((time.time() - t0) * 1000), "results": _clean([rows[k] for k in best])}

    def suggestions(self) -> dict[str, Any]:
        if not self.engine():
            return {"available": False, "suggestions": []}
        if self._sug and time.time() - self._sug[0] < 600:
            return self._sug[1]
        fam = self.reader.q("""SELECT title, city FROM navidad_search.doc WHERE kind = 'familia' AND city IN ('Madrid', 'Barcelona')
                               ORDER BY hashint8(household_id + extract(doy FROM now())::bigint) LIMIT 1""")
        crew = self.reader.q("""SELECT split_part(title, ' ', 2) AS n FROM navidad_search.doc WHERE kind = 'duende'
                                ORDER BY hashint8(route_id + extract(doy FROM now())::bigint) LIMIT 1""")
        sug = []
        if fam:
            sug.append({"q": fam[0]["title"], "label": f"{fam[0]['title']} · {fam[0]['city']}"})
        if crew:
            sug.append({"q": f"duende {crew[0]['n']}", "label": f"duende {crew[0]['n']} (su recorrido)"})
        sug += [{"q": "pajes melchor sevilla", "label": "pajes de Melchor en Sevilla"},
                {"q": "Antía", "label": "familias con alguien llamado Antía"}]
        out = {"available": True, "engine": self.engine(), "suggestions": sug}
        self._sug = (time.time(), out)
        return out

    def doc(self, doc_id: str, schema: str) -> dict[str, Any]:
        rows = self.reader.q("SELECT * FROM navidad_search.doc WHERE doc_id = %s", (doc_id,))
        if not rows:
            raise HTTPException(404, "no encontrado")
        d = rows[0]
        d.pop("search_vector", None)
        sch = _ident(schema)
        if d["kind"] == "familia":
            hid = d["household_id"]
            people = self.reader.q(f"""SELECT p.given_name, p.age_years, p.role, pr.gift_affinity, pr.window_policy
                                       FROM {sch}.people_sync p LEFT JOIN {sch}.preferences_sync pr USING (person_id)
                                       WHERE p.household_id = %s ORDER BY p.age_years DESC""", (hid,))
            hist = self.reader.q(f"""SELECT j.year, j.campaign, j.bringer, j.route_id, j.stop_number, j.arrived_at_utc,
                                            j.dwell_minutes, j.is_revisit, h.timezone,
                                            (SELECT c.title FROM navidad_search.doc c
                                              WHERE c.kind IN ('duende', 'paje') AND c.year = j.year AND c.campaign = j.campaign
                                                AND c.route_id = j.route_id AND j.stop_number BETWEEN c.stop_from AND c.stop_to) AS crew
                                     FROM {sch}.journeys_sync j JOIN {sch}.households_sync h USING (household_id)
                                     WHERE j.household_id = %s ORDER BY j.year DESC, j.campaign, j.arrived_at_utc""", (hid,))
            try:
                live = self.reader.q("""SELECT d.sim_run_id, d.status, d.helper_id, d.event_ts, d.updated_at
                                        FROM navidad_live.delivery_status d JOIN navidad_live.sim_run r USING (sim_run_id)
                                        WHERE d.household_id = %s ORDER BY r.started_at DESC LIMIT 1""", (hid,))
            except Exception:
                live = []
            return {**_clean([d])[0], "people": _clean(people), "history": _clean(hist), "live": _clean(live)[0] if live else None}
        path = self.reader.q(f"""SELECT j.stop_number, j.arrived_at_utc, h.lat, h.lon, h.timezone
                                 FROM {sch}.journeys_sync j JOIN {sch}.households_sync h USING (household_id)
                                 WHERE j.year = %s AND j.campaign = %s AND j.route_id = %s
                                   AND j.stop_number BETWEEN %s AND %s ORDER BY j.stop_number""",
                             (d["year"], d["campaign"], d["route_id"], d["stop_from"], d["stop_to"]))
        n = len(path)
        step = max(1, -(-n // MAX_PATH_POINTS))
        pts = path[::step] + ([path[-1]] if n and (n - 1) % step else [])
        return {**_clean([d])[0], "stops": n, "first_utc": _iso(path[0]["arrived_at_utc"]) if path else None,
                "last_utc": _iso(path[-1]["arrived_at_utc"]) if path else None,
                "timezone": path[0]["timezone"] if path else None,
                "path": [[float(p["lat"]), float(p["lon"])] for p in pts]}


def _ident(name: str) -> str:
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError(f"unsafe identifier {name!r}")
    return name


def mount_search(app: FastAPI, client: Any = None) -> Search:
    reader = ReadRouter(Db(os.environ.get("NAVIDAD_PROJECT_ID", ""), client))
    schema = os.environ.get("NAVIDAD_SCHEMA", "public")
    s = Search(reader)

    def guard(fn, *a):
        try:
            return fn(*a)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.get("/api/search/status")
    def search_status() -> dict[str, Any]:
        return guard(lambda: {"engine": s.engine(), "reads": reader.target})

    @app.get("/api/search/suggestions")
    def search_suggestions() -> dict[str, Any]:
        return guard(s.suggestions)

    @app.get("/api/search")
    def search(q: str, limit: int = 8) -> dict[str, Any]:
        return guard(s.search, q, max(1, min(limit, 20)))

    @app.get("/api/search/doc/{doc_id}")
    def search_doc(doc_id: str) -> dict[str, Any]:
        return guard(s.doc, doc_id, schema)

    return s
