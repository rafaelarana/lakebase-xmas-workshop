---
name: navidad-hack-infra
description: >
  Provisions a Spain Christmas hackathon workbench (Papá Noel, Reyes Magos and
  regional gift-bringers such as Olentzero and Tió): synthetic historic
  households, people, preferences, and two-night journeys in Unity Catalog, a
  per-user Lakebase Autoscaling project with inbound synced tables, and a
  Spanish-language plumbing Databricks App wired to Lakebase SQL and a Unity
  Catalog AI Gateway model. Use when setting up the Navidad hackathon
  workbench or skipping Apps-to-Lakebase plumbing. Do not use for building
  maps, GIS routes, or customer segments after setup (that is the
  navidad-hack-build-assistant skill).
---

# Navidad hackathon workbench

Provisions a deterministic Spain Christmas dataset (Nochebuena + Reyes) in Unity Catalog, a per-user Lakebase Autoscaling project with inbound sync, and a plumbing Databricks App (Spanish UI) already wired to Postgres and a Unity Catalog AI Gateway model. Participants start at customisation, not Apps-to-Lakebase plumbing.

Skill root (`<skill_root>`): the folder holding this `SKILL.md`. Installed for Genie Code it is `/Workspace/Users/<user>/.assistant/skills/navidad-hack-infra` (user skill) or `/Workspace/.assistant/skills/navidad-hack-infra` (workspace skill); check which exists. Do not mix files into `lakebase_demo/`, `app.py`, or `resources/app.yml`.

## Interview (one turn max)

Collect catalog. Everything else has sensible defaults:

- Slug: `slugify(<current user email>)` from `scripts/plan.py` (lowercase alphanumeric only; e.g. `ana.garcia@…` → `anagarcia`)
  - UC schema: `navidad_<slug>`
  - Lakebase project id and app prefix: `navidad-<slug>` (hyphens; Lakebase project ids cannot contain `_`)
- Model: `system.ai.claude-opus-5`
- Scale: `1.0`, Seed: `20260106`

If the user says "set it up", "run it", "defaults", or similar — ask only:

> Default setup in catalog `<catalog>`? I'll create schema `navidad_<slug>`, Lakebase project `navidad-<slug>`, and app `navidad-<slug>-app`.

One "yes" = run all four pipeline steps end-to-end. No plan display, no function-body review, no per-step approval. Report progress inline as steps complete (`Running step N of 4: …`). Do not pause between steps.

If catalog is already known (from memory, this conversation, or workspace context), skip the question entirely and start executing.

Only pause for approval if:

- The user explicitly asks to review the plan first (then call `build_plan` / `format_plan` from `scripts/plan.py` and wait)
- A step fails and needs user input to resolve

Auth via `DATABRICKS_CONFIG_PROFILE`. Do not commit tokens or copy `~/.databrickscfg`.

Never create a catalog. Warehouse id is **not** required for UC write. If SQL is needed at all, list warehouses and pick a **serverless** warehouse first.

## How to execute (Genie Code)

Do **not** dump function bodies into chat. Import and call them.

### Running the functions

1. Run in a **Databricks notebook with a Spark session** (serverless compute preferred).
2. Make the **first cell** the dependency install, before any variables are defined (the restart wipes Python state):

   ```python
   %pip install psycopg2-binary "databricks-sdk==0.122.0"
   dbutils.library.restartPython()
   ```

   `deploy_app` needs `psycopg2` to grant the app's Postgres role SELECT. `provision_lakebase` and `deploy_app` need a Databricks SDK with the Lakebase Autoscaling `postgres` API; the SDK preinstalled on serverless can be too old (`'WorkspaceClient' object has no attribute 'postgres'`), so pin the same version as the app. Everything else ships with serverless.
3. Put the skill `scripts/` directory on `sys.path`, then **import and call** the functions. Use `WorkspaceClient()` with no profile when already in the workspace.

```python
import sys
sys.path.insert(0, "<skill_root>/scripts")
from generate_data import GenerateConfig
from write_uc import write_uc
from provision_lakebase import provision_lakebase
from deploy_app import deploy_app
from databricks.sdk import WorkspaceClient

write_uc(spark=spark, catalog=catalog, schema=schema, cfg=GenerateConfig(scale=scale))
provision_lakebase(catalog=catalog, schema=schema, project_id=project_id, client=WorkspaceClient())
```

Never `subprocess.run`, `%sh python scripts/write_uc.py`, or equivalent. If the action reviewer blocks a call: **stop**, name the step, ask only what is needed to unblock, then retry the **same** import/call. Never retry via a different route: no shell, no `subprocess`, no `%sh`, and no hand-rolling the same SDK calls inline.

## Hard rules

- Never create a catalog. If the catalog does not exist or the user cannot create a schema in it, stop with that message.
- Never skip ahead. Do not provision Lakebase if UC write failed. Do not deploy the app if inbound sync is not ready.
- On failure: stop. Name the function and last completed substep. Rerun only that step (functions are idempotent for the same prefix).
- Names must start with `navidad_` (UC schema) or `navidad-` (Lakebase project and Databricks App) so they cannot collide with `lakebase_demo_*` / `lakebase-demo-*` or other demo workbenches in the same workspace.
- Inbound sync only. No CDF outbound / reverse sync.
- Default Gateway model: `system.ai.claude-opus-5`.
- Prefer **serverless** Spark compute for `write_uc`. Prefer a **serverless SQL warehouse** if SQL is needed at all.

## Pipeline (never skip ahead)

Use schema = `navidad_<slug>`, project-id = `navidad-<slug>`, app name `navidad-<slug>-app`.

### 1. Generate (optional smoke)

Import `generate` from `scripts/generate_data.py`. Pure Python. Does not write Databricks.

```python
from generate_data import GenerateConfig, checksum, generate
ds = generate(GenerateConfig(scale=scale, seed=20260106))
print(checksum(ds), {k: len(getattr(ds, k)) for k in ("households", "people", "preferences", "journeys")})
```

Default counts at scale `1.0`: 500_000 households, 2_000_000 people, 2_000_000 preferences, ~4_000_000 journey stops over 5 campaign years, two nights each (Nochebuena + Reyes, plus ~0.1% revisits). Generation takes ~20 s of pure Python.

### 2. Write UC

Read `scripts/write_uc.py`, then call `write_uc(spark=spark, ...)`. Creates `{catalog}.{schema}` if needed. Overwrites four Delta tables with Spark DataFrames and enables CDF. **Do not** `INSERT … VALUES` in chunks. Does not create catalogs.

```python
from write_uc import write_uc
from generate_data import GenerateConfig
write_uc(spark=spark, catalog=catalog, schema=schema, cfg=GenerateConfig(scale=scale, seed=20260106))
```

Tables: `households`, `people`, `preferences`, `journeys`. Do not start Lakebase if this fails.

To regenerate one table only (e.g. new route order), pass `tables=("journeys",)` with the **same** scale and seed as the existing tables (checked against `households`; mismatch raises). Then refresh `journeys_sync` in Lakebase: SNAPSHOT/TRIGGERED synced tables do not pick up a Delta rewrite on their own.

### 3. Lakebase Autoscaling + inbound sync

Read `scripts/provision_lakebase.py`, then call `provision_lakebase(...)`. One Autoscaling project per prefix. Reuse if it already exists. Create four inbound synced tables if missing; wait until ready.

```python
from provision_lakebase import provision_lakebase
from databricks.sdk import WorkspaceClient
provision_lakebase(catalog=catalog, schema=schema, project_id=project_id, client=WorkspaceClient())
```

`sync_mode` defaults to `SNAPSHOT`: one full copy, no running pipeline (the cheap choice for workshops and demos). Pass `sync_mode="CONTINUOUS"` only when Delta changes must reach Lakebase within seconds (~80% of the running cost). The mode cannot be changed on an existing synced table; with SNAPSHOT, refresh `<table>_sync` after rewriting a Delta table.

Primary keys: households `household_id`; people `person_id`; preferences `person_id`; journeys `(year, campaign, route_id, stop_number)`. Do not deploy the app if sync is not done. On missing Lakebase entitlement, surface `Lakebase entitlement missing` — do not invent a fallback.

### 4. Copy template and deploy the App

Read `scripts/deploy_app.py`, then call `deploy_app(...)`. Copies `templates/app/` into a per-user folder, injects catalog/schema/Lakebase/model, creates or updates the Databricks App in place.

```python
import tempfile
from pathlib import Path
from deploy_app import deploy_app
from databricks.sdk import WorkspaceClient
skill_root = Path("<skill_root>")
deploy_app(
    prefix=prefix,
    catalog=catalog,
    schema=schema,
    project_id=project_id,
    model="system.ai.claude-opus-5",
    source_template=skill_root / "templates" / "app",
    dest_dir=Path(tempfile.mkdtemp()) / prefix,
    client=WorkspaceClient(),
    synced_ready=True,
)
```

The local `dest_dir` is a scratch copy. The app source the user edits afterwards is the workspace path printed in the plan (`/Workspace/Users/<user>/navidad-hackathon/<prefix>-app`). Never write into the skill folder itself.

App name is `navidad-<slug>-app`. Second run for the same slug must update in place, never create a second app. On missing Apps entitlement, surface `Apps entitlement missing`. `deploy_app` wakes the Lakebase endpoint before creating the app: a cold endpoint makes the app's automatic Postgres grant time out and leaves the app in a fatal ERROR state. If `deploy_app` raises `AppFatalError`, ask the user before deleting that app, then rerun `deploy_app` (it re-creates the app).

### 5. Handoff

Report using the handoff template at the end of this file. Take the URL from the `url` key returned by `deploy_app`; if it is empty, read it from `WorkspaceClient().apps.get(name=app_name).url`. Never guess or construct the URL.

## Optional: incident hack track (Filomena · DANA · cortes de calle)

Run **only** when the user or event organiser asks for the incident track ("Filomena", "DANA", "incidencias", "simular en rama", "pgRouting"). Run it after a successful pipeline, in the same notebook. It does not change steps 1–4.

```python
from provision_incidents import provision_incidents
from databricks.sdk import WorkspaceClient
report = provision_incidents(
    project_id=project_id, schema=schema, app_name=f"{prefix}-app", client=WorkspaceClient(),
    osm_cache_dir=f"/Volumes/{catalog}/{schema}/osm",  # optional: reuse downloads; create the volume first or pass None
)
```

What it does (idempotent, ~2–3 min):

1. `CREATE EXTENSION postgis, pgrouting`, then the native schema `navidad_geo` (`sql/incidents.sql`). Geometry never goes into `*_sync`.
2. For Madrid, Barcelona and Valencia it builds the road graph:
   - fetches the OpenStreetMap drive network from the public Overpass API, ~1 min per city;
   - splits OSM ways into pgRouting edges;
   - keeps each city's largest strongly connected component;
   - snaps every household to its nearest road vertex.
3. Loads three scenario presets with their zones **inactive** on production.
4. Grants the app's service principal access to `navidad_geo`, plus `CAN_MANAGE` on the Lakebase project so the app can create and delete simulation branches.

Madrid, Valencia and Barcelona ship pre-built in `data/<city>_osm.csv.gz` (ODbL notice inside; OSM extract 2026-11-12), so they load in seconds without the network. Pass `cities=("Valencia", "Barcelona")` to skip Madrid (Filomena). Any city without a file needs outbound HTTPS to `overpass-api.de` from serverless. If it is blocked, stop and say so. Do not invent another data source.

The UI and API come from `templates/incidents/` (`incidents.py` and `static/planner.html`). The **navidad-hack-build-assistant** installs them into the user's app (`references/hack-incidents.md`). Do not copy them into git `templates/app/`.

Road data © OpenStreetMap contributors (ODbL). The page already shows the attribution; keep it.

## Customisation

Edit generator parameters (`scale`, `seed`, or code in `scripts/generate_data.py`) and/or the **copied** app under the per-user folder. Column and grain changes must be reflected in `references/data-dictionary.md`. If you change a city's coordinates or add a city, regenerate `scripts/es_land.py` with `tools/build_land_masks.py` (dev-only; needs shapely and Natural Earth 10m land).

Do not edit git `templates/app/` unless improving the skeleton for everyone.

Rerun only the steps that changed:

- Generator / dictionary grain → `write_uc` then `provision_lakebase` then app if SQL changed.
- Copied app only → `deploy_app`.
- Model name only → redeploy with a new `model`.

Git’s template stays the known-good plumbing skeleton (FastAPI + React: health, sample SQL, model ping). No map, no Christmas product UI. The incident track ships separately in `templates/incidents/` (see above). The only seasonal touch in the skeleton is a decorative header (`static/navidad-fx.css` + `static/navidad-fx.js`: snowfall, trees + snowman, Santa hat on the logo, Santa's sleigh with reindeer, shooting stars); it is aria-hidden, click-through, respects `prefers-reduced-motion`, and has an on/off toggle. Turn it off by default with `<html data-navidad-fx="off">`. UI copy is Spanish (`lang="es"`, es-ES number formatting); skills and code stay in English.

## Handoff template

After a successful pipeline, reply with exactly this shape, filled in with real values. Lead with the clickable app link. No extra commentary.

```markdown
**Workbench ready:** [navidad-<slug>-app](<app url>)

Assets created:
- Unity Catalog schema: `<catalog>.navidad_<slug>` (tables: `households`, `people`, `preferences`, `journeys`)
- Lakebase project: `navidad-<slug>` (synced tables: `navidad_<slug>.households_sync`, `people_sync`, `preferences_sync`, `journeys_sync`)
- Databricks App: `navidad-<slug>-app`
- App source: `/Workspace/Users/<user>/navidad-hackathon/navidad-<slug>-app`

Next: use the `navidad-hack-build-assistant` skill to build maps, routes, or segments.
```
