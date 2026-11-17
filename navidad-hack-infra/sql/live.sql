-- Real-time control center («Centro de control») · Lakebase schema navidad_live
-- Idempotent. __SCHEMA__ is replaced with the workbench schema (navidad_<tag>) by provision_live.py.
--
-- Who writes what (one writer per table):
--   producer notebook (direct psycopg) : sim_run, run_plan
--   streaming sink (Zerobus → Delta → Spark → format("postgresql")) : delivery_status, helper_position, exception_event
--     (in «lakebase_directo» mode the producer writes these three itself, same shape)
--   app + agent (direct psycopg)       : exception_state, agent_review, agent_action
-- updated_at is set by a trigger, so «event → screen» latency = updated_at − sent_ts.

CREATE SCHEMA IF NOT EXISTS navidad_live;

CREATE OR REPLACE FUNCTION navidad_live.touch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := clock_timestamp();
  RETURN NEW;
END $$;

-- ---------------------------------------------------------------- producer
CREATE TABLE IF NOT EXISTS navidad_live.sim_run (
  sim_run_id   text PRIMARY KEY,
  year         int NOT NULL,
  campaign     text NOT NULL,
  mode         text NOT NULL,                 -- zerobus | lakebase_directo
  speed        double precision NOT NULL,     -- simulated seconds per real second
  night_start  timestamptz NOT NULL,          -- simulated clock at t0 (UTC)
  night_end    timestamptz NOT NULL,
  started_at   timestamptz NOT NULL DEFAULT now(),
  ended_at     timestamptz,
  status       text NOT NULL DEFAULT 'en_curso',   -- en_curso | terminada | cancelada
  planned      int NOT NULL,
  helpers      int NOT NULL,
  exception_rate double precision NOT NULL
);
ALTER TABLE navidad_live.sim_run ADD COLUMN IF NOT EXISTS incident text;   -- INCIDENTS key, or NULL

CREATE TABLE IF NOT EXISTS navidad_live.run_plan (
  sim_run_id text NOT NULL REFERENCES navidad_live.sim_run ON DELETE CASCADE,
  city       text NOT NULL,
  lat        double precision NOT NULL,
  lon        double precision NOT NULL,
  planned    int NOT NULL,
  PRIMARY KEY (sim_run_id, city)
);

-- ---------------------------------------------------------------- streaming sink
CREATE TABLE IF NOT EXISTS navidad_live.delivery_status (
  sim_run_id     text   NOT NULL,
  household_id   bigint NOT NULL,
  city           text,
  route_id       bigint,
  stop_number    bigint,
  bringer        text,
  helper_id      text,
  helper_kind    text,
  status         text,                        -- entregado | excepcion
  exception_code text,
  gifts          int,
  lat            double precision,
  lon            double precision,
  event_ts       timestamptz,                 -- simulated clock
  sent_ts        timestamptz,                 -- producer wall clock
  updated_at     timestamptz,
  PRIMARY KEY (sim_run_id, household_id)
);
CREATE INDEX IF NOT EXISTS delivery_status_run_upd ON navidad_live.delivery_status (sim_run_id, updated_at);
CREATE INDEX IF NOT EXISTS delivery_status_run_city ON navidad_live.delivery_status (sim_run_id, city, status);

CREATE TABLE IF NOT EXISTS navidad_live.helper_position (
  sim_run_id  text NOT NULL,
  helper_id   text NOT NULL,
  helper_name text,
  helper_kind text,
  bringer     text,
  city        text,
  route_id    bigint,
  stop_number bigint,
  lat         double precision,
  lon         double precision,
  event_ts    timestamptz,
  sent_ts     timestamptz,
  updated_at  timestamptz,
  PRIMARY KEY (sim_run_id, helper_id)
);

CREATE TABLE IF NOT EXISTS navidad_live.exception_event (
  event_id       text PRIMARY KEY,
  sim_run_id     text NOT NULL,
  household_id   bigint NOT NULL,
  city           text,
  route_id       bigint,
  stop_number    bigint,
  bringer        text,
  helper_id      text,
  helper_kind    text,
  exception_code text,
  lat            double precision,
  lon            double precision,
  event_ts       timestamptz,
  sent_ts        timestamptz,
  updated_at     timestamptz
);
ALTER TABLE navidad_live.exception_event ADD COLUMN IF NOT EXISTS helper_name text;
CREATE INDEX IF NOT EXISTS exception_event_run ON navidad_live.exception_event (sim_run_id, event_ts DESC);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['delivery_status', 'helper_position', 'exception_event'] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS touch ON navidad_live.%I', t);
    EXECUTE format('CREATE TRIGGER touch BEFORE INSERT OR UPDATE ON navidad_live.%I FOR EACH ROW EXECUTE FUNCTION navidad_live.touch()', t);
  END LOOP;
END $$;

-- Stream health, one row per AvailableNow iteration (written by notebook 04)
CREATE TABLE IF NOT EXISTS navidad_live.stream_progress (
  iteration_id bigserial PRIMARY KEY,
  started_at   timestamptz NOT NULL,
  seconds      double precision NOT NULL,
  queries      jsonb NOT NULL                -- {table: {rows, ms}}
);

-- Read-replica lag probe (bonus «Réplica de lectura»): written on the primary, read on the replica
CREATE TABLE IF NOT EXISTS navidad_live.replica_probe (
  probe_id   bigint PRIMARY KEY,
  written_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- ---------------------------------------------------------------- app + agent
CREATE TABLE IF NOT EXISTS navidad_live.exception_state (
  exception_id text PRIMARY KEY,               -- = exception_event.event_id
  status       text NOT NULL CHECK (status IN ('abierta', 'en_revision', 'resuelta', 'descartada')),
  resolution   text,
  updated_by   text,
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS navidad_live.agent_review (
  review_id    bigserial PRIMARY KEY,
  exception_id text NOT NULL,
  model        text,
  context      jsonb NOT NULL,                 -- what the agent saw (tool results)
  diagnosis    text NOT NULL,
  source       text NOT NULL,                  -- llm | reglas (fallback)
  latency_ms   int,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS navidad_live.agent_action (
  action_id    bigserial PRIMARY KEY,
  review_id    bigint NOT NULL REFERENCES navidad_live.agent_review ON DELETE CASCADE,
  exception_id text NOT NULL,
  action_code  text NOT NULL,
  params       jsonb NOT NULL DEFAULT '{}',
  label        text NOT NULL,
  effect       text,
  status       text NOT NULL DEFAULT 'propuesta' CHECK (status IN ('propuesta', 'confirmada', 'rechazada', 'descartada')),
  decided_by   text,
  decided_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_action_exception ON navidad_live.agent_action (exception_id);

-- ---------------------------------------------------------------- read model for the app
-- Explicit column lists (new columns go at the END) so CREATE OR REPLACE keeps the views, their grants and
-- anything built on top of them.
-- State: a decision taken in the app (resuelta / descartada) wins; otherwise a later successful delivery of the
-- same household (retry) resolves it; otherwise abierta / en_revision.
CREATE OR REPLACE VIEW navidad_live.v_exception AS
SELECT e.event_id, e.sim_run_id, e.household_id, e.city, e.route_id, e.stop_number, e.bringer, e.helper_id,
       e.helper_kind, e.exception_code, e.lat, e.lon, e.event_ts, e.sent_ts, e.updated_at, e.helper_name,
       CASE WHEN s.status IN ('resuelta', 'descartada') THEN s.status
            WHEN d.status = 'entregado' AND d.event_ts > e.event_ts THEN 'resuelta'
            ELSE COALESCE(s.status, 'abierta') END AS state,
       CASE WHEN s.status IN ('resuelta', 'descartada') THEN s.resolution
            WHEN d.status = 'entregado' AND d.event_ts > e.event_ts THEN 'Entregado en un reintento' END AS resolution,
       CASE WHEN s.status IN ('resuelta', 'descartada') THEN s.updated_by END AS resolved_by
FROM navidad_live.exception_event e
LEFT JOIN navidad_live.exception_state s ON s.exception_id = e.event_id
LEFT JOIN navidad_live.delivery_status d ON d.sim_run_id = e.sim_run_id AND d.household_id = e.household_id;

CREATE OR REPLACE VIEW navidad_live.v_city_progress AS
SELECT p.sim_run_id, p.city, p.lat, p.lon, p.planned,
       COALESCE(d.delivered, 0) AS delivered,
       COALESCE(x.open, 0)      AS exceptions_open,
       COALESCE(d.processed, 0) AS processed,        -- stops reached so far (delivered or exception)
       COALESCE(t.exceptions, 0) AS exceptions       -- all exceptions so far (rate = exceptions / processed)
FROM navidad_live.run_plan p
LEFT JOIN (SELECT sim_run_id, city, count(*) FILTER (WHERE status = 'entregado') AS delivered, count(*) AS processed
           FROM navidad_live.delivery_status GROUP BY 1, 2) d USING (sim_run_id, city)
LEFT JOIN (SELECT sim_run_id, city, count(*) AS open
           FROM navidad_live.v_exception WHERE state IN ('abierta', 'en_revision') GROUP BY 1, 2) x USING (sim_run_id, city)
LEFT JOIN (SELECT sim_run_id, city, count(*) AS exceptions
           FROM navidad_live.exception_event GROUP BY 1, 2) t USING (sim_run_id, city);

-- Household context for the agent (households_sync lives in the workbench schema)
CREATE OR REPLACE VIEW navidad_live.v_household AS
SELECT h.household_id, h.label, h.city, h.household_size, h.timezone,
       (SELECT count(*) FROM __SCHEMA__.people_sync p WHERE p.household_id = h.household_id AND p.role = 'child') AS children,
       (SELECT string_agg(DISTINCT pr.window_policy, ', ') FROM __SCHEMA__.people_sync p
          JOIN __SCHEMA__.preferences_sync pr ON pr.person_id = p.person_id
         WHERE p.household_id = h.household_id) AS window_policy
FROM __SCHEMA__.households_sync h;
