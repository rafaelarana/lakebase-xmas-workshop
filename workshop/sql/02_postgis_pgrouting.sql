-- 02 · PostGIS + pgRouting (solo lectura; necesita la pista de incidencias de 00_workbench)
SET search_path TO navidad_tu_slug, public;

-- La red de carreteras cargada (© OpenStreetMap contributors, ODbL)
SELECT city, edges, vertices, osm_timestamp FROM navidad_geo.osm_meta;

-- KNN: los 5 hogares más cercanos a la Plaza del Ayuntamiento de Valencia
SELECT h.household_id, h.label,
       round(ST_Distance(ST_SetSRID(ST_MakePoint(h.lon, h.lat), 4326)::geography,
                         ST_SetSRID(ST_MakePoint(-0.3763, 39.4699), 4326)::geography)) AS metros
FROM households_sync h WHERE h.city = 'Valencia'
ORDER BY ST_SetSRID(ST_MakePoint(h.lon, h.lat), 4326) <-> ST_SetSRID(ST_MakePoint(-0.3763, 39.4699), 4326)
LIMIT 5;

-- pgRouting: tiempo por carretera entre las dos primeras paradas de Papá Noel en Valencia
WITH legs AS (
  SELECT a.node_id AS src, b.node_id AS dst
  FROM journeys_sync j1
  JOIN journeys_sync j2 ON j2.year = j1.year AND j2.campaign = j1.campaign AND j2.route_id = j1.route_id
                       AND j2.stop_number = j1.stop_number + 1
  JOIN navidad_geo.household_node a ON a.household_id = j1.household_id
  JOIN navidad_geo.household_node b ON b.household_id = j2.household_id
  WHERE j1.year = (SELECT max(year) FROM journeys_sync) AND j1.campaign = 'nochebuena'
    AND a.city = 'Valencia' AND j1.stop_number = 1
  LIMIT 1)
SELECT round((agg_cost / 60)::numeric, 1) AS minutos
FROM legs, pgr_dijkstraCost(
  'SELECT id, source, target, cost_base AS cost, reverse_cost_base AS reverse_cost FROM navidad_geo.ways WHERE city = ''Valencia''',
  legs.src, legs.dst, true);

-- Los escenarios preparados (en producción, todas las zonas están INACTIVAS)
SELECT scenario_id, city, campaign, title,
       (SELECT count(*) FROM navidad_geo.zone z WHERE z.scenario_id = s.scenario_id) AS zonas
FROM navidad_geo.scenario s;
