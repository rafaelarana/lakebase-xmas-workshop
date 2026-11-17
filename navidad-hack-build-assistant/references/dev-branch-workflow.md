# Branch-first development (agent)

Every **new feature or implementation** is built and tested on its own Lakebase **dev branch**, never directly on `production`. Show the test results, ask for approval, and only then migrate the changes to `production` and deploy the app.

Exempt: read-only exploration in chat (`read_only: true` on `production`), and incident what-ifs, which have their own `sim-*` branches (`hack-incidents.md`).

## 1 · Create the dev branch

One branch per feature, named `dev-<feature-slug>-<MMDD-HHMM>`. Make it a child of `production`, with a TTL of 24 h. The branch is a copy-on-write copy, ready in seconds, with its own `primary` compute. Synced tables appear on it as writable copies.

Genie Code has no branch tool, so use a notebook cell. The first cell must pin the SDK, as in `lakebase-sql.md`:

```python
import datetime as dt
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.postgres import Branch, BranchSpec
from google.protobuf.duration_pb2 import Duration

w = WorkspaceClient()
project = "projects/<remembered-project>"
dev_branch = f"dev-<feature-slug>-{dt.datetime.utcnow():%m%d-%H%M}"
w.postgres.create_branch(
    parent=project, branch_id=dev_branch,
    branch=Branch(spec=BranchSpec(source_branch=f"{project}/branches/production",
                                  ttl=Duration(seconds=24 * 3600))),
).wait()
host = w.postgres.get_endpoint(name=f"{project}/branches/{dev_branch}/endpoints/primary").status.hosts.host
```

From then on, call `executeLakebasePostgresSql` with `branch_name=<dev_branch>` and **that branch's** `host` for all feature work. Tell the user in one line: `Trabajo en la rama dev-… (copia de production); production no se toca hasta que lo apruebes.`

Keep at most **2** dev branches at a time. If one for the same feature already exists, reuse it. If the user starts a different feature, offer to discard the older branch.

## 2 · Write the change as a migration script

Put every Lakebase change for the feature in one ordered, **idempotent** script in the app source: `migrations/NNN_<feature-slug>.sql`. Use `CREATE … IF NOT EXISTS` / `CREATE OR REPLACE`, the `GRANT`s to the app SP, and indexes.

- Run **the file** on the dev branch. What is tested must be exactly what gets migrated.
- Derived data is rebuilt from `*_sync` inside the script, so it is not copied from the branch.
- Never `ALTER`/`UPDATE`/`DELETE`/`INSERT` `*_sync`. That holds on the branch too.
- If a step cannot run inside a transaction (`CREATE EXTENSION`, `CREATE INDEX CONCURRENTLY`), put it at the top of the file in a separate block marked `-- no transaction`.

## 3 · Test on the branch

Run the checks with `read_only: true` on the dev branch. At a minimum:

| Check | How |
| --- | --- |
| Objects created | `information_schema` / `pg_class`: every table, view and function from the script exists |
| Data sanity | Row counts vs the source (e.g. households in the city), no `NULL` keys or geometries, value ranges |
| Feature queries | Run the **exact** SQL of each new or changed app endpoint, with realistic parameters: rows returned, a sample, time in ms |
| Performance | `EXPLAIN (ANALYZE, BUFFERS)` on the heaviest query; flag sequential scans on `journeys_sync` |
| App SP access | `SELECT has_table_privilege('<app-sp-client-id>', '<schema>.<obj>', 'SELECT')` for each new object |
| Sync tables intact | Counts of `*_sync` on the branch = on `production` |
| Idempotent | Run the script a second time on the branch: no errors, same counts |

The app still points at `production`, so it cannot show branch data. Show the results in chat. For map features, add a small GeoJSON sample or summary.

## 4 · Show results and ask for approval

Reply with this structure, in the user's language:

1. **Funcionalidad** and **rama** (`dev-…`).
2. **Cambios** that the migration will apply: objects to create or replace, with row counts.
3. **Pruebas**: the check table with ✓ / ✗, the numbers, and the time per endpoint query.
4. **Impacto en production**: estimated time, locks (if any), whether the app is redeployed, and what users will see.
5. **Vuelta atrás**: the `DROP` list for the new objects, plus a point in time for a recovery branch (PITR) taken just before the migration.
6. Then ask: `¿Migro estos cambios a production? (sí / ajustar / descartar)`.

**Stop and wait for an explicit yes.** Silence, a question, or "looks good" about one detail is not approval. If any check is ✗, say so and propose a fix instead of asking to migrate.

- **ajustar** → change the script, rerun it on the same branch, and run the tests and approval again.
- **descartar** → delete the branch (`w.postgres.delete_branch(name=…).wait()`). Production is unchanged.

## 5 · Migrate to production (only after "sí")

Lakebase has no branch merge. Migrating means **replaying the tested script** on `production`:

1. Record the restore point: `SELECT now()` on `production`. Tell the user the value; it is the PITR moment if they ever need to undo.
2. Run the `-- no transaction` block, then the rest of the script **in one transaction** (`BEGIN … COMMIT`) on `production` (`read_only: false`). If anything fails, it rolls back: report the error, fix it on the dev branch, and ask again.
3. Re-run the check table on `production`. Counts can differ slightly if the synced tables refreshed since the branch was created; explain any difference.
4. Deploy the app changes now (`app-deploy.md`), not before. Hit the new endpoint from the app before telling the user it is ready.
5. Delete the dev branch and confirm: `Migrado a production a las HH:MM (punto de restauración …). Rama dev-… borrada.`

Data that only exists on the branch (rows the user entered by hand) is the exception. Copy it with `COPY … TO STDOUT` on the branch → `COPY … FROM STDIN` into a temp table on `production` → `INSERT … ON CONFLICT` in the same transaction. List those rows in the approval message.

## App-only changes

If a feature changes only app code (no Lakebase objects or data), still run its new SQL on a dev branch. Show the results and ask before deploying. The migration step is then just the deploy.
