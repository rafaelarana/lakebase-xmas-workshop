# Incident track: Filomena · DANA · cortes de calle (agent)

**Story.** An incident hits a city on a gift night. Which stops are at risk, how late does the night run, and how do we reroute? Test it on a **Lakebase branch**: a copy-on-write copy of production with its own compute, ready in seconds. Then let **pgRouting** suggest the plan. Production stays untouched until the user clicks "Aplicar plan".

| Preset | City · night | Red zone(s) | Effect |
| --- | --- | --- | --- |
| `filomena_madrid` | Madrid · Reyes | Whole city + heavy snow wedge (north/west, the Kings' sectors) + M-30 tunnels | Streets ×2.5–5 slower, main roads ×1.5–2.5, dwell ×1.2–1.5; tunnels closed (main roads only) |
| `dana_valencia` | Valencia · Nochebuena | Flood corridor south (barranco, l'Horta Sud) + a cut bridge + muddy surroundings | Inside the flood: closed; around it ×2–4 slower |
| `cortes_barcelona` | Barcelona · Nochebuena | 5 street closures appearing at minutes 0–40 | Closed; replay re-routes as they appear |

Tone: these are real events with fatalities. App copy says **"inspirados en"**, gives no casualty figures and shows no damage imagery. Barcelona copy stays neutral ("cortes de calle").

## Prerequisite (infra)

The incident step of **navidad-hack-infra** must have run, or the schema is missing. Check:

```sql
SELECT city, edges, vertices FROM navidad_geo.osm_meta;        -- Madrid, Barcelona, Valencia
SELECT scenario_id FROM navidad_geo.scenario;                   -- 3 presets
SELECT count(*) FILTER (WHERE active) FROM navidad_geo.zone;    -- 0 on production
```

If `navidad_geo` does not exist, tell the user the organiser (or the infra skill) must run `provision_incidents`. Do not build the graph from this skill.

## Install the reference module into the user's app

The infra skill ships a working module at `<infra_skill_root>/templates/incidents/` (installed next to this skill under `.assistant/skills/navidad-hack-infra/`):

1. Copy `incidents.py` into the app source folder. Copy `static/planner.html` into the app's `static/`.
2. In the app's `app.py`, inside `create_app()`, before `return app`:

   ```python
   from incidents import mount_incidents
   mount_incidents(app, STATIC_DIR)
   ```

   The app must already serve `/static` (`app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")`) for the Christmas header assets.
3. Run `add_nav(dest)` (provision_incidents) so the left rail shows **Planificador** (`/planner`; the old `/incidencias` redirects).
4. Check the app's `app.yaml` env has `NAVIDAD_PROJECT_ID` and `NAVIDAD_SCHEMA` (the infra deploy sets both). Deploy in place (`app-deploy.md`).
5. Open `/planner`, pick DANA, click **Simular en rama**. The first run takes ~15–40 s: branch ~3 s, compute, then routing.

Endpoints added (see `app-endpoints.md`): `GET /planner`, `GET /api/incidents/scenarios`, `GET|POST /api/sim` (`{scenario, helpers, boost}`), `GET|DELETE /api/sim/{branch}`, `POST /api/sim/{branch}/minute`, `POST /api/sim/{branch}/params` (re-plan without re-simulating), `POST /api/sim/{branch}/apply`.

**Planner flow:** section 1 = city (all 21) + night; section 2 = parameters; section 3 = what-if scenario, **optional** (only the city's own scenarios are selectable — road networks exist for Madrid, Valencia, Barcelona). Without a scenario «Simular» runs `POST /api/plan {city, campaign, helpers, boost}`: a read-only night plan on `production` (no branch) where no route is late and the parameters move the end of the night (`load = (1 − tf + tf/boost) / (1 + 0.6·helpers)`, tf = 0.62 measured with pgRouting); sliders re-plan live. `GET /api/plan/options` lists cities and years.

**Planner parameters** (left panel, section 2): extra helpers per route (duendes in Nochebuena, pajes in Reyes; each adds 60 % of a bringer's capacity) and a reindeer/camel boost (×1.0–1.5, speeds up only the travel share of the night, measured per simulation). `navidad_geo.apply_params(helpers, boost)` runs after `simulate` and before `suggest`, so late stops and suggested reinforcements reflect them; the page shows late stops with and without parameters. Section 2 is the what-if scenario (Filomena Madrid · DANA Valencia · cortes Barcelona).

## Explore → Interpret → Build

1. **Explore.** Show the preset zones over the existing route map (red polygons from `/api/incidents/scenarios`). Ask which night, city and incident tells their story best.
2. **Interpret.** Run one simulation and read the KPIs together:
   - stops in the zone, households cut off, late stops and the per-King load factor;
   - "What surprises you?" Typical answers: legs cross the zone because the historic stop order (a Hilbert curve) ignores streets and closures; the unbalanced Kings in Filomena.
3. **Build.** Pick one improvement and ship it (ideas below). Every change runs on the branch first.

## How the simulation works (SQL in `navidad_geo`)

- `set_minute(scenario, minute)` activates zones; `refresh_costs(city)` rewrites `ways.cost`. Closed = `-1`, which pgRouting skips. Otherwise cost = base × the largest zone factor.
- `simulate(schema, scenario, p_max_legs := 3000)` does the routing:
  - legs = consecutive stops; affected = within 300 m of an active zone;
  - routes a **deterministic sample** with `pgr_dijkstraCost` (base vs incident);
  - per-route `load_factor`, then `late_stops = stops × (1 − 1/load_factor)`.
- `suggest(schema, scenario)` produces the plan:
  - detour paths (`pgr_dijkstra`);
  - pickup points (nearest reachable vertex, clustered ~800 m);
  - helpers per route (`ceil(load_factor) − 1`), with the late tail handed over and a children-first KPI;
  - `pgr_TSP` over ≤120 affected stops of the worst route.
- `sim_geojson(schema)` returns the map payload.

Routes are huge: a Madrid Reyes route has ~44K stops. **Never** run TSP on a whole route, and never send all legs to the browser. Work on affected legs, a sample, or one cluster.

## Branch rules

- Simulations **always** run on a child branch named `sim-<scenario>-<stamp>`, with TTL 24 h and at most 3 at a time. The module deletes the oldest.
- Never activate zones or update `navidad_geo.ways` on **production**.
- The only production write is `navidad_geo.applied_plan`, via "Aplicar plan".
- On the branch, synced tables are **writable copies**. You may write derived tables there for what-ifs, but still never write `*_sync` on production.
- To start fresh, discard the branch and simulate again. Reset-from-parent is UI-only.
- Each branch has its own compute (0.5–8 CU, scales to zero after 60 s idle). Discard branches when done.

## Extension ideas (pick one, ship small)

- **Draw your own incident.** Click a centre and radius on the map → `INSERT INTO navidad_geo.zone (…, scenario_id = 'custom_<user>')` on the branch → re-simulate.
- **Live night.** Auto-advance `cortes_barcelona` every 10 s with a time slider, and show the KPI deltas per minute.
- **Better re-sequencing.** Replace the Hilbert-curve order inside the zone with `pgr_TSP` per cluster (DBSCAN) over street costs and measure total time saved. The current order is compact in straight-line distance but ignores streets and closures, so the gain comes from the road network (DANA: TSP −46 %).
- **Pajes reales as a fleet.** Turn the helpers suggestion into real assignments:
  - each helper starts at the nearest main-road vertex to its tail stops;
  - balance makespan, with a fixed overhead per paje.
- **Isochrones.** `pgr_drivingDistance` from a King's position at minute X: how far does he get before dawn, with and without snow? Draw it as a concave hull.
- **A/B what-ifs.** Two branches in parallel, e.g. "only closures" vs "closures + helpers". Compare the KPIs side by side.
- **Spanish briefing.** Send the KPI JSON to the Gateway: "Resume en 5 frases para el centro de operaciones".

Copy stays Spanish (es-ES). Keep the OpenStreetMap attribution and the "inspirados en" note visible.
