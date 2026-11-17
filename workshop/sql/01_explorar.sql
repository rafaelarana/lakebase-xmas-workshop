-- 01 · Explorar el Taller de Navidad en Lakebase (solo lectura)
-- Editor SQL de Lakebase → proyecto navidad-<tu_slug> → rama production → base databricks_postgres.
-- Sustituye navidad_tu_slug por tu esquema (navidad_<tu_slug>).
SET search_path TO navidad_tu_slug, public;

-- Las dos noches por campaña (Nochebuena y Reyes) y cuántas paradas tiene cada una
SELECT year AS campaña, campaign AS noche, count(*) AS paradas, count(DISTINCT route_id) AS rutas
FROM journeys_sync GROUP BY 1, 2 ORDER BY 1 DESC, 2 LIMIT 6;

-- Las ciudades más grandes y quién trae los regalos en Nochebuena
SELECT city, count(*) AS hogares,
       count(*) FILTER (WHERE nochebuena_bringer = 'papa_noel') AS papa_noel,
       count(*) FILTER (WHERE nochebuena_bringer = 'tio')       AS tio,
       count(*) FILTER (WHERE nochebuena_bringer = 'olentzero') AS olentzero
FROM households_sync GROUP BY city ORDER BY hogares DESC LIMIT 8;

-- Reyes: los tres Reyes como flota, con un sector de 120° por ciudad
SELECT h.city, j.bringer AS rey, count(*) AS paradas, round(avg(j.dwell_minutes), 1) AS min_por_parada
FROM journeys_sync j JOIN households_sync h USING (household_id)
WHERE j.year = (SELECT max(year) FROM journeys_sync) AND j.campaign = 'reyes' AND h.city IN ('Valencia', 'Barcelona')
GROUP BY 1, 2 ORDER BY 1, 2;
