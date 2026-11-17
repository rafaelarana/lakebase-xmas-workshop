import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "templates" / "control"))
sys.path.insert(0, str(ROOT / "templates" / "search"))
pytest.importorskip("fastapi")
import search  # noqa: E402
from provision_search import crew_segments, search_sql  # noqa: E402


def test_query_normalisation_matches_the_index_analyser():
    assert search._norm_tokens("Familia Núñez-García, Antía!") == ["familia", "nunez", "garcia", "antia"]
    assert search._norm_tokens("a b c d e f g h") == ["a", "b", "c", "d", "e", "f"]   # capped


def test_crew_segments_match_the_control_center_split():
    from live_sim import Stop, assign_helpers
    import datetime as dt
    t0 = dt.datetime(2025, 12, 24, tzinfo=dt.timezone.utc)
    stops = [Stop(45, i, i, "papa_noel", "Madrid", t0, 40.0, -3.0) for i in range(1, 1001)]
    who = assign_helpers(stops, 4)
    segs = crew_segments(45, 1000, 1, 4, "papa_noel")
    for s in segs:
        assert {who[(45, n)][0] for n in range(s["stop_from"], s["stop_to"] + 1)} == {s["helper_id"]}
        assert who[(45, s["stop_from"])][1] == s["name"]
    assert segs[0]["stop_from"] == 1 and segs[-1]["stop_to"] == 1000
    assert crew_segments(7, 10, 1, 4, "melchor")[0]["kind"] == "paje"


def test_index_sql_avoids_stopword_tables_and_builds_bm25_after_loading():
    sql = search_sql("navidad_x")
    assert "__SCHEMA__" not in sql and "navidad_x.households_sync" in sql
    assert "to_tsvector('simple'" in sql and "Stopwords" not in sql          # replica-safe analyser
    assert "USING lakebase_bm25 (search_vector)" in sql and "USING gin (search_vector)" in sql
    assert "RETURN 'fts'" in sql                                               # works without Lakebase Search


class Reader:
    def __init__(self, bm25=True):
        self.sql, self.bm25 = [], bm25

    def q(self, sql, params=None):
        self.sql.append(sql)
        if "to_regclass" in sql:
            return [{"doc": True, "bm25": self.bm25}]
        if "to_bm25query" in sql:
            return [{"doc_id": "h:1", "kind": "familia", "title": "Familia García Ortega", "subtitle": "", "city": "Madrid", "lat": 1, "lon": 2},
                    {"doc_id": "h:2", "kind": "familia", "title": "Familia Ortega García", "subtitle": "", "city": "Bilbao", "lat": 1, "lon": 2}]
        if "to_tsquery" in sql:
            return [{"doc_id": "h:2", "kind": "familia", "title": "Familia Ortega García", "subtitle": "", "city": "Bilbao", "lat": 1, "lon": 2},
                    {"doc_id": "h:3", "kind": "familia", "title": "Familia García Garrido", "subtitle": "", "city": "Vigo", "lat": 1, "lon": 2}]
        return []


def test_bm25_and_prefix_results_are_fused():
    s = search.Search(Reader())
    out = s.search("garcia ortega", 3)
    assert out["engine"] == "bm25" and [r["doc_id"] for r in out["results"]] == ["h:2", "h:1", "h:3"]
    assert not any("ts_rank" in q for q in s.reader.sql)       # prefix candidates are not ranked (cost)


def test_without_lakebase_search_only_the_prefix_match_runs():
    r = Reader(bm25=False)
    out = search.Search(r).search("garcia", 5)
    assert out["engine"] == "fts" and not any("to_bm25query" in q for q in r.sql)
    assert search.Search(r).search("g", 5)["results"] == []     # too short
