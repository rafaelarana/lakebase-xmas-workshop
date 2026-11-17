# Databricks App deploy (agent)

Edit the **existing** copied app in workspace, then deploy in place. Never create a second app. Deploy a feature only after its Lakebase migration has been approved and applied to `production` (`dev-branch-workflow.md`). The `runDatabricksCli` tool may block `apps get`; use the SDK.

```python
from datetime import timedelta
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.apps import AppDeployment

w = WorkspaceClient()
wait = w.apps.deploy(
    app_name="<app-name>",
    app_deployment=AppDeployment(
        source_code_path="<absolute-workspace-path>",
    ),
)
result = wait.result(timeout=timedelta(minutes=10))
```

`source_code_path` belongs on `AppDeployment`, not as a kwarg to `deploy()`. Timeout is `timeout=timedelta(...)` on `.result()`, not `timeout_minutes`.

After deploy:

- Give the user the app URL.
- Ask them to open it in a browser and confirm it loads (especially map tiles).
- Do **not** test the app from a notebook (Apps sit behind OAuth).
- If tiles fail, switch the basemap to OSM standard (`references/hack-routes.md`).
