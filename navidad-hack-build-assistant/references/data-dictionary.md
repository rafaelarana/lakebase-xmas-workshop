# Navidad hackathon data dictionary

Spain-only synthetic data (peninsula, Balearics, Canaries). Geo lives on the household: that is the delivery point. People replace a locations table. Journeys are the fact table.

Two gift nights per campaign year:

- **Nochebuena** (night of 24 Dec): the household's regional gift-bringer — Papá Noel, Olentzero (País Vasco, Navarra), Tió de Nadal (Cataluña) or Apalpador (Galicia). Some households get nobody on Nochebuena.
- **Reyes** (night of 5→6 Jan of the *next* calendar year): Melchor, Gaspar and Baltasar. Most households get the Kings.

`year` is always the **campaign year**: the 2025 campaign is Nochebuena 2025-12-24 and Reyes 2026-01-06.

Grain:

- One household is one delivery point (coordinates, city, province, comunidad).
- People are 1:N to households.
- Preferences are 1:1 with people (naughty/nice is per person, not per household).
- One journey row is one household visit by one gift-bringer on one night. The bringer delivers for everyone in the house. Do not emit person-level journey rows.

## Defaults

| Knob | Value |
| --- | --- |
| Seed | `20260106` (same seed → same bytes) |
| `households` | 500,000 |
| `people` | 2,000,000 (~4 per household, varying 1–8) |
| `preferences` | 2,000,000 (1:1 with people) |
| `journeys` | ~4,000,000 stops over 5 campaigns (2021–2025) |

Per campaign, ~64% of households get a Nochebuena visit and ~95% get Reyes, so each household averages ~1.6 visits a year, plus a tiny seeded revisit fraction.

## Spanish cities

21 cities, weighted by municipal population. City centres plus tight jitter (σ ≈ 2.5 km, capped at ~8 km). Coastal cities use land masks derived from Natural Earth (`scripts/es_land.py`), so no household sits in the sea.

Madrid, Barcelona, Valencia, Zaragoza, Sevilla, Málaga, Murcia, Palma, Las Palmas de Gran Canaria, Alicante, Bilbao, Córdoba, Valladolid, Vigo, Gijón, Vitoria-Gasteiz, A Coruña, Granada, Santa Cruz de Tenerife, Pamplona, San Sebastián.

Madrid is the largest (~28% of households); San Sebastián is the smallest.

**Canarias:** Las Palmas and Santa Cruz are ~1,700 km from Madrid and one hour behind (`Atlantic/Canary`, UTC+0 in winter; the rest is `Europe/Madrid`, UTC+1). Maps need a separate Canarias view or inset.

## Routes and revisits

`route_id` identifies one **(campaign, city, bringer)** sub-tour and is stable across years and seeds:

- Nochebuena: one route per city and regional bringer (e.g. Bilbao has an Olentzero route and a Papá Noel route).
- Reyes: each King owns a fixed 120° sector of every city, measured from the city centre (Melchor 0–120°, Gaspar 120–240°, Baltasar 240–360°, counter-clockwise from east). So the seed data is **already a three-agent fleet** per city. Coastal cities lose part of a sector to the sea, so the Kings' workloads are unbalanced — on purpose.

Cities are toured east → west, following midnight, with the Canaries last. Within a route, stops follow a Hilbert space-filling curve over the route's area (consecutive stops are neighbours: leg p50 ≈ 28 m; good, not optimal — street-aware re-sequencing is still a hack track). About 0.1 % of stops are revisits of a household visited up to 30 stops earlier (`is_revisit`). No route crosses cities.

Arrival times start at 22:00 local (Nochebuena) or 01:00 local (Reyes), plus one minute per city in tour order. Each route is then **compressed to fit the night** ("magia navideña": Nochebuena ends by ~07:00, Reyes by ~08:00 local). `dwell_minutes` is the uncompressed effort at the stop, so gaps between `arrived_at_utc` values are shorter than dwell + travel on big routes.

A seeded ~0.1% of stops are backtracks to a household already visited earlier on the same route (0–30 stops before), with `is_revisit` = true.

Primary keys for Lakebase inbound sync: `household_id`, `person_id`, `person_id`, and composite (`year`, `campaign`, `route_id`, `stop_number`) for journeys. There is no `journey_id` column.

## Serving

Query **Lakebase** from the app for serving. Use **Unity Catalog** for Spark-scale analysis. Always use this dictionary for column names.

Inbound synced Postgres tables live in schema `{user_schema}` with a `_sync` suffix: `households_sync`, `people_sync`, `preferences_sync`, `journeys_sync`. Example: `navidad_<slug>.households_sync`. Unity Catalog table names do **not** have the suffix.

Categorical values are lowercase ASCII codes in Spanish (`juguetes`, `zapatos`, `papa_noel`). Map them to display labels with accents in the app (`Papá Noel`, `Tió de Nadal`).

PII: synthetic only. Names are random combinations of common Spanish given names and surnames. No real addresses. Coordinates are city-jittered, not address-level.

## `households`

Dimension. One row per delivery point.

| column | type | pk/fk | description |
| --- | --- | --- | --- |
| `household_id` | BIGINT | PK | Synthetic household key |
| `label` | STRING | | `Familia <apellido1> <apellido2>` |
| `city` | STRING | | City from the city set (with accents, e.g. `Málaga`) |
| `province` | STRING | | Province of the city (e.g. `Bizkaia`, `Illes Balears`) |
| `autonomous_community` | STRING | | Comunidad autónoma (e.g. `País Vasco`, `Canarias`) |
| `lat` | DOUBLE | | WGS84 latitude, city centre plus tight jitter, on land |
| `lon` | DOUBLE | | WGS84 longitude, city centre plus tight jitter, on land |
| `postcode_area` | STRING | | First two digits of the código postal = province code (`"08"`, keep the leading zero) |
| `timezone` | STRING | | `Europe/Madrid` or `Atlantic/Canary` |
| `household_size` | INT | | Count of people in the household (1–8) |
| `first_seen_year` | INT | | First campaign year this household appears |
| `nochebuena_bringer` | STRING | | `papa_noel`, `olentzero`, `tio`, `apalpador`, or `none` (no Nochebuena visit) |
| `visits_reyes` | BOOLEAN | | True if the Reyes Magos visit this household (always true when `nochebuena_bringer` = `none`) |

## `people`

Dimension. 1:N to households.

| column | type | pk/fk | description |
| --- | --- | --- | --- |
| `person_id` | BIGINT | PK | Synthetic person key |
| `household_id` | BIGINT | FK → `households.household_id` | Household this person lives in |
| `given_name` | STRING | | Given name; ~35% regional (Catalan, Basque, Galician, Balearic) in those communities |
| `age_years` | INT | | Age in years |
| `role` | STRING | | `child` or `adult` (`child` if `age_years` < 18, else `adult`) |

## `preferences`

Dimension. 1:1 with people. Naughty/nice is per person.

| column | type | pk/fk | description |
| --- | --- | --- | --- |
| `person_id` | BIGINT | PK/FK → `people.person_id` | Person this preference row belongs to |
| `naughty_nice_score` | INT | | Integer score from 0 to 100. Low scores get carbón (coal) from the Reyes |
| `gift_affinity` | STRING | | `juguetes`, `libros`, `tecnologia`, `ropa`, `dulces`, `musica`, `deporte` |
| `dietary_notes` | STRING | | `ninguna`, `frutos_secos`, `gluten`, `lactosa`, `vegetariana`, `vegana` |
| `window_policy` | STRING | | Where gifts are left: `zapatos` (shoes by the door, Reyes tradition), `balcon`, `ventana`, `chimenea`, `magia` |
| `favourite_king` | STRING | | `melchor`, `gaspar` or `baltasar` (the King the letter is addressed to) |
| `last_updated_year` | INT | | Campaign year this preference row was last updated |

## `journeys`

Fact. Grain: one row = one stop. A night's tour is `year` + `campaign` + `route_id` ordered by `stop_number`.

| column | type | pk/fk | description |
| --- | --- | --- | --- |
| `year` | INT | PK (composite) | Campaign year (Reyes rows arrive in January of `year` + 1) |
| `campaign` | STRING | PK (composite) | `nochebuena` or `reyes` |
| `route_id` | BIGINT | PK (composite) | Stable id of the (campaign, city, bringer) sub-tour |
| `stop_number` | INT | PK (composite) | Order of this stop within (`year`, `campaign`, `route_id`) |
| `household_id` | BIGINT | FK → `households.household_id` | Household visited |
| `bringer` | STRING | | `papa_noel`, `olentzero`, `tio`, `apalpador`, `melchor`, `gaspar`, `baltasar` |
| `arrived_at_utc` | TIMESTAMP | | Arrival time in UTC (compressed to fit the night; see above) |
| `dwell_minutes` | INT | | Minutes of effort at the stop (2–8) |
| `is_revisit` | BOOLEAN | | True for the seeded ~0.1% backtrack stops |

## Incident track: `navidad_geo` (Postgres only, optional)

Created by the optional incident step (`provision_incidents`). It lives **only in Lakebase**, in a native schema, because synced tables cannot carry PostGIS geometry. There is no Unity Catalog copy. It covers Madrid, Barcelona and Valencia. Road data © OpenStreetMap contributors (ODbL): show the attribution on every map.

| table | grain | key columns |
| --- | --- | --- |
| `navidad_geo.ways` | One routable road edge (OSM way split at intersections) | `id`, `city`, `source`, `target`, `highway`, `main` (motorway/trunk/primary/secondary), `length_m`, `cost_base` / `reverse_cost_base` (seconds at night free-flow speed; `-1` = one-way), `cost` / `reverse_cost` (after active zones; `-1` = closed), `geom` LineString 4326 |
| `navidad_geo.vertices` | Road intersection (OSM node id) | `id`, `city`, `main_component` (in the city's largest strongly connected component), `geom` |
| `navidad_geo.household_node` | Household → nearest well-connected vertex | `household_id`, `city`, `node_id`, `snap_m` |
| `navidad_geo.scenario` | Incident scenario preset | `scenario_id` (`filomena_madrid`, `dana_valencia`, `cortes_barcelona`), `city`, `campaign`, `year`, `title`, `story` |
| `navidad_geo.zone` | Incident zone (red polygon) | `zone_id`, `scenario_id`, `kind`, `label`, `closed`, `main_only`, `factor_main`, `factor_local`, `dwell_factor`, `appears_at_min`, `active`, `geom` MultiPolygon |
| `navidad_geo.sim_leg` | One leg (stop → next stop) of the simulated night | `route_id`, `stop_number`, `hh_from`, `hh_to`, `node_from`, `node_to`, `sampled`, `base_s`, `sim_s`, `status` (`ok`, `lento`, `desvio`, `sin_ruta`, `inaccesible`, `sin_evaluar`) |
| `navidad_geo.sim_route` | One route of the simulated night | `route_id`, `bringer`, `stops`, `affected_legs`, `ratio_affected`, `load_factor`, `late_stops`, `unreachable` |
| `navidad_geo.sim_suggestion` | One suggestion | `kind` (`desvio`, `punto_recogida`, `refuerzo`, `resecuenciar`), `route_id`, `household_id`, `from_bringer`, `to_bringer`, `detail` jsonb, `geom` |
| `navidad_geo.applied_plan` | Suggestions copied to production by "apply plan" | `plan_id` (= branch), `scenario_id`, `kind`, … |

On **production**, zones are inactive and `cost = cost_base`. Simulations activate zones and write `sim_*` **on a child branch** only.
