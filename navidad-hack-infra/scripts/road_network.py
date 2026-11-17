"""Road graph for the incident hack track (Madrid, Barcelona, Valencia).

Fetches the drivable OpenStreetMap network around each city's households from the
public Overpass API, splits OSM ways into routable edges at intersections, and loads
them into the native Lakebase schema ``navidad_geo`` (``ways`` + ``vertices``) for
pgRouting. Pure Python (stdlib + psycopg2); no osmnx/geopandas.

Data © OpenStreetMap contributors, ODbL 1.0. Show that attribution on any map.

Geometry stays out of the synced schema on purpose: Lakebase synced tables (and CDF)
do not carry PostGIS geometry, so ``navidad_geo`` is a separate, Postgres-native schema.
"""

from __future__ import annotations

import io
import json
import math
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

from generate_data import ES_CITIES, MAX_OFFSET_DEG

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "navidad-hack-skills/1.0 (Databricks Lakebase hackathon)"
INCIDENT_CITIES = ("Madrid", "Barcelona", "Valencia")
BBOX_MARGIN_DEG = 0.01  # ~1 km beyond the furthest household

# Night-time free-flow speeds (km/h) by OSM highway class.
SPEED_KMH = {
    "motorway": 90, "trunk": 70, "primary": 50, "secondary": 40, "tertiary": 35,
    "unclassified": 30, "residential": 25, "living_street": 10,
}
LINK_FACTOR = 0.7  # *_link ramps run slower than their parent class
MAIN_CLASSES = {"motorway", "trunk", "primary", "secondary"}
HIGHWAY_RE = (
    "^(motorway|motorway_link|trunk|trunk_link|primary|primary_link|secondary|secondary_link|"
    "tertiary|tertiary_link|unclassified|residential|living_street)$"
)


@dataclass
class Edge:
    id: int
    city: str
    source: int
    target: int
    highway: str
    main: bool
    length_m: float
    cost_s: float
    reverse_cost_s: float  # -1 = cannot be traversed target → source (one-way)
    coords: list[tuple[float, float]] = field(default_factory=list)  # (lon, lat)


def city_bbox(city: str, margin: float = BBOX_MARGIN_DEG) -> tuple[float, float, float, float]:
    """(south, west, north, east) covering every household of ``city`` plus a margin."""
    c = next((c for c in ES_CITIES if c.name == city), None)
    if c is None:
        raise ValueError(f"unknown city {city!r}")
    r = MAX_OFFSET_DEG + margin
    return (round(c.lat - r, 4), round(c.lon - r, 4), round(c.lat + r, 4), round(c.lon + r, 4))


def overpass_query(bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox
    return (
        "[out:json][timeout:300];"
        f'way["highway"~"{HIGHWAY_RE}"]["access"!~"^(no|private)$"]["area"!="yes"]({s},{w},{n},{e});'
        "(._;>;);out body qt;"
    )


def fetch_osm(bbox: tuple[float, float, float, float], *, url: str = OVERPASS_URL,
              attempts: int = 4, opener: Any = None) -> dict[str, Any]:
    """POST the Overpass query; retries on 429/5xx with backoff."""
    body = urllib.parse.urlencode({"data": overpass_query(bbox)}).encode()
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
    open_ = opener or urllib.request.urlopen
    last: Exception | None = None
    for i in range(attempts):
        try:
            with open_(req, timeout=360) as resp:
                return json.loads(resp.read())
        except Exception as exc:  # urllib raises HTTPError/URLError; retry both
            last = exc
            time.sleep(10 * (i + 1))
    raise RuntimeError(f"Overpass fetch failed after {attempts} attempts: {last}")


def _speed_kmh(tags: dict[str, str]) -> float:
    hw = tags.get("highway", "")
    base = hw.removesuffix("_link")
    speed = float(SPEED_KMH.get(base, 25))
    if hw.endswith("_link"):
        speed *= LINK_FACTOR
    m = re.match(r"^\s*(\d+)", tags.get("maxspeed", ""))
    if m:
        speed = min(speed, float(m.group(1)))  # never faster than the posted limit
    return max(speed, 5.0)


def _oneway(tags: dict[str, str]) -> int:
    """1 = forward only, -1 = reverse only, 0 = both ways."""
    v = tags.get("oneway", "").lower()
    if v in ("yes", "true", "1"):
        return 1
    if v == "-1":
        return -1
    if v == "no":
        return 0
    if tags.get("highway") in ("motorway", "motorway_link") or tags.get("junction") in ("roundabout", "circular"):
        return 1
    return 0


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(a))


def build_edges(osm: dict[str, Any], city: str, start_id: int = 1) -> list[Edge]:
    """Split OSM ways at shared nodes (intersections) into pgRouting edges."""
    nodes: dict[int, tuple[float, float]] = {}
    ways: list[dict[str, Any]] = []
    for el in osm.get("elements", []):
        if el.get("type") == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])
        elif el.get("type") == "way" and len(el.get("nodes", [])) >= 2:
            ways.append(el)

    use: dict[int, int] = {}
    for w in ways:
        for nid in w["nodes"]:
            use[nid] = use.get(nid, 0) + 1
        use[w["nodes"][0]] = use.get(w["nodes"][0], 0) + 1  # endpoints are always vertices
        use[w["nodes"][-1]] = use.get(w["nodes"][-1], 0) + 1

    edges: list[Edge] = []
    eid = start_id
    for w in ways:
        tags = w.get("tags", {})
        hw = tags.get("highway", "")
        direction = _oneway(tags)
        speed_ms = _speed_kmh(tags) / 3.6
        seq = [n for n in w["nodes"] if n in nodes]
        if direction == -1:
            seq.reverse()
            direction = 1
        start = 0
        for i in range(1, len(seq)):
            if i < len(seq) - 1 and use.get(seq[i], 0) < 2:
                continue  # interior shape point, not an intersection
            part = seq[start : i + 1]
            start = i
            if part[0] == part[-1] and len(part) <= 2:
                continue
            coords = [nodes[n] for n in part]
            length = sum(haversine_m(*coords[k], *coords[k + 1]) for k in range(len(coords) - 1))
            if length <= 0:
                continue
            cost = length / speed_ms
            edges.append(Edge(
                id=eid, city=city, source=part[0], target=part[-1], highway=hw,
                main=hw.removesuffix("_link") in MAIN_CLASSES, length_m=round(length, 2),
                cost_s=round(cost, 3), reverse_cost_s=-1.0 if direction == 1 else round(cost, 3),
                coords=coords,
            ))
            eid += 1
    return edges


def vertices_of(edges: Iterable[Edge]) -> dict[int, tuple[float, float]]:
    out: dict[int, tuple[float, float]] = {}
    for e in edges:
        out.setdefault(e.source, e.coords[0])
        out.setdefault(e.target, e.coords[-1])
    return out


def _linestring(coords: list[tuple[float, float]]) -> str:
    return "SRID=4326;LINESTRING(" + ",".join(f"{x:.7f} {y:.7f}" for x, y in coords) + ")"


def load_edges(conn: Any, city: str, edges: list[Edge], osm_timestamp: str = "") -> dict[str, int]:
    """Replace one city's graph in navidad_geo (schema from incidents.sql must exist)."""
    verts = vertices_of(edges)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM navidad_geo.ways WHERE city = %s", (city,))
        cur.execute("DELETE FROM navidad_geo.vertices WHERE city = %s", (city,))
        buf = io.StringIO()
        for vid, (x, y) in verts.items():
            buf.write(f"{vid}\t{city}\tSRID=4326;POINT({x:.7f} {y:.7f})\n")
        buf.seek(0)
        cur.copy_expert("COPY navidad_geo.vertices (id, city, geom) FROM STDIN", buf)
        buf = io.StringIO()
        for e in edges:
            buf.write(
                f"{e.id}\t{city}\t{e.source}\t{e.target}\t{e.highway}\t{'t' if e.main else 'f'}\t"
                f"{e.length_m}\t{e.cost_s}\t{e.reverse_cost_s}\t{e.cost_s}\t{e.reverse_cost_s}\t{_linestring(e.coords)}\n"
            )
        buf.seek(0)
        cur.copy_expert(
            "COPY navidad_geo.ways (id, city, source, target, highway, main, length_m, cost_base, "
            "reverse_cost_base, cost, reverse_cost, geom) FROM STDIN",
            buf,
        )
        cur.execute(
            "INSERT INTO navidad_geo.osm_meta (city, edges, vertices, osm_timestamp, loaded_at) "
            "VALUES (%s, %s, %s, %s, now()) ON CONFLICT (city) DO UPDATE SET edges = EXCLUDED.edges, "
            "vertices = EXCLUDED.vertices, osm_timestamp = EXCLUDED.osm_timestamp, loaded_at = now()",
            (city, len(edges), len(verts), osm_timestamp),
        )
    return {"edges": len(edges), "vertices": len(verts)}


def city_id_offset(city: str) -> int:
    """Disjoint edge-id ranges per city so reloading one city never collides with another."""
    return (INCIDENT_CITIES.index(city) + 1) * 10_000_000


# --- Pre-built graph files (workshops: no Overpass dependency) --------------------
GRAPH_HEADER = "id,source,target,highway,main,length_m,cost_s,reverse_cost_s,coords"
GRAPH_NOTICE = "# Road graph derived from OpenStreetMap. Data (c) OpenStreetMap contributors, ODbL 1.0: https://www.openstreetmap.org/copyright"


def save_edges(path: Any, edges: list[Edge], osm_timestamp: str = "") -> None:
    """Write edges as gzip CSV (coords 'lon lat;lon lat;…'), with the ODbL notice."""
    import gzip

    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(f"{GRAPH_NOTICE}\n# osm_timestamp={osm_timestamp}\n{GRAPH_HEADER}\n")
        for e in edges:
            coords = ";".join(f"{x:.6f} {y:.6f}" for x, y in e.coords)
            f.write(f"{e.id},{e.source},{e.target},{e.highway},{int(e.main)},{e.length_m},{e.cost_s},{e.reverse_cost_s},{coords}\n")


def read_edges(path: Any, city: str) -> tuple[list[Edge], str]:
    """Inverse of save_edges. Returns (edges, osm_timestamp)."""
    import gzip

    edges: list[Edge] = []
    ts = ""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.startswith("# osm_timestamp="):
                ts = line.strip().split("=", 1)[1]
            if line.startswith("#") or line.startswith("id,"):
                continue
            i, s, t, hw, main, length, cost, rev, coords = line.rstrip("\n").split(",", 8)
            edges.append(Edge(
                id=int(i), city=city, source=int(s), target=int(t), highway=hw, main=main == "1",
                length_m=float(length), cost_s=float(cost), reverse_cost_s=float(rev),
                coords=[tuple(map(float, p.split(" "))) for p in coords.split(";")],
            ))
    return edges, ts
