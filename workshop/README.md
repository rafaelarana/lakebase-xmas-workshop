# Lakebase Xmas Workshop · Lakebase for agentic applications

Build the "Xmas Workshop" from zero, **with nothing but a workspace**: Santa Claus and the Three Kings as a delivery fleet across 21 Spanish cities. It includes:

- data in **Unity Catalog**;
- an operational database in **Lakebase** (serverless Postgres) with *synced tables*;
- a **Databricks App**;
- an **incidents** track (Storm Filomena-style snowfall in Madrid, DANA flash flood in Valencia, street closures in Barcelona). Incidents are simulated on **Lakebase branches** with **PostGIS + pgRouting**.

> **Language note.** This English edition ships the English deck and README. The notebooks' explanatory text and the app UI are still in Spanish (Spain-themed data); code, SQL and identifiers are in English.

## Requirements

- A Databricks workspace (AWS or Azure) with **Lakebase** and **Databricks Apps** enabled and **serverless compute**.
- Permission to create a schema in an existing catalog (`USE CATALOG` + `CREATE SCHEMA`).
- Nothing else: no CLI, no local install.

## Deck

`workshop/deck/lakebase-agents-en.html`: open it in a browser (works offline).
- **Keys:** ← → steps · 1–6 tabs · N notes · T light/dark background · G Grinch · F full screen.
- **Buttons:** "❄ Effects" (snow, Santa and reindeer; off by default) and "Architecture" (step by step through iterations).

## Path A · notebooks (recommended)

1. Bring the content into your workspace, in one of two ways:
   - **Git folder:** *Workspace → Create → Git folder* with the repo URL.
   - **Zip:** *Workspace → (your folder) → Import → File*. Upload `lakebase-xmas-workshop.zip` (from the Releases page) and keep its folder structure.
2. Open `workshop/notebooks/00_workbench`, type your catalog in the `catalog` widget and click **Run all**. About **5 minutes** at scale 0.1 (measured: data 77 s, Lakebase 31 s, app 143 s, incidents 9 s).
3. When it finishes, the last cell shows the links to the app and to `/planner`.

| Notebook | What it does |
|---|---|
| `00_workbench` | Data → Lakebase (synced tables) → app (with Incidents) → road network and scenarios → control center ready (`navidad_live` + ▶ / ■ job) |
| `01_rama_whatif` | Branching step by step without the app: create a branch, simulate, compare with production, delete it |
| `02_instalar_skills` | Copies the skills to Genie Code (`.assistant/skills`) for agentic development |
| `03_grinch` | **Optional.** The Grinch deletes 80 % of the letters to the Kings: snapshot, branch from the safe point in time (PITR) and recovery without stopping the app; optional full restore (~1 min) |
| `04_tiempo_real_stream` | **Control center (facilitator).** Prepares `navidad_live` (Lakebase), the bronze table and the Zerobus identity, and runs Spark Structured Streaming on **serverless** (`AvailableNow` in a loop) → **Lakebase sink** (`format("postgresql")` + `upsertkey`). Start it first |
| `05_tiempo_real_productor` | **Control center (facilitator).** Replays the whole night across Spain in fast motion (elves / pages, ~1.5 % exceptions) and sends it through **Zerobus**; `destino = lakebase_directo` writes straight to Lakebase (rehearsal). Open the app at `/control` |
| `06_tiempo_real_sin_broker` | **Comparison (facilitator).** Serverless Spark without Zerobus or Delta: `rate` source (~350 events/s) → join with the plan → Lakebase sink in an `AvailableNow` loop; measures latency (≈ 5.7 s p50) |
| `07_replica_lectura` | **Bonus · Read replica.** Creates a `READ_ONLY` endpoint (`replica`) on the `production` branch (~2 s, same storage, no data copy), checks that it rejects writes and measures lag (p50 ≈ 3 ms on the reference workspace). `/control` detects it and reads from it ("Reads: replica"); ingestion, agent and ▶/■ keep writing to the primary. `accion = borrar` removes it |
| `08_busqueda` | **Bonus · Search.** Requires **Lakebase Search** enabled on the project (Settings → Lakebase Search; irreversible, restarts compute). Indexes 500,000 families and 364 elf/page crews in `navidad_search.doc` (BM25 + GIN for prefixes, ~1 min) and runs test queries (BM25 ≈ 3 ms). In the app: search bar at the top ("/"), results on the "Route map" |
| `99_limpieza` | Deletes the app, the Lakebase project (**purged**, no 7-day trash, so it can be recreated with the same name) and the schema. Type `BORRAR` to confirm |

## Real-time control center (block 3)

Attendees **review the architecture and how data is written to Lakebase** ("Technical detail" tab in block 3); the facilitator starts the night with **▶ Start night / ■ Stop** at `/control` (job `navidad-live-<slug>` = `04` + `05` in parallel, created the first time `04` runs) or runs `04` and `05` by hand.

```
05 producer ──Zerobus──► Delta bronze ──04 Spark (serverless)──► Lakebase navidad_live ──► app /control ──► agent (confirm)
```

Three ways to write to Lakebase, one table per writer: **synced tables** (`*_sync`), **Lakebase sink** (live state: `delivery_status`, `helper_position`, `exception_event`) and **direct writes** from the app and the agent (`exception_state`, `agent_review`, `agent_action`).

Measured on the reference workspace (full Christmas Eve, 318,170 stops, 21 cities, 112 elves): Zerobus acks in ~0.2 s and writes to Delta every 5 s; the sink writes each batch in 0.1–0.2 s; **event → Lakebase p50 ≈ 14 s, p95 ≈ 23 s**, all serverless (the fixed cost of each `AvailableNow` run is ~3–8 s; on classic compute with `processingTime` it would be lower). The agent (Haiku) reviews an incident in ~7–10 s.

Comparison (reference workspace, serverless, 350 events/s): **direct write** to Lakebase p50 0.02 s · **Spark without broker or Delta** (`06_tiempo_real_sin_broker`, `rate` source → sink) p50 5.7 s / p95 8.0 s · **Zerobus → Delta → Spark** p50 ≈ 14 s / p95 ≈ 23 s. Future: classic compute with `processingTime` or Real-Time Mode (the reference workspace is serverless-only). Requirements: Zerobus in the workspace region and permission to create a service principal (`04` does it).

## Databricks Free Edition

Validated from zero on a Free Edition workspace (all notebooks, then cleanup); `00` takes about 5 min at scale 0.1.

- **Widget `edicion` = Free Edition:** applies everything below automatically (model, Lakebase project limits).
- **Catalog:** `workspace`.
- **Scale:** `scale` = **0.1**. The Free Edition daily compute quota makes 0.75 risky.
- **Model:** `model` = **`databricks-gpt-oss-120b`** (or `databricks-meta-llama-3-3-70b-instruct`). Free Edition has no `system.ai.*` models in AI Gateway; the app calls the serving endpoint directly.
- **Lakebase:**
  - 1 project per account, so no second workbench with a suffix;
  - compute is capped at 1 CU;
  - minimum scale-to-zero of 300 s (the notebook sets it).
- **Branches, snapshots and PITR:** work the same (branch ~3 s, snapshot < 1 s, restore ~3 s).
- **Apps:**
  - up to 3 per account;
  - stop automatically after 24 h (restart from *Compute → Apps*);
  - the model ping uses the endpoint you set.
- **Restricted outbound internet:** the Madrid, Valencia and Barcelona road networks are preloaded (no Overpass needed).

## Path B · SQL

Lakebase SQL editor (project `navidad-<your_slug>`). Replace `navidad_<your_slug>` with your schema.

- `sql/01_explorar.sql`: the two nights, the cities, the Kings as a fleet.
- `sql/02_postgis_pgrouting.sql`: KNN, road travel time with `pgr_dijkstraCost`, scenarios.
- `sql/03_rama_whatif.sql`: **only on a branch**. Activates the DANA, simulates and suggests. Then compare with `production`.

## Path C · DAB or Genie Code

- **DAB:** `databricks bundle deploy/run` with `workshop/databricks.yml`. Same notebook, as a serverless job.
- **Genie Code:** run `02_instalar_skills` and ask Genie Code: *"Help me build on top of the Xmas Workshop"*. The `navidad-hack-build-assistant` skill suggests tracks (routes, segmentation, incidents) and builds on Lakebase.

## Cost and cleanup

- By default, synced tables run in **SNAPSHOT** mode: one copy, no continuous pipeline. It's the cheap option.
- Lakebase compute scales to zero after 60 s idle.
- Simulation branches expire after 24 h, and the app keeps at most 3 at a time.
- When you're done, run `99_limpieza`.
- Pricing: [public Lakebase pricing page](https://www.databricks.com/product/pricing/lakebase).

## Notes

- **Synthetic** data: random names, coordinates jittered per city.
- Scenarios **inspired by** real events (2024 DANA, street closures). Geometry is illustrative.
- Road network © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), ODbL licence (files `navidad-hack-infra/data/*_osm.csv.gz`).
- Coastlines and land: Natural Earth (public domain).
