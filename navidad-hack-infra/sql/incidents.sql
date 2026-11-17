-- =============================================================================
-- Navidad incident hack track: road graph, incident zones and what-if simulation
-- (PostGIS + pgRouting on Lakebase). Idempotent: CREATE … IF NOT EXISTS and
-- CREATE OR REPLACE only. Applied by scripts/provision_incidents.py as the
-- project owner on the production branch; simulations run on CHILD BRANCHES.
--
-- navidad_geo is a native schema on purpose: Lakebase synced tables / CDF do not
-- carry PostGIS geometry. The synced dataset (<schema>.households_sync,
-- journeys_sync) is read by name, passed in as p_schema.
--
-- Road data © OpenStreetMap contributors (ODbL 1.0).
-- =============================================================================
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgrouting;
CREATE SCHEMA IF NOT EXISTS navidad_geo;

-- Road graph ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS navidad_geo.osm_meta (
  city           TEXT PRIMARY KEY,
  edges          INT NOT NULL,
  vertices       INT NOT NULL,
  osm_timestamp  TEXT,
  loaded_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS navidad_geo.vertices (
  id         BIGINT PRIMARY KEY,                 -- OSM node id
  city       TEXT NOT NULL,
  main_component BOOLEAN NOT NULL DEFAULT false, -- in the city's largest strongly connected component
  geom       geometry(Point, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS vertices_geom_gix ON navidad_geo.vertices USING gist (geom);
CREATE INDEX IF NOT EXISTS vertices_city_ix ON navidad_geo.vertices (city);

CREATE TABLE IF NOT EXISTS navidad_geo.ways (
  id                 BIGINT PRIMARY KEY,
  city               TEXT NOT NULL,
  source             BIGINT NOT NULL,
  target             BIGINT NOT NULL,
  highway            TEXT NOT NULL,
  main               BOOLEAN NOT NULL,           -- motorway/trunk/primary/secondary (+links)
  length_m           DOUBLE PRECISION NOT NULL,
  cost_base          DOUBLE PRECISION NOT NULL,  -- seconds at night free-flow speed
  reverse_cost_base  DOUBLE PRECISION NOT NULL,  -- -1 = one-way
  cost               DOUBLE PRECISION NOT NULL,  -- current cost after active zones (-1 = closed)
  reverse_cost       DOUBLE PRECISION NOT NULL,
  geom               geometry(LineString, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS ways_geom_gix ON navidad_geo.ways USING gist (geom);
CREATE INDEX IF NOT EXISTS ways_city_ix ON navidad_geo.ways (city);

-- Household → nearest well-connected road vertex.
CREATE TABLE IF NOT EXISTS navidad_geo.household_node (
  household_id  BIGINT PRIMARY KEY,
  city          TEXT NOT NULL,
  node_id       BIGINT NOT NULL,
  snap_m        DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS household_node_city_ix ON navidad_geo.household_node (city);

-- Incidents -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS navidad_geo.zone (
  zone_id        TEXT PRIMARY KEY,
  scenario_id    TEXT NOT NULL,
  city           TEXT NOT NULL,
  kind           TEXT NOT NULL,                  -- nieve | inundacion | corte | custom
  label          TEXT NOT NULL,
  closed         BOOLEAN NOT NULL DEFAULT false, -- true: roads inside are impassable
  main_only      BOOLEAN NOT NULL DEFAULT false, -- closure applies to main roads only (e.g. tunnels)
  factor_main    DOUBLE PRECISION NOT NULL DEFAULT 1,  -- travel-time multiplier, main roads
  factor_local   DOUBLE PRECISION NOT NULL DEFAULT 1,  -- travel-time multiplier, local streets
  dwell_factor   DOUBLE PRECISION NOT NULL DEFAULT 1,  -- time-at-stop multiplier
  appears_at_min INT NOT NULL DEFAULT 0,         -- replay: minutes into the night
  active         BOOLEAN NOT NULL DEFAULT true,
  geom           geometry(MultiPolygon, 4326) NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS zone_geom_gix ON navidad_geo.zone USING gist (geom);
ALTER TABLE navidad_geo.zone ADD COLUMN IF NOT EXISTS main_only BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS navidad_geo.scenario (
  scenario_id  TEXT PRIMARY KEY,
  city         TEXT NOT NULL,
  campaign     TEXT NOT NULL,                    -- nochebuena | reyes
  year         INT NOT NULL,
  title        TEXT NOT NULL,
  story        TEXT NOT NULL
);

-- Simulation results (written on the simulation branch) -------------------------
CREATE TABLE IF NOT EXISTS navidad_geo.sim_leg (
  route_id      BIGINT NOT NULL,
  stop_number   INT NOT NULL,                    -- leg = stop_number → next stop
  bringer       TEXT NOT NULL,
  hh_from       BIGINT NOT NULL,
  hh_to         BIGINT NOT NULL,
  node_from     BIGINT NOT NULL,
  node_to       BIGINT NOT NULL,
  dwell_min     INT NOT NULL,
  dwell_factor  DOUBLE PRECISION NOT NULL,
  sampled       BOOLEAN NOT NULL,
  base_s        DOUBLE PRECISION,
  sim_s         DOUBLE PRECISION,
  status        TEXT NOT NULL,                   -- ok | lento | desvio | sin_ruta | inaccesible | sin_evaluar
  PRIMARY KEY (route_id, stop_number)
);

CREATE TABLE IF NOT EXISTS navidad_geo.sim_route (
  route_id        BIGINT PRIMARY KEY,
  bringer         TEXT NOT NULL,
  stops           INT NOT NULL,
  affected_legs   INT NOT NULL,
  sampled_legs    INT NOT NULL,
  ratio_affected  DOUBLE PRECISION NOT NULL,     -- sim/base time over sampled affected legs (incl. dwell)
  load_factor     DOUBLE PRECISION NOT NULL,     -- whole-route time vs normal night (1 = fits)
  late_stops      INT NOT NULL,                  -- stops that no longer fit in the night
  unreachable     INT NOT NULL
);

CREATE TABLE IF NOT EXISTS navidad_geo.sim_suggestion (
  id            BIGSERIAL PRIMARY KEY,
  kind          TEXT NOT NULL,                   -- desvio | punto_recogida | refuerzo | resecuenciar
  route_id      BIGINT,
  household_id  BIGINT,
  from_bringer  TEXT,
  to_bringer    TEXT,
  detail        JSONB NOT NULL DEFAULT '{}'::jsonb,
  geom          geometry(Geometry, 4326)
);

CREATE TABLE IF NOT EXISTS navidad_geo.sim_kpi (
  key    TEXT PRIMARY KEY,
  value  JSONB NOT NULL
);

-- "Apply plan": the one table written back to production.
CREATE TABLE IF NOT EXISTS navidad_geo.applied_plan (
  plan_id       TEXT NOT NULL,
  scenario_id   TEXT NOT NULL,
  branch        TEXT NOT NULL,
  applied_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  applied_by    TEXT,
  kind          TEXT NOT NULL,
  route_id      BIGINT,
  household_id  BIGINT,
  from_bringer  TEXT,
  to_bringer    TEXT,
  detail        JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS applied_plan_ix ON navidad_geo.applied_plan (plan_id);

-- Helpers ---------------------------------------------------------------------
-- Circular sector (Reyes Kings own 120° sectors, measured counter-clockwise from east).
CREATE OR REPLACE FUNCTION navidad_geo.wedge(p_lon FLOAT8, p_lat FLOAT8, p_radius_m FLOAT8,
                                             p_from_deg FLOAT8, p_to_deg FLOAT8)
RETURNS geometry LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT ST_Multi(ST_MakePolygon(ST_MakeLine(ARRAY[c] || pts || ARRAY[c])))
  FROM (SELECT ST_SetSRID(ST_MakePoint(p_lon, p_lat), 4326) AS c) o,
  LATERAL (
    SELECT array_agg(
             ST_Project(o.c::geography, p_radius_m, radians(90 - a::float8))::geometry ORDER BY a) AS pts
    FROM generate_series(p_from_deg::numeric, p_to_deg::numeric, 5) a
  ) s
$$;

CREATE OR REPLACE FUNCTION navidad_geo.buffer_m(g geometry, p_m FLOAT8)
RETURNS geometry LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT ST_Multi(ST_Buffer(g::geography, p_m)::geometry)
$$;

CREATE OR REPLACE FUNCTION navidad_geo.line(VARIADIC p FLOAT8[])
RETURNS geometry LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT ST_SetSRID(ST_MakeLine(array_agg(ST_MakePoint(p[i], p[i + 1]) ORDER BY i)), 4326)
  FROM generate_series(1, array_length(p, 1) - 1, 2) i
$$;

-- Largest strongly connected component per city (one-way streets can strand nodes).
CREATE OR REPLACE FUNCTION navidad_geo.mark_components(p_city TEXT) RETURNS INT
LANGUAGE plpgsql AS $$
DECLARE n INT;
BEGIN
  UPDATE navidad_geo.vertices SET main_component = false WHERE city = p_city;
  WITH sc AS (
    SELECT component, node FROM pgr_strongComponents(format(
      'SELECT id, source, target, cost_base AS cost, reverse_cost_base AS reverse_cost
         FROM navidad_geo.ways WHERE city = %L', p_city))
  ), big AS (
    SELECT component FROM sc GROUP BY component ORDER BY count(*) DESC LIMIT 1
  )
  UPDATE navidad_geo.vertices v SET main_component = true
  FROM sc JOIN big USING (component)
  WHERE v.id = sc.node AND v.city = p_city;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- Snap every household of the city to its nearest main-component vertex.
CREATE OR REPLACE FUNCTION navidad_geo.snap_households(p_schema TEXT, p_city TEXT) RETURNS INT
LANGUAGE plpgsql AS $$
DECLARE n INT;
BEGIN
  DELETE FROM navidad_geo.household_node WHERE city = p_city;
  EXECUTE format($q$
    INSERT INTO navidad_geo.household_node (household_id, city, node_id, snap_m)
    SELECT h.household_id, h.city, v.id,
           ST_Distance(ST_SetSRID(ST_MakePoint(h.lon, h.lat), 4326)::geography, v.geom::geography)
    FROM %I.households_sync h
    CROSS JOIN LATERAL (
      SELECT id, geom FROM navidad_geo.vertices
      WHERE city = h.city AND main_component
      ORDER BY geom <-> ST_SetSRID(ST_MakePoint(h.lon, h.lat), 4326)
      LIMIT 1
    ) v
    WHERE h.city = %L $q$, p_schema, p_city);
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- Scenario presets -------------------------------------------------------------
-- Inspired by real events (Borrasca Filomena, Jan 2021; DANA, Oct 2024). Geometry is
-- illustrative, not a reconstruction. Re-running a preset replaces its zones.
CREATE OR REPLACE FUNCTION navidad_geo.load_scenario(p_scenario TEXT) RETURNS INT
LANGUAGE plpgsql AS $$
DECLARE n INT;
BEGIN
  DELETE FROM navidad_geo.zone WHERE scenario_id = p_scenario;
  IF p_scenario = 'filomena_madrid' THEN
    INSERT INTO navidad_geo.scenario VALUES
      ('filomena_madrid', 'Madrid', 'reyes', 2025, 'Borrasca tipo Filomena en Madrid',
       'Nevada histórica la noche de Reyes: toda la ciudad va más lenta y el norte y el oeste, mucho más. ¿Llegan los Reyes antes del amanecer?')
    ON CONFLICT (scenario_id) DO UPDATE SET title = EXCLUDED.title, story = EXCLUDED.story;
    INSERT INTO navidad_geo.zone (zone_id, scenario_id, city, kind, label, closed, factor_main, factor_local, dwell_factor, geom) VALUES
      ('filomena_moderada', p_scenario, 'Madrid', 'nieve', 'Nieve en toda la ciudad', false, 1.5, 2.5, 1.2,
         navidad_geo.buffer_m(ST_SetSRID(ST_MakePoint(-3.7038, 40.4168), 4326), 11000)),
      ('filomena_intensa', p_scenario, 'Madrid', 'nieve', 'Nevada intensa (norte y oeste)', false, 2.5, 5.0, 1.5,
         navidad_geo.wedge(-3.7038, 40.4168, 11000, 60, 200)),
      ('filomena_m30', p_scenario, 'Madrid', 'corte', 'Túneles de la M-30 cerrados', true, 1, 1, 1,
         navidad_geo.buffer_m(navidad_geo.line(-3.7270, 40.4230, -3.7200, 40.4050, -3.7080, 40.3950, -3.6950, 40.3880), 80));
    UPDATE navidad_geo.zone SET main_only = true WHERE zone_id = 'filomena_m30';
  ELSIF p_scenario = 'dana_valencia' THEN
    INSERT INTO navidad_geo.scenario VALUES
      ('dana_valencia', 'Valencia', 'nochebuena', 2025, 'DANA en l''Horta Sud',
       'Lluvias torrenciales desbordan el barranco: calles inundadas y puentes cortados al sur de la ciudad. ¿A qué hogares no se puede llegar y por dónde se rodea?')
    ON CONFLICT (scenario_id) DO UPDATE SET title = EXCLUDED.title, story = EXCLUDED.story;
    INSERT INTO navidad_geo.zone (zone_id, scenario_id, city, kind, label, closed, factor_main, factor_local, dwell_factor, geom) VALUES
      ('dana_inundacion', p_scenario, 'Valencia', 'inundacion', 'Zona inundada (barranco)', true, 1, 1, 1,
         navidad_geo.buffer_m(navidad_geo.line(-0.4660, 39.4250, -0.4300, 39.4280, -0.4180, 39.4280,
                                               -0.3950, 39.4230, -0.3830, 39.4200, -0.3600, 39.4050), 1200)),
      ('dana_puente', p_scenario, 'Valencia', 'corte', 'Puente cortado (nuevo cauce)', true, 1, 1, 1,
         navidad_geo.buffer_m(ST_SetSRID(ST_MakePoint(-0.3830, 39.4480), 4326), 150)),
      ('dana_lodo', p_scenario, 'Valencia', 'inundacion', 'Calles con barro y agua', false, 2.0, 4.0, 1.3,
         navidad_geo.buffer_m(navidad_geo.line(-0.4660, 39.4250, -0.4180, 39.4280, -0.3830, 39.4200, -0.3600, 39.4050), 3000));
  ELSIF p_scenario = 'cortes_barcelona' THEN
    INSERT INTO navidad_geo.scenario VALUES
      ('cortes_barcelona', 'Barcelona', 'nochebuena', 2025, 'Cortes de calle en Barcelona',
       'Incidentes y contenedores quemados obligan a cortar calles durante la noche. Las rutas se recalculan a medida que aparecen los cortes.')
    ON CONFLICT (scenario_id) DO UPDATE SET title = EXCLUDED.title, story = EXCLUDED.story;
    INSERT INTO navidad_geo.zone (zone_id, scenario_id, city, kind, label, closed, appears_at_min, geom) VALUES
      ('bcn_eixample', p_scenario, 'Barcelona', 'corte', 'Corte en l''Eixample', true, 0,  navidad_geo.buffer_m(ST_SetSRID(ST_MakePoint(2.1650, 41.3910), 4326), 450)),
      ('bcn_raval',    p_scenario, 'Barcelona', 'corte', 'Corte en el Raval',     true, 10, navidad_geo.buffer_m(ST_SetSRID(ST_MakePoint(2.1690, 41.3800), 4326), 350)),
      ('bcn_sants',    p_scenario, 'Barcelona', 'corte', 'Corte en Sants',        true, 20, navidad_geo.buffer_m(ST_SetSRID(ST_MakePoint(2.1350, 41.3750), 4326), 400)),
      ('bcn_gracia',   p_scenario, 'Barcelona', 'corte', 'Corte en Gràcia',       true, 30, navidad_geo.buffer_m(ST_SetSRID(ST_MakePoint(2.1570, 41.4030), 4326), 350)),
      ('bcn_poblenou', p_scenario, 'Barcelona', 'corte', 'Corte en el Poblenou',  true, 40, navidad_geo.buffer_m(ST_SetSRID(ST_MakePoint(2.1970, 41.4000), 4326), 500));
  ELSE
    RAISE EXCEPTION 'unknown scenario %', p_scenario;
  END IF;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- Recompute every edge cost of a city from the base cost and its ACTIVE zones.
-- Overlapping zones: closed wins, otherwise the largest factor applies.
CREATE OR REPLACE FUNCTION navidad_geo.refresh_costs(p_city TEXT) RETURNS INT
LANGUAGE plpgsql AS $$
DECLARE n INT;
BEGIN
  UPDATE navidad_geo.ways SET cost = cost_base, reverse_cost = reverse_cost_base
  WHERE city = p_city AND (cost <> cost_base OR reverse_cost <> reverse_cost_base);
  WITH hit AS (
    SELECT w.id, bool_or(z.closed AND (w.main OR NOT z.main_only)) AS closed,
           max(CASE WHEN w.main THEN z.factor_main ELSE z.factor_local END) AS f
    FROM navidad_geo.ways w
    JOIN navidad_geo.zone z ON z.active AND z.city = w.city AND ST_Intersects(w.geom, z.geom)
    WHERE w.city = p_city
    GROUP BY w.id
  )
  UPDATE navidad_geo.ways w
  SET cost = CASE WHEN hit.closed THEN -1 ELSE w.cost_base * hit.f END,
      reverse_cost = CASE WHEN hit.closed OR w.reverse_cost_base < 0 THEN -1 ELSE w.reverse_cost_base * hit.f END
  FROM hit WHERE w.id = hit.id;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- The simulation -------------------------------------------------------------
-- Legs = consecutive stops of each route. Affected = the straight leg passes within
-- p_buffer_m of an active zone or ends inside one. At most p_max_legs affected legs
-- are routed (deterministic sample); route-level load extrapolates from the sample.
CREATE OR REPLACE FUNCTION navidad_geo.simulate(p_schema TEXT, p_scenario TEXT,
                                                p_max_legs INT DEFAULT 3000,
                                                p_buffer_m FLOAT8 DEFAULT 300)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
  s RECORD;
  edges_base TEXT;
  edges_sim TEXT;
  t0 TIMESTAMPTZ := clock_timestamp();
  k JSONB;
BEGIN
  SELECT * INTO s FROM navidad_geo.scenario WHERE scenario_id = p_scenario;
  IF NOT FOUND THEN RAISE EXCEPTION 'scenario % not loaded', p_scenario; END IF;

  PERFORM navidad_geo.refresh_costs(s.city);
  TRUNCATE navidad_geo.sim_leg, navidad_geo.sim_route, navidad_geo.sim_suggestion, navidad_geo.sim_kpi;

  -- 1. legs of the night in this city, flagged when near an active zone
  EXECUTE format($q$
    WITH stops AS (
      SELECT j.route_id, j.stop_number, j.bringer, j.household_id, j.dwell_minutes, hn.node_id,
             ST_SetSRID(ST_MakePoint(h.lon, h.lat), 4326) AS pt
      FROM %1$I.journeys_sync j
      JOIN %1$I.households_sync h ON h.household_id = j.household_id
      JOIN navidad_geo.household_node hn ON hn.household_id = j.household_id
      WHERE j.year = %2$s AND j.campaign = %3$L AND h.city = %4$L
    ), legs AS (
      SELECT route_id, stop_number, bringer, household_id AS hh_from, node_id AS node_from, pt AS p_from,
             lead(household_id) OVER w AS hh_to, lead(node_id) OVER w AS node_to,
             lead(pt) OVER w AS p_to, lead(dwell_minutes) OVER w AS dwell_min
      FROM stops WINDOW w AS (PARTITION BY route_id ORDER BY stop_number)
    ), zones AS (
      SELECT ST_Union(navidad_geo.buffer_m(geom, %5$s)) AS near, ST_Union(geom) AS inside,
             ST_Union(geom) FILTER (WHERE closed AND NOT main_only) AS closed_area
      FROM navidad_geo.zone WHERE active AND city = %4$L
    )
    INSERT INTO navidad_geo.sim_leg (route_id, stop_number, bringer, hh_from, hh_to, node_from, node_to,
                                     dwell_min, dwell_factor, sampled, status)
    SELECT l.route_id, l.stop_number, l.bringer, l.hh_from, l.hh_to, l.node_from, l.node_to, l.dwell_min,
           coalesce((SELECT max(z.dwell_factor) FROM navidad_geo.zone z
                     WHERE z.active AND z.city = %4$L AND ST_Intersects(z.geom, l.p_to)), 1),
           false,
           CASE WHEN ST_Intersects(z.closed_area, l.p_to) THEN 'inaccesible'
                WHEN ST_Intersects(z.near, ST_MakeLine(l.p_from, l.p_to)) THEN 'sin_evaluar'
                ELSE 'ok' END
    FROM legs l CROSS JOIN zones z
    WHERE l.hh_to IS NOT NULL $q$,
    p_schema, s.year, s.campaign, s.city, p_buffer_m);

  -- 2. deterministic sample of affected legs to route
  UPDATE navidad_geo.sim_leg SET sampled = true
  WHERE (route_id, stop_number) IN (
    SELECT route_id, stop_number FROM navidad_geo.sim_leg WHERE status = 'sin_evaluar'
    ORDER BY hashint8(route_id * 100003 + stop_number) LIMIT p_max_legs);

  -- 3. pgRouting: base vs incident travel time for the sampled legs
  edges_base := format('SELECT id, source, target, cost_base AS cost, reverse_cost_base AS reverse_cost
                          FROM navidad_geo.ways WHERE city = %L', s.city);
  edges_sim  := format('SELECT id, source, target, cost, reverse_cost FROM navidad_geo.ways WHERE city = %L', s.city);
  CREATE TEMP TABLE IF NOT EXISTS _pairs (source BIGINT, target BIGINT) ON COMMIT DROP;
  TRUNCATE _pairs;
  INSERT INTO _pairs SELECT DISTINCT node_from, node_to FROM navidad_geo.sim_leg
  WHERE sampled AND node_from <> node_to;

  UPDATE navidad_geo.sim_leg l SET base_s = c.agg_cost
  FROM pgr_dijkstraCost(edges_base, 'SELECT source, target FROM _pairs', true) c
  WHERE l.sampled AND l.node_from = c.start_vid AND l.node_to = c.end_vid;
  UPDATE navidad_geo.sim_leg l SET sim_s = c.agg_cost
  FROM pgr_dijkstraCost(edges_sim, 'SELECT source, target FROM _pairs', true) c
  WHERE l.sampled AND l.node_from = c.start_vid AND l.node_to = c.end_vid;
  UPDATE navidad_geo.sim_leg SET base_s = 0, sim_s = 0 WHERE sampled AND node_from = node_to;

  UPDATE navidad_geo.sim_leg SET status = CASE
      WHEN base_s IS NULL THEN 'ok'                       -- no path even before the incident: ignore
      WHEN sim_s IS NULL THEN 'sin_ruta'
      WHEN sim_s > base_s * 1.5 + 60 THEN 'desvio'
      WHEN sim_s > base_s * 1.05 THEN 'lento'
      ELSE 'ok' END
  WHERE sampled;

  -- 4. per-route load: affected share × time ratio over the sample (dwell included)
  INSERT INTO navidad_geo.sim_route
  SELECT route_id, min(bringer), count(*) + 1,
         count(*) FILTER (WHERE status <> 'ok' OR sampled),
         count(*) FILTER (WHERE sampled AND sim_s IS NOT NULL AND base_s IS NOT NULL),
         coalesce(sum(sim_s + dwell_min * 60 * dwell_factor) FILTER (WHERE sampled AND sim_s IS NOT NULL AND base_s IS NOT NULL)
                  / nullif(sum(base_s + dwell_min * 60) FILTER (WHERE sampled AND sim_s IS NOT NULL AND base_s IS NOT NULL), 0), 1),
         1, 0,
         count(*) FILTER (WHERE status IN ('inaccesible', 'sin_ruta'))
  FROM navidad_geo.sim_leg GROUP BY route_id;
  UPDATE navidad_geo.sim_route
  SET load_factor = 1 + (affected_legs::float8 / greatest(stops - 1, 1)) * (ratio_affected - 1);
  UPDATE navidad_geo.sim_route
  SET late_stops = CASE WHEN load_factor > 1 THEN round((stops - unreachable) * (1 - 1 / load_factor))::int ELSE 0 END;

  -- 5. KPIs
  SELECT jsonb_build_object(
    'scenario', p_scenario, 'city', s.city, 'campaign', s.campaign, 'year', s.year,
    'stops', (SELECT sum(stops) FROM navidad_geo.sim_route),
    'affected_legs', (SELECT count(*) FROM navidad_geo.sim_leg WHERE status <> 'ok' OR sampled),
    'sampled_legs', (SELECT count(*) FROM navidad_geo.sim_leg WHERE sampled),
    'unreachable_households', (SELECT count(DISTINCT hh_to) FROM navidad_geo.sim_leg WHERE status IN ('inaccesible', 'sin_ruta')),
    'detours', (SELECT count(*) FROM navidad_geo.sim_leg WHERE status = 'desvio'),
    'slow_legs', (SELECT count(*) FROM navidad_geo.sim_leg WHERE status = 'lento'),
    'detours_est', (SELECT round(count(*) FILTER (WHERE status = 'desvio') * count(*) FILTER (WHERE status <> 'ok' OR sampled)::numeric
                           / nullif(count(*) FILTER (WHERE sampled), 0)) FROM navidad_geo.sim_leg),
    'extra_min_per_leg', (SELECT round((avg(sim_s - base_s) / 60)::numeric, 1) FROM navidad_geo.sim_leg WHERE sampled AND sim_s IS NOT NULL AND base_s IS NOT NULL),
    'late_stops', (SELECT sum(late_stops) FROM navidad_geo.sim_route),
    'routes', (SELECT jsonb_agg(jsonb_build_object('route_id', route_id, 'bringer', bringer, 'stops', stops,
                     'load_factor', round(load_factor::numeric, 3), 'late_stops', late_stops, 'unreachable', unreachable)
                     ORDER BY bringer) FROM navidad_geo.sim_route),
    'elapsed_s', round(extract(epoch FROM clock_timestamp() - t0)::numeric, 1)
  ) INTO k;
  INSERT INTO navidad_geo.sim_kpi VALUES ('simulate', k)
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
  RETURN k;
END $$;

-- Suggestions --------------------------------------------------------------------
-- desvio: rerouted path for the worst legs; punto_recogida: households cut off are
-- grouped (~800 m) around a still-reachable road point; refuerzo: helpers per route so
-- the night fits again, with the late tail handed over (children first as a KPI);
-- resecuenciar: pgr_TSP over the affected stops of the worst route (≤ p_tsp_max stops).
CREATE OR REPLACE FUNCTION navidad_geo.suggest(p_schema TEXT, p_scenario TEXT,
                                               p_detours INT DEFAULT 25, p_tsp_max INT DEFAULT 120)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
  s RECORD;
  edges_sim TEXT;
  moved INT := 0;
  tsp_route BIGINT; tsp_before FLOAT8; tsp_after FLOAT8; tsp_n INT; tsp_anchor BIGINT; tsp_skip TEXT;
  k JSONB;
BEGIN
  SELECT * INTO s FROM navidad_geo.scenario WHERE scenario_id = p_scenario;
  DELETE FROM navidad_geo.sim_suggestion;
  edges_sim := format('SELECT id, source, target, cost, reverse_cost FROM navidad_geo.ways WHERE city = %L', s.city);

  -- desvio: incident path of the legs with the largest delay
  CREATE TEMP TABLE IF NOT EXISTS _worst (route_id BIGINT, stop_number INT, source BIGINT, target BIGINT, delay FLOAT8) ON COMMIT DROP;
  TRUNCATE _worst;
  INSERT INTO _worst SELECT route_id, stop_number, node_from, node_to, sim_s - base_s
  FROM navidad_geo.sim_leg WHERE status = 'desvio' ORDER BY sim_s - base_s DESC LIMIT p_detours;
  INSERT INTO navidad_geo.sim_suggestion (kind, route_id, detail, geom)
  SELECT 'desvio', w.route_id, jsonb_build_object('stop_number', w.stop_number, 'extra_min', round((w.delay / 60)::numeric, 1)),
         ST_LineMerge(ST_Collect(e.geom ORDER BY p.path_seq))
  FROM _worst w
  JOIN pgr_dijkstra(edges_sim, 'SELECT source, target FROM _worst', true) p
    ON p.start_vid = w.source AND p.end_vid = w.target
  JOIN navidad_geo.ways e ON e.id = p.edge
  GROUP BY w.route_id, w.stop_number, w.delay;

  -- punto_recogida: per cut-off household, nearest vertex outside closed zones; clustered
  INSERT INTO navidad_geo.sim_suggestion (kind, household_id, detail, geom)
  SELECT 'punto_recogida', l.hh_to, jsonb_build_object('route_id', l.route_id), v.geom
  FROM (SELECT DISTINCT ON (hh_to) hh_to, route_id, node_to FROM navidad_geo.sim_leg
        WHERE status IN ('inaccesible', 'sin_ruta')) l
  JOIN navidad_geo.vertices hv ON hv.id = l.node_to
  CROSS JOIN LATERAL (
    SELECT vv.geom FROM navidad_geo.vertices vv
    WHERE vv.city = s.city AND vv.main_component
      AND NOT EXISTS (SELECT 1 FROM navidad_geo.zone z WHERE z.active AND z.closed AND NOT z.main_only AND z.city = s.city AND ST_Intersects(z.geom, vv.geom))
    ORDER BY vv.geom <-> hv.geom LIMIT 1) v;
  WITH c AS (
    SELECT id, ST_ClusterDBSCAN(ST_Transform(geom, 3857), eps := 800, minpoints := 1) OVER () AS cid
    FROM navidad_geo.sim_suggestion WHERE kind = 'punto_recogida'
  ), centre AS (
    SELECT c.cid, ST_PointOnSurface(ST_Collect(s2.geom)) AS g, count(*) AS n
    FROM c JOIN navidad_geo.sim_suggestion s2 ON s2.id = c.id GROUP BY c.cid
  ), snapped AS (
    SELECT centre.cid, centre.n, v.geom FROM centre
    CROSS JOIN LATERAL (
      SELECT vv.geom FROM navidad_geo.vertices vv
      WHERE vv.city = s.city AND vv.main_component
        AND NOT EXISTS (SELECT 1 FROM navidad_geo.zone z WHERE z.active AND z.closed AND NOT z.main_only
                        AND z.city = s.city AND ST_Intersects(z.geom, vv.geom))
      ORDER BY vv.geom <-> centre.g LIMIT 1) v
  )
  UPDATE navidad_geo.sim_suggestion t
  SET geom = sn.geom, detail = t.detail || jsonb_build_object('pickup_id', sn.cid, 'households_at_point', sn.n)
  FROM c JOIN snapped sn ON sn.cid = c.cid WHERE t.id = c.id;

  -- refuerzo: helpers per route so the night fits again; the late tail of each route
  -- (last stops by stop_number) goes to the helpers. Children first: late slots are
  -- filled with households without children whenever possible.
  EXECUTE format($q$
    WITH kids AS (
      SELECT DISTINCT p.household_id FROM %1$I.people_sync p
      JOIN navidad_geo.household_node hn ON hn.household_id = p.household_id AND hn.city = %2$L
      WHERE p.role = 'child'),
    tail AS (
      SELECT l.route_id, l.hh_to, l.stop_number, r.bringer, r.late_stops,
             row_number() OVER (PARTITION BY l.route_id ORDER BY l.stop_number DESC) AS from_end
      FROM navidad_geo.sim_leg l JOIN navidad_geo.sim_route r USING (route_id)
      WHERE r.late_stops > 0 AND l.status NOT IN ('inaccesible', 'sin_ruta'))
    INSERT INTO navidad_geo.sim_suggestion (kind, route_id, household_id, from_bringer, to_bringer, detail)
    SELECT 'refuerzo', t.route_id, t.hh_to, t.bringer, 'ayudante_' || t.bringer,
           jsonb_build_object('stop_number', t.stop_number, 'with_children', k.household_id IS NOT NULL)
    FROM tail t LEFT JOIN kids k ON k.household_id = t.hh_to
    WHERE t.from_end <= t.late_stops $q$, p_schema, s.city);
  EXECUTE format($q$
    WITH kids AS (
      SELECT DISTINCT p.household_id FROM %1$I.people_sync p
      JOIN navidad_geo.household_node hn ON hn.household_id = p.household_id AND hn.city = %2$L
      WHERE p.role = 'child'),
    per_route AS (
      SELECT r.route_id, r.late_stops,
             count(*) FILTER (WHERE k.household_id IS NULL) AS no_kids
      FROM navidad_geo.sim_route r
      JOIN navidad_geo.sim_leg l USING (route_id)
      LEFT JOIN kids k ON k.household_id = l.hh_to
      WHERE l.status NOT IN ('inaccesible', 'sin_ruta')
      GROUP BY r.route_id, r.late_stops)
    INSERT INTO navidad_geo.sim_kpi
    SELECT 'children', jsonb_build_object(
      'late_with_children_now', (SELECT count(*) FROM navidad_geo.sim_suggestion WHERE kind = 'refuerzo' AND (detail->>'with_children')::boolean),
      'late_with_children_if_prioritised', (SELECT coalesce(sum(greatest(0, late_stops - no_kids)), 0) FROM per_route))
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value $q$, p_schema, s.city);
  SELECT coalesce(sum(greatest(0, ceil(load_factor) - 1)), 0)::int INTO moved FROM navidad_geo.sim_route;

  -- resecuenciar: TSP over the affected, reachable stops of the worst route
  SELECT route_id INTO tsp_route FROM navidad_geo.sim_route ORDER BY load_factor DESC LIMIT 1;
  CREATE TEMP TABLE IF NOT EXISTS _cluster (stop_number INT, node BIGINT) ON COMMIT DROP;
  TRUNCATE _cluster;
  INSERT INTO _cluster
  SELECT DISTINCT ON (node_to) stop_number, node_to FROM navidad_geo.sim_leg
  WHERE route_id = tsp_route AND sampled AND status IN ('lento', 'desvio')
  ORDER BY node_to, stop_number LIMIT p_tsp_max;
  SELECT count(*) INTO tsp_n FROM _cluster;
  IF tsp_n < 4 THEN
    tsp_skip := format('only %s affected stops (min 4)', tsp_n);
  ELSE
    -- A suggestion must never fail the simulation, but a skip must be visible: the reason goes to
    -- the 'suggest' KPI (resequence_skipped) and a WARNING, not a silent NOTICE.
    BEGIN
      CREATE TEMP TABLE IF NOT EXISTS _matrix (start_vid BIGINT, end_vid BIGINT, agg_cost FLOAT8) ON COMMIT DROP;
      TRUNCATE _matrix;
      INSERT INTO _matrix SELECT start_vid, end_vid, agg_cost
      FROM pgr_dijkstraCostMatrix(edges_sim, (SELECT array_agg(node) FROM _cluster), directed => false);
      -- With closures some affected stops can't reach each other: pgr_TSP needs a complete matrix
      -- («Graph is not fully connected»). The graph is undirected, so reachability splits the stops
      -- into components: keep exactly ONE, the largest (ties → lowest node id), anchored on one node.
      SELECT start_vid INTO tsp_anchor FROM _matrix GROUP BY start_vid ORDER BY count(*) DESC, start_vid LIMIT 1;
      DELETE FROM _cluster c
      WHERE c.node IS DISTINCT FROM tsp_anchor
        AND NOT EXISTS (SELECT 1 FROM _matrix m WHERE m.start_vid = tsp_anchor AND m.end_vid = c.node);
      DELETE FROM _matrix m WHERE m.start_vid NOT IN (SELECT node FROM _cluster) OR m.end_vid NOT IN (SELECT node FROM _cluster);
      SELECT count(*) INTO tsp_n FROM _cluster;
      IF tsp_n < 4 THEN
        tsp_skip := format('largest connected group has %s stops (min 4)', tsp_n);
      ELSIF (SELECT count(*) FROM _matrix) <> tsp_n * (tsp_n - 1) THEN
        tsp_skip := format('cost matrix incomplete (%s of %s pairs)', (SELECT count(*) FROM _matrix), tsp_n * (tsp_n - 1));
      ELSE
        -- current order (by stop_number) vs TSP tour, both closed tours on the same matrix
        SELECT sum(m.agg_cost) INTO tsp_before
        FROM (SELECT node, coalesce(lead(node) OVER (ORDER BY stop_number),
                                    (SELECT node FROM _cluster ORDER BY stop_number LIMIT 1)) AS nxt
              FROM _cluster) o
        JOIN _matrix m ON m.start_vid = o.node AND m.end_vid = o.nxt;
        SELECT max(agg_cost) INTO tsp_after FROM pgr_TSP('SELECT start_vid, end_vid, agg_cost FROM _matrix');
        INSERT INTO navidad_geo.sim_suggestion (kind, route_id, detail)
        VALUES ('resecuenciar', tsp_route, jsonb_build_object(
          'stops', tsp_n, 'before_min', round((tsp_before / 60)::numeric, 1), 'after_min', round((tsp_after / 60)::numeric, 1),
          'saving_pct', round((100 * (1 - tsp_after / nullif(tsp_before, 0)))::numeric, 1)));
      END IF;
    EXCEPTION WHEN OTHERS THEN
      tsp_skip := format('error %s: %s', SQLSTATE, SQLERRM);
      RAISE WARNING 'resecuenciar skipped: %', tsp_skip;
    END;
  END IF;

  SELECT jsonb_build_object(
    'detours', (SELECT count(*) FROM navidad_geo.sim_suggestion WHERE kind = 'desvio'),
    'pickup_households', (SELECT count(*) FROM navidad_geo.sim_suggestion WHERE kind = 'punto_recogida'),
    'pickup_points', (SELECT count(DISTINCT detail->>'pickup_id') FROM navidad_geo.sim_suggestion WHERE kind = 'punto_recogida'),
    'helpers', moved,
    'helper_label', CASE WHEN s.campaign = 'reyes' THEN 'pajes reales' ELSE 'ayudantes' END,
    'stops_to_helpers', (SELECT count(*) FROM navidad_geo.sim_suggestion WHERE kind = 'refuerzo'),
    'late_stops', (SELECT sum(late_stops) FROM navidad_geo.sim_route),
    'children', (SELECT value FROM navidad_geo.sim_kpi WHERE key = 'children'),
    'resequence', (SELECT detail FROM navidad_geo.sim_suggestion WHERE kind = 'resecuenciar' LIMIT 1),
    'resequence_skipped', tsp_skip
  ) INTO k;
  INSERT INTO navidad_geo.sim_kpi VALUES ('suggest', k)
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
  RETURN k;
END $$;

-- Map payload ------------------------------------------------------------------
CREATE OR REPLACE FUNCTION navidad_geo.sim_geojson(p_schema TEXT, p_max_points INT DEFAULT 4000)
RETURNS JSONB LANGUAGE plpgsql STABLE AS $$
DECLARE k JSONB;
BEGIN
  EXECUTE format($q$
    SELECT jsonb_build_object(
      'zones', (SELECT jsonb_agg(jsonb_build_object('type', 'Feature', 'geometry', ST_AsGeoJSON(geom, 5)::jsonb,
                 'properties', jsonb_build_object('zone_id', zone_id, 'label', label, 'kind', kind, 'closed', closed,
                   'factor_local', factor_local, 'appears_at_min', appears_at_min, 'active', active)))
                FROM navidad_geo.zone WHERE scenario_id = (SELECT value->>'scenario' FROM navidad_geo.sim_kpi WHERE key = 'simulate')),
      'stops', (SELECT jsonb_agg(jsonb_build_array(round(h.lon::numeric, 5), round(h.lat::numeric, 5), l.status, l.bringer))
                FROM (SELECT * FROM navidad_geo.sim_leg WHERE status <> 'ok'
                      ORDER BY (status IN ('inaccesible', 'sin_ruta')) DESC, hashint8(hh_to) LIMIT %2$s) l
                JOIN %1$I.households_sync h ON h.household_id = l.hh_to),
      'suggestions', (SELECT jsonb_agg(jsonb_build_object('type', 'Feature', 'geometry', ST_AsGeoJSON(geom, 5)::jsonb,
                       'properties', jsonb_build_object('kind', kind, 'route_id', route_id, 'household_id', household_id,
                         'from', from_bringer, 'to', to_bringer) || detail))
                      FROM (SELECT * FROM navidad_geo.sim_suggestion WHERE geom IS NOT NULL
                            ORDER BY kind, id LIMIT %2$s) x)
    ) $q$, p_schema, p_max_points) INTO k;
  RETURN k;
END $$;

-- Replay: activate the scenario's zones that have appeared by minute p_minute.
CREATE OR REPLACE FUNCTION navidad_geo.set_minute(p_scenario TEXT, p_minute INT) RETURNS INT
LANGUAGE plpgsql AS $$
DECLARE n INT;
BEGIN
  UPDATE navidad_geo.zone SET active = (appears_at_min <= p_minute) WHERE scenario_id = p_scenario;
  SELECT count(*) INTO n FROM navidad_geo.zone WHERE scenario_id = p_scenario AND active;
  RETURN n;
END $$;

-- Planner parameters ----------------------------------------------------------------
-- Applied after simulate() and before suggest(): extra helpers per route (duendes / pajes)
-- add HELPER_CAPACITY of a bringer each; the boost (reindeer / camels) speeds up only the
-- travel share of the night (measured on the sampled legs). Re-runnable on the same branch.
ALTER TABLE navidad_geo.sim_route ADD COLUMN IF NOT EXISTS load_raw DOUBLE PRECISION;

CREATE OR REPLACE FUNCTION navidad_geo.apply_params(p_helpers INT DEFAULT 0, p_boost FLOAT8 DEFAULT 1)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
  helper_capacity CONSTANT FLOAT8 := 0.6;
  tf FLOAT8;
  h INT := greatest(0, least(coalesce(p_helpers, 0), 10));
  b FLOAT8 := greatest(1, least(coalesce(p_boost, 1), 2));
  k JSONB;
BEGIN
  SELECT coalesce(sum(base_s) / nullif(sum(base_s + dwell_min * 60), 0), 0.6) INTO tf
  FROM navidad_geo.sim_leg WHERE sampled AND base_s IS NOT NULL AND sim_s IS NOT NULL;
  UPDATE navidad_geo.sim_route SET load_raw = coalesce(load_raw, load_factor);
  UPDATE navidad_geo.sim_route
  SET load_factor = load_raw * (1 - tf + tf / b) / (1 + helper_capacity * h);
  UPDATE navidad_geo.sim_route
  SET late_stops = CASE WHEN load_factor > 1 THEN round((stops - unreachable) * (1 - 1 / load_factor))::int ELSE 0 END;
  SELECT value INTO k FROM navidad_geo.sim_kpi WHERE key = 'simulate';
  k := coalesce(k, '{}'::jsonb) || jsonb_build_object(
    'late_stops', (SELECT sum(late_stops) FROM navidad_geo.sim_route),
    'late_stops_without_params', (SELECT sum(CASE WHEN load_raw > 1 THEN round((stops - unreachable) * (1 - 1 / load_raw))::int ELSE 0 END)
                                  FROM navidad_geo.sim_route),
    'max_load', (SELECT round(max(load_factor)::numeric, 3) FROM navidad_geo.sim_route),
    'routes', (SELECT jsonb_agg(jsonb_build_object('route_id', route_id, 'bringer', bringer, 'stops', stops,
                     'load_factor', round(load_factor::numeric, 3), 'load_raw', round(load_raw::numeric, 3),
                     'late_stops', late_stops, 'unreachable', unreachable) ORDER BY bringer) FROM navidad_geo.sim_route),
    'params', jsonb_build_object('helpers', h, 'boost', b, 'travel_share', round(tf::numeric, 2),
                                 'helper_capacity', helper_capacity));
  INSERT INTO navidad_geo.sim_kpi VALUES ('simulate', k) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
  RETURN k;
END $$;
