-- 03 · What-if en una RAMA (nunca en production)
-- 1. Lakebase → proyecto navidad-<tu_slug> → Branches → Create branch (desde production; caducidad 1 día).
-- 2. Abre el editor SQL sobre ESA rama y ejecuta lo de abajo.
-- 3. Compara con production (mismas consultas finales) y borra la rama al terminar.
SET search_path TO navidad_tu_slug, public;

-- Activar la DANA (todas sus zonas) y simular: pgRouting, con y sin incidencia
SELECT navidad_geo.set_minute('dana_valencia', 10000) AS zonas_activas;
SELECT jsonb_pretty(navidad_geo.simulate('navidad_tu_slug', 'dana_valencia') - 'routes');
SELECT jsonb_pretty(navidad_geo.suggest('navidad_tu_slug', 'dana_valencia'));

-- Barcelona minuto a minuto: los cortes aparecen durante la noche
-- SELECT navidad_geo.set_minute('cortes_barcelona', 0);  SELECT navidad_geo.simulate('navidad_tu_slug', 'cortes_barcelona') - 'routes';
-- SELECT navidad_geo.set_minute('cortes_barcelona', 40); SELECT navidad_geo.simulate('navidad_tu_slug', 'cortes_barcelona') - 'routes';

-- ¿Qué ha cambiado? Ejecuta esto en la rama y en production
SELECT (SELECT count(*) FROM navidad_geo.zone WHERE active)              AS zonas_activas,
       (SELECT count(*) FROM navidad_geo.ways WHERE cost <> cost_base)   AS tramos_modificados,
       (SELECT count(*) FROM navidad_geo.sim_suggestion)                 AS sugerencias;
