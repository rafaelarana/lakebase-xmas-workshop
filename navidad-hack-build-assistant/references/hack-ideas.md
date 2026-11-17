# Suggested hack tracks (agent)

Offer these as **suggested tracks**, not a fixed menu. After **data orientation** (latest campaign year, both nights, city sizes, route semantics), select a concise, varied subset. Do not claim these are the only hackathon tracks.

The seed data has **two nights** per campaign year. Nochebuena is **one regional bringer per city route** (Papá Noel, Olentzero, Tió, Apalpador). Reyes is **already a fleet of three** (Melchor, Gaspar, Baltasar), each owning a fixed 120° sector of every city. Bigger fleets (more pajes, more camels) are a derived what-if, not a column in `journeys_sync`.

## Suggestions grounded in the supplied data

- **Reyes fleet rebalancing** — the three Kings' sectors are fixed and coastal cities leave one King short of homes. Reassign households to minimise the slowest King's night, then try more pajes (helpers). Include a fixed paje overhead per helper and a minimum useful workload so the optimum cannot become one paje per house.
- **Route optimisation and maps** — compare the historic stop order (a Hilbert space-filling curve: compact, but blind to streets and closures) with pgRouting street-aware routes for one city and night, and visualise the change.
- **Noche de Reyes operations centre** — route progress, ETA, delayed Kings, dwell-time hotspots, revisits and workload balance, with a time slider over the night in local time.
- **Island logistics** — the Balearics and Canaries need a hop by sea or air, and the Canaries run one hour behind. Plan when each island route can start and whether the Kings make it before dawn.
- **Forgotten-present detective** — locate revisits, quantify expensive backtracks, and explain likely operational causes.
- **Carbón planner** — test naughty/nice thresholds for coal, show who moves between categories, and estimate how much coal each King carries.
- **Regional traditions map** — where Olentzero, Tió, Apalpador and Papá Noel cover Nochebuena, and which households get both nights (double the gift logistics).
- **Gift logistics** — estimate gift mix and load per route from household size, ages and `gift_affinity`.
- **Letters to the Kings** — use `favourite_king` to see whether each King's fan base lives in his own sector; propose a swap that delights more children.
- **Shoes, balconies and chimneys** — use `window_policy` (`zapatos`, `balcon`, …) to sequence stops and flag difficult deliveries.
- **Camel and reindeer catering** — aggregate `dietary_notes` by route or city to provision snacks safely (turrón has `frutos_secos`).
- **City growth and capacity forecasting** — use `first_seen_year` to estimate future workload by city.
- **Incidencias en rama** — a Filomena-style storm in Madrid, a DANA in Valencia or street closures in Barcelona: simulate on a Lakebase branch, red zones on the map, pgRouting detours, pickup points and helpers, then apply the plan (`hack-incidents.md`; needs the infra incident step).
- **Centro de control en directo** — the whole night of deliveries streaming into Lakebase (Zerobus → Spark → Lakebase sink), exceptions on the map and an agent that proposes actions and waits for confirmation; extend it with SLA alerts, heatmaps or batch review (`hack-control-center.md`; needs notebooks 04 + 05).
- **Buscador** — Lakebase Search (BM25) over families and duende/paje crews: the history of a household on the map, or the path a crew followed; extend it with semantic search or filters (`hack-search.md`; needs Search enabled + notebook 08).
- **AI briefing assistant** — explain a route, segment or operational problem in Spanish from aggregate data using the existing Gateway.

## Reyes fleet model

Use Lakebase for the model inputs and output tables. Start with:

```text
route_time =
  travel_time
  + sum(dwell_minutes)
  + fixed_paje_overhead_per_helper
```

Optimise for **makespan** (the maximum route time across Kings and pajes), not only total distance. Let the user vary:

- number of pajes per King;
- fixed paje overhead;
- night length (01:00 → dawn, local time; Canaries one hour later in UTC);
- minimum households per helper or maximum fleet size;
- whether city boundaries or King sectors are hard or soft constraints.

Persist assignments and scenario results in new Postgres tables such as `{schema}.reyes_scenarios` and `{schema}.reyes_route_assignments`. Read `*_sync` as the immutable base.

## Opening response

Once workbench details and a data-orientation summary are available, ask what the user wants to build. Then say:

> Suggested tracks include:

Show 6–8 varied ideas. Keep each to one line. **Suggest concrete options** for any follow-up choice (cost function, segments, KPIs) rather than open-ended questions. Rotate remaining ideas rather than dumping the catalogue.
