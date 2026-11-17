# Lakebase SQL (agent)

Synced tables are the **base**. New objects go **beside** them. Never mutate `*_sync`.

## Names and session cache

Resolve once at the start of Lakebase work and reuse:

- `project_name`: remembered Lakebase project (e.g. `navidad-<slug>-2`)
- `branch_name`: `production` for exploration; the feature's `dev-*` branch for any build work (`dev-branch-workflow.md`)
- `database`: `databricks_postgres` (hyphen form `databricks-postgres` is the Apps resource path only)
- Schema: `navidad_<tag>` (or the remembered schema)
- Endpoint: `projects/{project_name}/branches/{branch_name}/endpoints/primary` (each branch has its own host)
- `host`: `WorkspaceClient().postgres.get_endpoint(name=endpoint).status.hosts.host`

Base tables (read-only): `households_sync`, `people_sync`, `preferences_sync`, `journeys_sync`.

If host/project lookup fails, stop. Do not invent a Spark fallback.

## Execution routing (Genie Code)

| When | How |
| --- | --- |
| Exploration, aggregations, data checks | `executeLakebasePostgresSql` with `read_only: true` |
| DDL: `CREATE EXTENSION postgis`, `CREATE TABLE/VIEW`, `GRANT` | `executeLakebasePostgresSql` with `read_only: false` |
| Heavy Python (solvers, ML) | Notebook + `psycopg2` (below) |
| User-facing results | Databricks App |

In Genie Code chat, **prefer `executeLakebasePostgresSql`** for all SQL. Users cannot run PostGIS themselves. Show exploration results in chat first; when they like it, build it into the app.

Required tool fields: `project_name`, `branch_name` (`production`, or the `dev-*` branch for feature work), `database=databricks_postgres`, and the `host` **of that branch** (discover via SDK or memory). Writes for a new feature go to the dev branch first, and reach `production` only through the approved migration (`dev-branch-workflow.md`).

## Notebook fallback (Python-only work)

First cell, before any variables:

```python
%pip install psycopg2-binary "databricks-sdk==0.122.0"
dbutils.library.restartPython()
```

```python
import psycopg2
from psycopg2.extras import RealDictCursor
from databricks.sdk import WorkspaceClient

project_id = "<remembered-project>"
schema = "<remembered-schema>"
endpoint = f"projects/{project_id}/branches/production/endpoints/primary"
w = WorkspaceClient()
ep = w.postgres.get_endpoint(name=endpoint)
cred = w.postgres.generate_database_credential(endpoint=endpoint)
me = w.current_user.me()
conn = psycopg2.connect(
    host=ep.status.hosts.host,
    user=me.user_name,
    password=cred.token,
    dbname="databricks_postgres",
    sslmode="require",
)
conn.autocommit = True
```

Quote known-safe identifiers (`[a-z0-9_]+`). Bind values with `%s`.

## Presence check

```sql
SELECT table_name
FROM information_schema.tables
WHERE table_schema = '<schema>'
  AND table_name IN (
    'households_sync', 'people_sync', 'preferences_sync', 'journeys_sync'
  );
```

Need all four. Also confirm `{catalog}.{schema}` in Unity Catalog.

## Orientation queries (chat, before design)

```sql
SELECT year, campaign, count(*) AS stops, count(DISTINCT route_id) AS routes
FROM <schema>.journeys_sync
GROUP BY year, campaign ORDER BY year, campaign;

SELECT city, autonomous_community, count(*) AS households
FROM <schema>.households_sync
GROUP BY city, autonomous_community ORDER BY households DESC;

SELECT autonomous_community, nochebuena_bringer, count(*) AS households
FROM <schema>.households_sync
GROUP BY autonomous_community, nochebuena_bringer
ORDER BY autonomous_community, households DESC;

SELECT bringer, count(*) AS stops
FROM <schema>.journeys_sync
WHERE year = (SELECT max(year) FROM <schema>.journeys_sync) AND campaign = 'reyes'
GROUP BY bringer;
```

Default to the latest `year` and Reyes night. Tell the user which year and night you are using.

## New objects + GRANT checklist

After every `CREATE TABLE` or `CREATE VIEW`:

1. `GRANT SELECT ON {schema}.{new_table} TO "<app-sp-client-id>";`
2. If needed: `GRANT USAGE ON SCHEMA {schema} TO "<app-sp-client-id>";`
3. On the dev branch: `SELECT has_table_privilege('<app-sp-client-id>', '{schema}.{new_table}', 'SELECT')`. After the migration to `production`: hit the app endpoint that reads it before telling the user it is ready.

App SP:

```python
WorkspaceClient().apps.get(name="<app-name>").service_principal_client_id
```

```sql
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE <schema>.person_segments AS
SELECT p.person_id,
       CASE
         WHEN pr.naughty_nice_score >= 80 THEN 'muy_bueno'
         WHEN pr.naughty_nice_score <= 20 THEN 'carbon'
         ELSE 'regular'
       END AS segment_label,
       pr.gift_affinity,
       pr.naughty_nice_score
FROM <schema>.people_sync p
JOIN <schema>.preferences_sync pr ON pr.person_id = p.person_id;

GRANT SELECT ON <schema>.person_segments TO "<app-sp-client-id>";
```

Geometry: `ST_SetSRID(ST_MakePoint(h.lon, h.lat), 4326)` — **lon first**. Never `ALTER` `*_sync`.

## Forbidden on synced tables

```sql
ALTER TABLE <schema>.households_sync ...
UPDATE <schema>.preferences_sync ...
DELETE FROM <schema>.journeys_sync ...
INSERT INTO <schema>.people_sync ...
```
