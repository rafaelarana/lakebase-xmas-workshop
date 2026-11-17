# Existing app endpoints (agent)

The infra skeleton copies `templates/app/` to `/Workspace/Users/<user>/navidad-hackathon/<app>/`. **Read that live `app.py` before adding routes.** A prior hack session may already have extra endpoints; do not duplicate or overwrite them.

## Infra skeleton (always present on first deploy)

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/health` | `{"ok": true}` |
| GET | `/` | Serves `static/index.html` |
| GET | `/static/*` | Static assets (`navidad-fx.css`, `navidad-fx.js`, `databricks-symbol.svg`, anything you add) |
| GET | `/api/sample-households` | `SELECT city, autonomous_community, count(*)` from `{NAVIDAD_SCHEMA}.households_sync` |
| POST | `/api/model-ping` | One Gateway sentence (in Spanish) via `/ai-gateway/mlflow/v1/chat/completions` |

Keep `/health` working. Reuse `execute_sql_from_pg`, `_quote_ident`, `NAVIDAD_SCHEMA`, and `complete_from_gateway`. The UI (`static/index.html`) is Spanish (`lang="es"`); keep new copy Spanish.

When rewriting `static/index.html`, keep the Christmas header: the `/static/navidad-fx.css` link, the `<header data-navidad-header>` with its `[data-navidad-logo]`, and the `/static/navidad-fx.js` script. Do not reimplement the effects in React. Add `?fx=rapido` to the app URL to see Santa fly within ~2 s.

## Incident track (after installing `templates/incidents/`, see `hack-incidents.md`)

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/planner` | Planificador: parameters + what-if scenarios on a branch (`static/planner.html`; `/incidencias` redirects) |
| GET / POST | `/api/plan/options` · `/api/plan` | Cities/years · read-only night plan without a scenario `{city, campaign, year?, helpers, boost}` (no branch) |
| POST | `/api/sim/{branch}/params` | Re-plan an existing simulation with `{helpers, boost}` (apply_params + suggest + map, ~3 s) |
| GET | `/api/incidents/scenarios` | Presets + zone GeoJSON from production (zones inactive) |
| GET / POST | `/api/sim` | List simulation branches / start `{scenario}` on a new `sim-*` branch |
| GET / DELETE | `/api/sim/{branch}` | Phases, KPIs, suggestions, GeoJSON / delete the branch |
| POST | `/api/sim/{branch}/minute` | Replay: activate zones up to `{minute}` and re-simulate on the same branch |
| POST | `/api/sim/{branch}/apply` | Copy the branch's suggestions into production `navidad_geo.applied_plan` |

## Often added during a hack (check before creating)

| Method | Path | Typical use |
| --- | --- | --- |
| GET | `/api/route` | GeoJSON stops / path for `year` + `campaign` + `route_id` or `city` |
| GET | `/api/route/options` | Distinct years / campaigns / cities / routes (bringers) for UI filters |
| GET | `/api/nearby` | Households near lon/lat |
| GET | `/api/segments` | Segment counts |
| GET | `/api/segment-members` | Sample members, always `LIMIT` |

If these already exist in the copied app, extend them. Do not ship a second conflicting handler.
