"""Route map track: «Mapa de rutas» (Leaflet) over the synced journeys.

Endpoints: GET /api/route/options · GET /api/route · GET /api/revisits.
Page: static/map.html (served at / when installed by app_template_for_workshop).
Mount with ``mount_map(app, run_sql)`` inside create_app(); ``run_sql(query, params)``
is the app's execute_sql_from_pg.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from fastapi import FastAPI, HTTPException


def _quote_ident(name: str) -> str:
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError(f"unsafe identifier {name!r}")
    return name


CAMPAIGNS = {"nochebuena", "reyes"}
ROUTE_SAMPLE_PER_ROUTE = 2000

OPTIONS_CITIES_SQL = """
SELECT city, autonomous_community, min(timezone) AS timezone,
       avg(lat) AS lat, avg(lon) AS lon, count(*) AS households
FROM {schema}.households_sync
GROUP BY city, autonomous_community
ORDER BY households DESC
"""

OPTIONS_YEARS_SQL = "SELECT min(year) AS min_year, max(year) AS max_year FROM {schema}.journeys_sync"

# Per-route KPIs over the full route (not the sample). hop_km = haversine between consecutive stops.
ROUTE_SUMMARY_SQL = """
WITH r AS (
  SELECT j.route_id, j.bringer, j.stop_number, j.arrived_at_utc, j.dwell_minutes, j.is_revisit,
         h.lat, h.lon, h.timezone, h.household_id
  FROM {schema}.journeys_sync j
  JOIN {schema}.households_sync h ON h.household_id = j.household_id
  WHERE j.year = %s AND j.campaign = %s AND h.city = %s
), d AS (
  SELECT *, 2 * 6371 * asin(sqrt(
           power(sin(radians(lat - lag(lat) OVER w) / 2), 2)
           + cos(radians(lat)) * cos(radians(lag(lat) OVER w))
             * power(sin(radians(lon - lag(lon) OVER w) / 2), 2))) AS hop_km
  FROM r WINDOW w AS (PARTITION BY route_id ORDER BY stop_number)
)
SELECT route_id, bringer, count(*) AS stops, count(DISTINCT household_id) AS households,
       round(coalesce(sum(hop_km), 0)::numeric, 1)::float AS km,
       round(coalesce(max(hop_km), 0)::numeric, 2)::float AS max_hop_km,
       min(arrived_at_utc) AS first_at_utc, max(arrived_at_utc) AS last_at_utc,
       sum(is_revisit::int) AS revisits, sum(dwell_minutes) AS dwell_minutes,
       min(timezone) AS timezone
FROM d GROUP BY route_id, bringer ORDER BY stops DESC
"""

# Evenly sampled stops per route (every k-th stop), always keeping revisits and the last stop.
ROUTE_STOPS_SQL = """
WITH r AS (
  SELECT j.route_id, j.bringer, j.stop_number, j.arrived_at_utc, j.dwell_minutes, j.is_revisit,
         h.household_id, h.label, h.household_size, h.lat, h.lon, h.timezone,
         count(*) OVER (PARTITION BY j.route_id) AS n
  FROM {schema}.journeys_sync j
  JOIN {schema}.households_sync h ON h.household_id = j.household_id
  WHERE j.year = %s AND j.campaign = %s AND h.city = %s
)
SELECT route_id, bringer, stop_number, arrived_at_utc, dwell_minutes, is_revisit,
       household_id, label, household_size, lat, lon, timezone
FROM r
WHERE is_revisit OR stop_number %% GREATEST(1, CEIL(n / %s::numeric)::int) = 1 OR stop_number = n
ORDER BY route_id, stop_number
"""


# Revisits with their original visit and the detour they caused:
# detour_km = d(prev, revisit) + d(revisit, next) - d(prev, next).
REVISITS_SQL = """
WITH s AS (
  SELECT j.route_id, j.bringer, j.stop_number, j.household_id, j.is_revisit, j.dwell_minutes,
         j.arrived_at_utc, h.label, h.lat, h.lon, h.timezone,
         lag(h.lat) OVER w AS plat, lag(h.lon) OVER w AS plon,
         lead(h.lat) OVER w AS nlat, lead(h.lon) OVER w AS nlon
  FROM {schema}.journeys_sync j
  JOIN {schema}.households_sync h ON h.household_id = j.household_id
  WHERE j.year = %s AND j.campaign = %s AND h.city = %s
  WINDOW w AS (PARTITION BY j.route_id ORDER BY j.stop_number)
), r AS (
  SELECT s.*, o.stop_number AS orig_stop, o.arrived_at_utc AS orig_at_utc
  FROM s
  LEFT JOIN LATERAL (
    SELECT oj.stop_number, oj.arrived_at_utc FROM {schema}.journeys_sync oj
    WHERE oj.year = %s AND oj.campaign = %s AND oj.route_id = s.route_id
      AND oj.stop_number BETWEEN s.stop_number - 500 AND s.stop_number - 1
      AND oj.household_id = s.household_id
    ORDER BY oj.stop_number DESC LIMIT 1
  ) o ON true
  WHERE s.is_revisit
)
SELECT *,
  {hav_prev} AS in_km,
  coalesce({hav_next}, 0) AS out_km,
  coalesce({hav_skip}, 0) AS skip_km
FROM r ORDER BY route_id, stop_number
"""


def _hav_sql(a: str, b: str, c: str, d: str) -> str:
    return (
        f"2 * 6371 * asin(sqrt(power(sin(radians({c} - {a}) / 2), 2)"
        f" + cos(radians({a})) * cos(radians({c})) * power(sin(radians({d} - {b}) / 2), 2)))"
    )


REVISITS_SQL = REVISITS_SQL.replace("{hav_prev}", _hav_sql("plat", "plon", "lat", "lon")).replace(
    "{hav_next}", _hav_sql("lat", "lon", "nlat", "nlon")
).replace("{hav_skip}", _hav_sql("plat", "plon", "nlat", "nlon"))

IMMEDIATE_GAP = 2  # revisit within 2 stops of the original visit
EXPENSIVE_KM = 1.0  # detour at or above this counts as an expensive backtrack (Hilbert order: ~13 % of revisits at scale 1, ~0.2 % reach 5 km)


def classify_revisit(gap: int | None, detour_km: float) -> str:
    """Heuristic hypothesis label for a revisit (no cause column exists in the data)."""
    if detour_km >= EXPENSIVE_KM:
        return "vuelta_cara"
    if gap is not None and gap <= IMMEDIATE_GAP:
        return "reintento_inmediato"
    return "recuperacion_cercana"


def mount_map(app: FastAPI, run_sql: Callable[..., list[dict[str, Any]]]) -> None:
    def _schema() -> str:
        return _quote_ident(os.environ.get("NAVIDAD_SCHEMA", "public"))

    def _iso(value: Any) -> Any:
        return value.isoformat() if hasattr(value, "isoformat") else value

    @app.get("/api/route/options")
    def route_options() -> dict[str, Any]:
        schema = _schema()
        try:
            years = run_sql(OPTIONS_YEARS_SQL.format(schema=schema))
            cities = run_sql(OPTIONS_CITIES_SQL.format(schema=schema))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        y = years[0] if years else {}
        year_list = list(range(int(y.get("min_year") or 0), int(y.get("max_year") or -1) + 1))
        return {
            "years": year_list,
            "campaigns": sorted(CAMPAIGNS),
            "cities": [
                {**c, "lat": float(c["lat"]), "lon": float(c["lon"]), "households": int(c["households"])}
                for c in cities
            ],
        }

    @app.get("/api/route")
    def route(year: int, city: str, campaign: str = "nochebuena") -> dict[str, Any]:
        if campaign not in CAMPAIGNS:
            raise HTTPException(status_code=400, detail="Campaña no válida")
        schema = _schema()
        try:
            summary = run_sql(ROUTE_SUMMARY_SQL.format(schema=schema), (year, campaign, city))
            stops = run_sql(
                ROUTE_STOPS_SQL.format(schema=schema),
                (year, campaign, city, ROUTE_SAMPLE_PER_ROUTE),
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        routes = [
            {
                **r,
                "route_id": int(r["route_id"]),
                "stops": int(r["stops"]),
                "households": int(r["households"]),
                "revisits": int(r["revisits"]),
                "dwell_minutes": int(r["dwell_minutes"]),
                "first_at_utc": _iso(r["first_at_utc"]),
                "last_at_utc": _iso(r["last_at_utc"]),
            }
            for r in summary
        ]
        features = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [float(s["lon"]), float(s["lat"])]},
                "properties": {
                    "route_id": int(s["route_id"]),
                    "bringer": s["bringer"],
                    "stop_number": int(s["stop_number"]),
                    "arrived_at_utc": _iso(s["arrived_at_utc"]),
                    "dwell_minutes": int(s["dwell_minutes"]),
                    "is_revisit": bool(s["is_revisit"]),
                    "household_id": int(s["household_id"]),
                    "label": s["label"],
                    "household_size": int(s["household_size"]),
                    "timezone": s["timezone"],
                },
            }
            for s in stops
        ]
        return {
            "year": year,
            "campaign": campaign,
            "city": city,
            "routes": routes,
            "sample_per_route": ROUTE_SAMPLE_PER_ROUTE,
            "stops": {"type": "FeatureCollection", "features": features},
        }

    @app.get("/api/revisits")
    def revisits(year: int, city: str, campaign: str = "nochebuena") -> dict[str, Any]:
        if campaign not in CAMPAIGNS:
            raise HTTPException(status_code=400, detail="Campaña no válida")
        schema = _schema()
        try:
            rows = run_sql(
                REVISITS_SQL.format(schema=schema), (year, campaign, city, year, campaign)
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        items = []
        for r in rows:
            in_km = float(r["in_km"] or 0)
            detour = max(0.0, in_km + float(r["out_km"] or 0) - float(r["skip_km"] or 0))
            gap = int(r["stop_number"]) - int(r["orig_stop"]) if r.get("orig_stop") is not None else None
            items.append(
                {
                    "route_id": int(r["route_id"]),
                    "bringer": r["bringer"],
                    "stop_number": int(r["stop_number"]),
                    "orig_stop": int(r["orig_stop"]) if r.get("orig_stop") is not None else None,
                    "gap_stops": gap,
                    "household_id": int(r["household_id"]),
                    "label": r["label"],
                    "timezone": r["timezone"],
                    "arrived_at_utc": _iso(r["arrived_at_utc"]),
                    "orig_at_utc": _iso(r.get("orig_at_utc")),
                    "dwell_minutes": int(r["dwell_minutes"]),
                    "detour_km": round(detour, 2),
                    "in_km": round(in_km, 2),
                    "cause": classify_revisit(gap, detour),
                    "point": [float(r["lon"]), float(r["lat"])],
                    "prev": [float(r["plon"]), float(r["plat"])] if r.get("plat") is not None else None,
                    "next": [float(r["nlon"]), float(r["nlat"])] if r.get("nlat") is not None else None,
                }
            )
        items.sort(key=lambda x: x["detour_km"], reverse=True)
        return {
            "year": year,
            "campaign": campaign,
            "city": city,
            "expensive_km": EXPENSIVE_KM,
            "summary": {
                "revisits": len(items),
                "detour_km": round(sum(i["detour_km"] for i in items), 1),
                "extra_minutes": sum(i["dwell_minutes"] for i in items),
                "expensive": sum(1 for i in items if i["cause"] == "vuelta_cara"),
            },
            "revisits": items,
        }
