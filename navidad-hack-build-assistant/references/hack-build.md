# Hack-time build playbook (agent)

Collaborative partner, not a silent builder. Read `references/data-dictionary.md` and `references/lakebase-sql.md`. Then:

- Route / map / GIS / King or bringer path / fleet → `references/hack-routes.md`
- Segmentation / clustering / naughty-nice (carbón) cohorts / regional traditions → `references/hack-segmentation.md`
- Incidents / storm / flood / street closures / what-if on a Lakebase branch / pgRouting → `references/hack-incidents.md`
- Real time / live deliveries / Zerobus / streaming / Lakebase sink / control center / exceptions agent → `references/hack-control-center.md`
- Search / find a family / find a duende or paje / BM25 / Lakebase Search / typeahead → `references/hack-search.md`

Every new feature: dev branch → migration script → tests → approval → migration (`references/dev-branch-workflow.md`).

## Where to edit

- App source: `/Workspace/Users/<user>/navidad-hackathon/<app-name>/` (remembered path; may include suffixes).
- Lakebase changes: `migrations/NNN_<feature-slug>.sql` in the app source, run on the dev branch first.
- After file changes, deploy in place (`references/app-deploy.md`), after the migration to `production` is approved and applied. Never create a second app.

## Which engine

| Work | Engine |
| --- | --- |
| Reads of seed facts | Lakebase `{schema}.*_sync` |
| New segments, derived facts, GIS | **New** Lakebase tables/views |
| Interactive map | Lakebase PostGIS + app |
| LLM copy | Existing Gateway helper |

Do **not** default to Spark. Do not query Spark from the app request path. Do not `INSERT … VALUES` millions of rows.

## Schema traps

- App SQL uses `*_sync` names. Journeys have no coords — join households.
- Naughty/nice is per person. One journey row = one household visit on one night; filter `campaign` too.
- Reyes rows for campaign `year` arrive in January of `year` + 1. Show local times (`timezone`), not UTC.
- Do not `ALTER TABLE …_sync`.

## App patterns

Reuse `execute_sql_from_pg`. Check `references/app-endpoints.md` first. Dashboard layout by default (title, KPIs, contained map), all copy in Spanish. Península / Canarias toggle. OSM muted tiles (`references/hack-routes.md`). Filter and `LIMIT` before GIS.

## Post-CREATE checklist

After every `CREATE TABLE` or `CREATE VIEW` in Lakebase:

1. `GRANT SELECT ON {schema}.{new_table} TO "<app-sp-client-id>";`
2. If a new schema: `GRANT USAGE ON SCHEMA {schema} TO ...`
3. Check `has_table_privilege` on the dev branch; after the migration, verify from the app endpoint before telling the user it is ready.

App SP: `WorkspaceClient().apps.get(name="<app-name>").service_principal_client_id`.
