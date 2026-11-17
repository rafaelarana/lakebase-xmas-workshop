# Real-time control center («Centro de control») — hack playbook (agent)

**Story.** A gift night in progress, all of Spain: deliveries arrive as events, the map fills in seconds, exceptions turn up, and an agent proposes what to do and **waits for a person to confirm**.

## What exists (built by `navidad-hack-infra`)

```
05_tiempo_real_productor ──Zerobus (gRPC)──► UC Delta  <schema>.delivery_events_bronze
                                                │
                    04_tiempo_real_stream  (Spark Structured Streaming, serverless, AvailableNow loop)
                                                │  Lakebase sink: format("postgresql") + upsertkey
                                                ▼
Lakebase production · schema navidad_live
  sink writes:      delivery_status (sim_run_id, household_id) · helper_position (sim_run_id, helper_id) · exception_event (event_id)
  producer writes:  sim_run · run_plan
  app/agent write:  exception_state · agent_review · agent_action
  views:            v_exception (state = agent decision, or 'resuelta' after a retry delivery) · v_city_progress · v_household
                                                │
                                   App /control (polls /api/control/state every 1.5 s with an updated_at cursor)
```

- `updated_at` is set by a trigger, so **event → Lakebase latency = `updated_at − sent_ts`**. The app shows p50/p95 of the last 30 s.
- **▶ Iniciar noche / ■ Parar** in `/control` start and cancel the serverless job `navidad-live-<slug>` (tasks: 04 stream + 05 producer in parallel, one retry each; the producer waits for the stream). Notebook 04 creates/updates the job (`ensure_live_job`) and gives the app's service principal CAN_MANAGE_RUN; runs execute as the job owner. Endpoints: `GET /api/control/job`, `POST /api/control/job/start {campaign, minutes, destino}`, `POST /api/control/job/stop`.
- Producer `destino = lakebase_directo` writes the same three sink tables straight into Lakebase (psycopg). Use it to work on the UI without Zerobus/streaming.
- An exception's state: a decision in the app (`resuelta` / `descartada`) wins; otherwise a later delivery of the same household (retry) resolves it. Reviews and confirmations are refused once it is resolved.
- Agent: `POST /api/control/exceptions/{id}/review`. It gathers context with SQL (household, nearest helpers by haversine, similar open exceptions, active `navidad_geo.zone`), asks the model (`NAVIDAD_AGENT_MODEL` if set — e.g. Haiku for ~10 s reviews — else `NAVIDAD_MODEL`) for 2–3 actions from a **fixed catalogue** (`reasignar_ayudante`, `reintentar`, `punto_recogida`, `avisar_familia`, `escalar`, `cerrar`), validates them, stores them as `propuesta`. Rules take over if the model fails. Confirm/reject: `POST /api/control/actions/{id}/confirm|reject`.

## Rules

- **One writer per table.** Never write `delivery_status`, `helper_position` or `exception_event` from the app or the agent; write decisions to `exception_state` / `agent_*` and read through `v_exception`.
- The agent never changes anything without a confirmed action. New action types go into the catalogue (`ACTIONS` + `_action` validation + `RULES`) — the model can't invent them.
- New Lakebase objects follow the branch-first workflow (`dev-branch-workflow.md`). The `navidad_live` views use explicit column lists and `CREATE OR REPLACE`: add new columns **at the end** so grants and views built on top survive a re-run of `provision_live` (notebook 04, cell 1).
- On serverless the sink only supports `Trigger.AvailableNow()`; keep the loop. Classic compute allows `processingTime`; `realTime` needs a Kafka/Kinesis/Event Hubs source.
- Keep `spark.sql.shuffle.partitions` low (8) for the stateful queries; the count is fixed per checkpoint (bump `CHK_VERSION` to change it).
- Never send all stops to the browser. `/api/control/state` returns only rows changed since the cursor (capped), helpers (≤ a few hundred), and the 200 newest open exceptions.

## Read replica (bonus)

- `07_replica_lectura` creates a `READ_ONLY` endpoint `replica` on `production` (`provision_live.ensure_read_replica`): same storage, own compute/autoscaling, measured lag p50 ≈ 3 ms.
- The app's `ReadRouter` sends **reads** (`/api/control/state`, `/metrics`, `/zones`, exception detail, the agent's context) to the replica — `LAKEBASE_READ_ENDPOINT` or the branch's first `READ_ONLY` endpoint, re-checked every minute — and falls back to the primary for 30 s if it fails. **Writes** (agent decisions, ▶/■ bookkeeping, transactions) always go to the primary. The badge shows «Lecturas: réplica / primaria».
- Don't read-your-own-write from the replica inside a transaction; use the primary there.

## Measured latency (reference workspace, serverless, ~350 events/s)

| Path | p50 | p95 |
| --- | --- | --- |
| Direct write (psycopg upsert, `lakebase_directo`) | 0.02 s | 0.03 s |
| Spark, no broker/Delta (`06_tiempo_real_sin_broker`: `rate` → join → sink, 1 stateless query) | 5.7 s | 8.0 s |
| Zerobus → Delta (commit every 5 s) → Spark (3 stateful queries) → sink | ≈ 14 s | ≈ 23 s |
| Future: classic `processingTime` / Real-Time Mode | not measured (serverless-only workspace) | |

Exception profiles: rates and codes vary by region (`live_sim.REGIONS`: north = weather, big cities = addresses, islands = transport, south lower); an optional incident (`INCIDENTS`: DANA Valencia, cortes Barcelona, Filomena Madrid) bursts exceptions inside its `navidad_geo.zone` during the middle 35–65 % of **that zone's own visits** (`live_sim.burst_window`, ranked by arrival time; not a fixed share of the night — Hilbert routes visit a zone in short blocks, so a fixed night window would miss it), and the app draws the zone.

## Explore → Interpret → Build

1. **Explore.** Start a run (`lakebase_directo` is fine) and open `/control`. Read `v_city_progress` and `v_exception` in chat (`read_only: true`): which cities and codes dominate?
2. **Interpret.** "What do you notice?" e.g. the Canary routes finish last (time zone), `direccion_no_encontrada` rarely recovers by retry.
3. **Build** one small improvement on a dev branch, show it, then extend.

## Extension ideas (pick one, ship small)

- **SLA alert:** a city whose open exceptions exceed 0.5 % of its stops flashes in the KPI strip.
- **Exception heatmap:** count exceptions per ~1 km cell (`ST_SnapToGrid`) for the current run; toggle layer.
- **Helper workload:** deliveries per helper in the last 10 simulated minutes; highlight idle and overloaded helpers.
- **Batch review:** "Revisar las 10 más antiguas" — the agent proposes, the user confirms one by one.
- **Incident link:** activate a `navidad_geo` zone on a `sim-*` branch, replay the producer with a higher exception rate inside the zone, compare KPIs.
- **Night replay:** a time slider over `delivery_status.event_ts` for a finished run.
