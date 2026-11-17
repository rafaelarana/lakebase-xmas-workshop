from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

PREFIX_RE = re.compile(r"^navidad-[a-z0-9-]+$")
IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")
RESOURCE_KEY = "navidad-pg"
PRODUCTION_BRANCH = "production"
PG_DATABASE = "databricks_postgres"
PG_DATABASE_RESOURCE = "databricks-postgres"
DEFAULT_MODEL = "system.ai.claude-opus-5"
# Control-center agent: optional faster model (e.g. system.ai.claude-haiku-5-5 ≈ 10 s per review vs Opus ≈ 16 s).
# When not given, the agent uses --model, so Free Edition / other workspaces keep working.
SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = SKILL_ROOT / "templates" / "app"


class AppsEntitlementError(RuntimeError):
    """Workspace is missing Databricks Apps entitlements."""


class AppFatalError(RuntimeError):
    """Existing app is in a fatal ERROR state; it must be deleted (with the user's OK)."""


class SyncNotReadyError(RuntimeError):
    """Lakebase inbound sync is not ready; refuse to deploy the app."""


def _is_not_found(exc: BaseException) -> bool:
    if isinstance(exc, (KeyError, LookupError, FileNotFoundError)):
        return True
    try:
        from databricks.sdk.errors import NotFound

        return isinstance(exc, NotFound)
    except ImportError:
        return type(exc).__name__ == "NotFound"


def _is_permission_denied(exc: BaseException) -> bool:
    try:
        from databricks.sdk.errors import PermissionDenied

        if isinstance(exc, PermissionDenied):
            return True
    except ImportError:
        pass
    return type(exc).__name__ in {"PermissionDenied", "PermissionError"}


def _reraise_entitlement(exc: BaseException) -> None:
    if _is_permission_denied(exc):
        raise AppsEntitlementError(f"Apps entitlement missing: {exc}") from exc
    raise exc


def _await_operation(result: Any) -> Any:
    result_fn = getattr(result, "result", None)
    if callable(result_fn):
        try:
            return result_fn()
        except TypeError:
            pass
    wait = getattr(result, "wait", None)
    if not callable(wait):
        return result
    try:
        from databricks.sdk.common.lro import LroOptions

        return wait(opts=LroOptions(timeout=None))
    except TypeError:
        return wait()


def _validate_names(prefix: str, catalog: str, schema: str, project_id: str) -> None:
    if not PREFIX_RE.match(prefix):
        raise ValueError(f"prefix {prefix!r} must match {PREFIX_RE.pattern}")
    if not PREFIX_RE.match(project_id):
        raise ValueError(f"project_id {project_id!r} must match {PREFIX_RE.pattern}")
    for label, value in (("catalog", catalog), ("schema", schema)):
        if not IDENT_RE.match(value):
            raise ValueError(f"{label} {value!r} is not a valid identifier")


def app_name_for(prefix: str) -> str:
    return f"{prefix}-app"


def _postgres_paths(project_id: str) -> tuple[str, str]:
    branch = f"projects/{project_id}/branches/{PRODUCTION_BRANCH}"
    database = f"{branch}/databases/{PG_DATABASE_RESOURCE}"
    return branch, database


def _write_app_yaml(*, dest_dir: Path, model: str, schema: str, project_id: str, agent_model: str | None = None) -> None:
    branch, database = _postgres_paths(project_id)
    contents = f"""command:
  - uvicorn
  - app:app
  - --host
  - 0.0.0.0
  - --port
  - "8000"

resources:
  - name: {RESOURCE_KEY}
    postgres:
      branch: {branch}
      database: {database}
      permission: CAN_CONNECT_AND_CREATE

env:
  - name: NAVIDAD_MODEL
    value: {model}
  - name: NAVIDAD_SCHEMA
    value: {schema}
  - name: NAVIDAD_PROJECT_ID
    value: {project_id}
  - name: LAKEBASE_ENDPOINT
    value: {branch}/endpoints/primary
"""
    if agent_model:
        contents += f"""  - name: NAVIDAD_AGENT_MODEL
    value: {agent_model}
"""
    (dest_dir / "app.yaml").write_text(contents)


def _copy_template(source_template: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_template, dest_dir, dirs_exist_ok=True)


def _upload_to_workspace(client: Any, dest_dir: Path, prefix: str) -> str | None:
    workspace = getattr(client, "workspace", None)
    current_user = getattr(client, "current_user", None)
    if workspace is None or current_user is None:
        return None
    me = current_user.me()
    user = getattr(me, "user_name", None) or getattr(me, "userName", None)
    if not user:
        return None
    from plan import workspace_source_path

    root = workspace_source_path(user, prefix)
    mkdirs = getattr(workspace, "mkdirs", None)
    upload = getattr(workspace, "upload", None)
    if not callable(mkdirs) or not callable(upload):
        return None
    mkdirs(root)
    from databricks.sdk.service.workspace import ImportFormat

    for path in dest_dir.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        remote = f"{root}/{path.relative_to(dest_dir).as_posix()}"
        parent = remote.rsplit("/", 1)[0]
        mkdirs(parent)
        upload(remote, path.read_bytes(), overwrite=True, format=ImportFormat.AUTO)
    return root


def _sdk_app(app_name: str, project_id: str) -> Any:
    from databricks.sdk.service.apps import (
        App,
        AppResource,
        AppResourcePostgres,
        AppResourcePostgresPostgresPermission,
    )

    branch, database = _postgres_paths(project_id)
    return App(
        name=app_name,
        description="Navidad hackathon workbench (España)",
        resources=[
            AppResource(
                name=RESOURCE_KEY,
                postgres=AppResourcePostgres(
                    branch=branch,
                    database=database,
                    permission=AppResourcePostgresPostgresPermission.CAN_CONNECT_AND_CREATE,
                ),
            )
        ],
    )


def _create_app(apps: Any, app_name: str, project_id: str) -> Any:
    try:
        result = apps.create(app=_sdk_app(app_name, project_id))
        return _await_operation(result)
    except TypeError:
        return apps.create(name=app_name)
    except Exception as exc:
        _reraise_entitlement(exc)
        raise


def _update_app(apps: Any, app_name: str, project_id: str) -> Any:
    update = getattr(apps, "update", None)
    create_update = getattr(apps, "create_update", None)
    try:
        app = _sdk_app(app_name, project_id)
        if callable(create_update):
            return _await_operation(
                create_update(app_name=app_name, update_mask="resources,description", app=app)
            )
        if callable(update):
            return _await_operation(update(name=app_name, app=app))
    except TypeError:
        return None
    except Exception as exc:
        _reraise_entitlement(exc)
        raise
    return None


def _ensure_app_running(apps: Any, app_name: str) -> None:
    existing = _get_app(apps, app_name)
    compute = getattr(getattr(existing, "compute_status", None), "state", None)
    if compute is not None and "ACTIVE" in str(compute).upper():
        return
    start_and_wait = getattr(apps, "start_and_wait", None)
    if callable(start_and_wait):
        try:
            start_and_wait(name=app_name)
            return
        except TypeError:
            try:
                start_and_wait(app_name)
                return
            except Exception:
                return
        except Exception:
            return
    start = getattr(apps, "start", None)
    if callable(start):
        try:
            _await_operation(start(name=app_name))
        except TypeError:
            try:
                _await_operation(start(app_name))
            except Exception:
                return
        except Exception:
            return


def _deploy(apps: Any, app_name: str, source_code_path: str | None) -> Any:
    try:
        from databricks.sdk.service.apps import AppDeployment

        kwargs: dict[str, Any] = {}
        if source_code_path:
            kwargs["source_code_path"] = source_code_path
        result = apps.deploy(app_name=app_name, app_deployment=AppDeployment(**kwargs))
        return _await_operation(result)
    except TypeError:
        return apps.deploy(name=app_name)
    except Exception as exc:
        _reraise_entitlement(exc)
        raise


def _get_app(apps: Any, app_name: str) -> Any:
    try:
        return apps.get(name=app_name)
    except Exception as exc:
        if _is_permission_denied(exc):
            _reraise_entitlement(exc)
        if _is_not_found(exc):
            return None
        raise


def _app_url(app: Any, app_name: str) -> str:
    if app is None:
        return ""
    if isinstance(app, dict):
        return str(app.get("url") or "")
    return str(getattr(app, "url", "") or "")


def _pg_connect(client: Any, project_id: str) -> Any:
    """Connect to the project's primary endpoint as the current user; None if unavailable."""
    postgres = getattr(client, "postgres", None)
    if postgres is None:
        return None
    get_endpoint = getattr(postgres, "get_endpoint", None)
    gen_cred = getattr(postgres, "generate_database_credential", None)
    current_user = getattr(client, "current_user", None)
    if not callable(get_endpoint) or not callable(gen_cred) or current_user is None:
        return None
    endpoint = f"projects/{project_id}/branches/{PRODUCTION_BRANCH}/endpoints/primary"
    ep = get_endpoint(name=endpoint)
    cred = gen_cred(endpoint=endpoint)
    me = current_user.me()
    user = getattr(me, "user_name", None)
    host = getattr(getattr(ep, "status", None), "hosts", None)
    host = getattr(host, "host", None)
    token = getattr(cred, "token", None)
    if not host or not user or not token:
        return None
    import psycopg2

    conn = psycopg2.connect(
        host=host,
        user=user,
        password=token,
        dbname=PG_DATABASE,
        sslmode="require",
    )
    conn.autocommit = True
    return conn


def _wake_endpoint(client: Any, project_id: str, attempts: int = 6) -> None:
    """Make sure the endpoint is awake before an app is created against it.

    A cold endpoint makes the app's automatic Postgres grant time out, which leaves the
    new app in a fatal ERROR state that can only be fixed by deleting it.
    """
    for attempt in range(attempts):
        try:
            conn = _pg_connect(client, project_id)
            if conn is None:
                return
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                return
            finally:
                conn.close()
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(5)


def _raise_if_fatal(app: Any, app_name: str) -> None:
    compute = str(getattr(getattr(app, "compute_status", None), "state", "") or "").upper()
    deployed = getattr(app, "active_deployment", None)
    if "ERROR" in compute and deployed is None:
        message = getattr(getattr(app, "compute_status", None), "message", "") or ""
        raise AppFatalError(
            f"App {app_name} is in a fatal ERROR state and was never deployed ({message}). "
            "Ask the user before deleting it; then rerun deploy_app to re-create it."
        )


def _grant_app_select(client: Any, project_id: str, schema: str, app: Any) -> None:
    if app is None:
        return
    role = (
        app.get("service_principal_client_id")
        if isinstance(app, dict)
        else getattr(app, "service_principal_client_id", None)
    )
    if not role:
        return
    if not IDENT_RE.match(schema):
        raise ValueError(f"schema {schema!r} is not a valid identifier")
    conn = _pg_connect(client, project_id)
    if conn is None:
        return
    role_sql = '"' + str(role).replace('"', '""') + '"'
    try:
        with conn.cursor() as cur:
            cur.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role_sql}")
            cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role_sql}")
    finally:
        conn.close()


def deploy_app(
    *,
    prefix: str,
    catalog: str,
    schema: str,
    project_id: str,
    model: str,
    source_template: Path,
    agent_model: str | None = None,
    dest_dir: Path,
    client: Any,
    synced_ready: bool,
) -> dict:
    """Refuse if not synced_ready. Copy template to dest_dir (exist_ok, overwrite files).

    Write generated app.yaml with app name `{prefix}-app`, env NAVIDAD_MODEL, NAVIDAD_SCHEMA.
    Create or update the Databricks App in place. Return {app_name, url}.
    """
    if not synced_ready:
        raise SyncNotReadyError(
            "Lakebase inbound sync is not ready; refuse to deploy the app until synced tables are ready"
        )
    _validate_names(prefix, catalog, schema, project_id)
    source_template = Path(source_template)
    dest_dir = Path(dest_dir)
    _copy_template(source_template, dest_dir)
    _write_app_yaml(dest_dir=dest_dir, model=model, schema=schema, project_id=project_id, agent_model=agent_model)
    app_name = app_name_for(prefix)
    apps = client.apps
    existing = _get_app(apps, app_name)
    created = False
    if existing is None:
        _wake_endpoint(client, project_id)
        existing = _create_app(apps, app_name, project_id)
        created = True
    else:
        _raise_if_fatal(existing, app_name)
    source_code_path = _upload_to_workspace(client, dest_dir, prefix)
    if not source_code_path:
        source_code_path = getattr(existing, "default_source_code_path", None)
        if isinstance(existing, dict):
            source_code_path = existing.get("default_source_code_path")
    _ensure_app_running(apps, app_name)
    _deploy(apps, app_name, source_code_path)
    refreshed = _get_app(apps, app_name) or existing
    _grant_app_select(client, project_id, schema, refreshed)
    return {
        "status": "ok",
        "app_name": app_name,
        "url": _app_url(refreshed, app_name),
        "dest_dir": str(dest_dir),
        "created": created,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy the Navidad app template and deploy a Databricks App")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--agent-model", default=None, help="model for the control-center agent (default: --model)")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--source-template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--dest-dir", type=Path, default=None)
    parser.add_argument("--synced-ready", action="store_true", default=True)
    parser.add_argument("--not-synced-ready", action="store_true", default=False)
    args = parser.parse_args(argv)
    dest_dir = args.dest_dir or (SKILL_ROOT / "copied-apps" / args.prefix)
    synced_ready = not args.not_synced_ready
    from databricks.sdk import WorkspaceClient

    client = WorkspaceClient(profile=args.profile)
    try:
        result = deploy_app(
            prefix=args.prefix,
            catalog=args.catalog,
            schema=args.schema,
            project_id=args.project_id,
            model=args.model,
            agent_model=args.agent_model,
            source_template=args.source_template,
            dest_dir=dest_dir,
            client=client,
            synced_ready=synced_ready,
        )
        print(json.dumps({"step": "deploy_app", **result}))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "step": "deploy_app",
                    "status": "error",
                    "error": str(exc),
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
