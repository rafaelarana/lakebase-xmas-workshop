# Route visualisation (agent)

Explore → Interpret → Build. Delivery surface is the **app**. SQL/PostGIS is behind the scenes.

`route_id` is one (campaign, city, bringer) sub-tour. On Reyes night every city has three routes, one per King sector; on Nochebuena one route per regional bringer. Bigger fleets are a *derived* model (`hack-ideas.md`), not in the seed data. Confirm cardinalities before asking “how many pajes?”.

## Explore

Show data shape in chat first (latest campaign year, Reyes night by default). Then add a **simple map in the app** for the chosen area: household points, optional historic path overlay, basic counts. One city, one night, latest year is enough for v1.

**Suggest, don't ask open-ended:**

- Area: “Start with Madrid (largest), a coastal city like Valencia, or Santa Cruz de Tenerife (island + other time zone)?”
- Night: “Reyes (three Kings per city) or Nochebuena (regional bringers — try Bilbao for Olentzero)?”
- Map highlight: “Colour by King, by gift volume, or by naughty/nice mix?”
- Paths: “Overlay the three Kings' routes so we can spot long hops, unbalanced sectors and revisits?”

**Basemap (no API key by default):**

| Provider | API key? | Notes |
| --- | --- | --- |
| OpenStreetMap | No | Default. `https://tile.openstreetmap.org/{z}/{x}/{y}.png`, attribution `© OpenStreetMap contributors`. Mute with CSS: `.muted-tiles { filter: saturate(0.3) brightness(1.05) contrast(0.9); }` |
| IGN España — Mapa base | No | Spanish national basemap, Spain only. WMTS `https://www.ign.es/wmts/ign-base?layer=IGNBaseTodo&style=default&tilematrixset=GoogleMapsCompatible&Service=WMTS&Request=GetTile&Version=1.0.0&Format=image/jpeg&TileMatrix={z}&TileCol={x}&TileRow={y}`. Attribution `© Instituto Geográfico Nacional de España (CC BY 4.0)`. Offer as the “local” look; same muted CSS works |
| CartoDB Positron / Dark Matter | Yes since ~2024 | Cleaner overlay look. Offer CARTO free-tier key (https://carto.com/basemaps/apikey/) only as an upgrade |
| Stadia / Stamen / Mapbox | Yes | Do not use without a key |

Default tiles: **OSM + muted CSS**. Layout: **dashboard** (title, KPIs, map in a panel), not full-bleed.

**Framing Spain:**

- Peninsula + Balearics view: centre `[40.2, -3.0]`, zoom 6. A single city: zoom 12 on the city centre.
- The Canaries are ~1,700 km south-west. Never fit bounds over all of Spain at once (the map becomes ocean). Give a **Península / Canarias** toggle (Canarias: centre `[28.3, -15.8]`, zoom 8), or a small inset map in the corner, as Spanish maps traditionally do.
- Times in the UI: convert `arrived_at_utc` with `Intl.DateTimeFormat("es-ES", { timeZone: household.timezone, ... })`. Label Canary times as “hora canaria”.

All map labels, legends, tooltips and KPI names in **Spanish** (`Hogares`, `Paradas`, `Rey Melchor`, `Revisitas`). Map bringer codes to display names: `papa_noel` → Papá Noel, `olentzero` → Olentzero, `tio` → Tió de Nadal, `apalpador` → Apalpador, `melchor`/`gaspar`/`baltasar` → Melchor/Gaspar/Baltasar. Default King colours: Melchor gold, Gaspar crimson, Baltasar royal blue (state them; let the user change).

Frontend path: offer “I'll build v1, you refine” vs “describe your vision” (`SKILL.md`).

## Interpret

With the map in front of them: “What stands out? A King whose sector is half sea? Where the Hilbert curve jumps between blocks? Revisit spikes and costly returns (≥ 1 km)?”

Then suggest an objective, e.g.:

- Minimise total distance per King, keeping each under a stop cap
- Minimise the longest single route (makespan) so all three Kings finish before dawn
- Balanced workload across Kings and pajes, with a fixed paje overhead so one-helper-per-house cannot win

## Build

Minimal first: one city, latest year, Reyes night, one King (historic `route_id` path vs a re-ordered path). Show it. Then scale.

### PostGIS

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
ST_SetSRID(ST_MakePoint(h.lon, h.lat), 4326)  -- lon first
```

For metric distances use `::geography`, or project to ETRS89 / UTM 30N (`ST_Transform(geom, 25830)`) for the peninsula. Use UTM 28N (`32628`) for the Canaries; never one projection for both.

Do not `ALTER` `households_sync`. Do not use warehouse `st_*` in the app's Postgres client.

### GeoJSON, not a dump

One `year`, one `campaign`, one `city` or `route_id`, `LIMIT` (e.g. 2000). Check `references/app-endpoints.md` before adding `/api/route`.

```sql
SELECT j.stop_number, j.arrived_at_utc, j.dwell_minutes, j.is_revisit, j.bringer,
       h.household_id, h.label, h.city, h.timezone, h.lat, h.lon,
       ST_AsGeoJSON(ST_SetSRID(ST_MakePoint(h.lon, h.lat), 4326)) AS point_geojson
FROM {schema}.journeys_sync j
JOIN {schema}.households_sync h ON h.household_id = j.household_id
WHERE j.year = %s AND j.campaign = %s AND j.route_id = %s
ORDER BY j.stop_number
LIMIT 2000
```

Route lookup for a city: `SELECT DISTINCT j.route_id, j.bringer FROM {schema}.journeys_sync j JOIN {schema}.households_sync h USING (household_id) WHERE j.year = %s AND j.campaign = %s AND h.city = %s`. Persist it as a small `{schema}.routes` table if the UI needs it often.

Nearby: filter city/bbox first, then `ST_DWithin(...::geography, ..., metres)`.

Do not assume `pgRouting`. Style `is_revisit`. Build new tables on the dev branch and GRANT after each one (`references/dev-branch-workflow.md`, `references/lakebase-sql.md`). After the user approves the migration, deploy (`references/app-deploy.md`); the user opens the URL.
