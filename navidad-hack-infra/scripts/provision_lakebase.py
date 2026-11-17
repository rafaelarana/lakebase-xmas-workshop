from __future__ import annotations

import argparse
import json
import time
import re
import sys
from datetime import timedelta
from typing import Any

# Lakebase project IDs allow hyphens only: ^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$
PROJECT_ID_RE = re.compile(r"^navidad-[a-z0-9-]+$")
IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")
TABLES = ("households", "people", "preferences", "journeys")
PRIMARY_KEYS = {
    "households": ["household_id"],
    "people": ["person_id"],
    "preferences": ["person_id"],
    "journeys": ["year", "campaign", "route_id", "stop_number"],
}
WAIT_TIMEOUT = timedelta(minutes=30)
PRODUCTION_BRANCH = "production"
PG_DATABASE = "databricks_postgres"


class ProjectMissing(RuntimeError):
    """Lakebase project does not exist yet."""


class SyncedTableMissing(RuntimeError):
    """Inbound synced table does not exist yet."""


class LakebaseEntitlementError(RuntimeError):
    """Workspace is missing Lakebase entitlements."""


def _is_not_found(exc: BaseException) -> bool:
    if isinstance(exc, (ProjectMissing, SyncedTableMissing, KeyError, LookupError)):
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
        raise LakebaseEntitlementError(f"Lakebase entitlement missing: {exc}") from exc
    raise exc


def _await_operation(result: Any) -> Any:
    wait = getattr(result, "wait", None)
    if not callable(wait):
        return result
    try:
        from databricks.sdk.common.lro import LroOptions

        return wait(opts=LroOptions(timeout=WAIT_TIMEOUT))
    except TypeError:
        return wait()


def _validate_names(catalog: str, schema: str, project_id: str) -> None:
    if not PROJECT_ID_RE.match(project_id):
        raise ValueError(
            f"project_id {project_id!r} must match {PROJECT_ID_RE.pattern}"
        )
    for label, value in (("catalog", catalog), ("schema", schema)):
        if not IDENT_RE.match(value):
            raise ValueError(f"{label} {value!r} is not a valid identifier")


# Project defaults. FREE_EDITION matches Databricks Free Edition: 1 CU compute cap and a
# 300 s minimum auto-suspend for the workspace tier.
class ProjectSoftDeleted(RuntimeError):
    pass


PROJECT_DEFAULTS = {"suspend_s": 60, "min_cu": 0.5, "max_cu": 8.0, "history_s": 604_800}
FREE_EDITION = {"suspend_s": 300, "min_cu": 0.5, "max_cu": 1.0, "history_s": 604_800}


def project_settings(free_edition: bool = False, **overrides: Any) -> dict:
    """Defaults (or the Free Edition preset) with explicit overrides; validates min <= max."""
    out = dict(FREE_EDITION if free_edition else PROJECT_DEFAULTS)
    out.update({k: v for k, v in overrides.items() if v is not None})
    unknown = set(out) - set(PROJECT_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown project settings: {sorted(unknown)}")
    if out["min_cu"] > out["max_cu"]:
        raise ValueError(f"min_cu {out['min_cu']} > max_cu {out['max_cu']}")
    return out


def _build_project(project_id: str, *, suspend_s: int, min_cu: float, max_cu: float, history_s: int) -> Any:
    from databricks.sdk.service.postgres import (
        Duration,
        Project,
        ProjectCustomTag,
        ProjectDefaultEndpointSettings,
        ProjectSpec,
    )

    return Project(
        spec=ProjectSpec(
            display_name=f"Navidad hackathon — {project_id}",
            pg_version=17,
            history_retention_duration=Duration(seconds=history_s),
            custom_tags=[
                ProjectCustomTag(key="purpose", value="navidad-hack-infra"),
            ],
            default_endpoint_settings=ProjectDefaultEndpointSettings(
                autoscaling_limit_min_cu=min_cu,
                autoscaling_limit_max_cu=max_cu,
                suspend_timeout_duration=Duration(seconds=suspend_s),
            ),
        )
    )


PROJECT_PROPAGATION_ATTEMPTS = 8
PROJECT_PROPAGATION_SLEEP_S = 10

SYNC_MODES = ("CONTINUOUS", "TRIGGERED", "SNAPSHOT")


def _build_synced_table(*, catalog: str, schema: str, table: str, project_id: str, sync_mode: str = "SNAPSHOT") -> Any:
    from databricks.sdk.service.postgres import (
        SyncedTable,
        SyncedTableSyncedTableSpec,
        SyncedTableSyncedTableSpecSyncedTableSchedulingPolicy,
    )

    full_name = f"{catalog}.{schema}.{table}"
    return SyncedTable(
        spec=SyncedTableSyncedTableSpec(
            source_table_full_name=full_name,
            branch=f"projects/{project_id}/branches/{PRODUCTION_BRANCH}",
            primary_key_columns=list(PRIMARY_KEYS[table]),
            scheduling_policy=SyncedTableSyncedTableSpecSyncedTableSchedulingPolicy[sync_mode],
            postgres_database=PG_DATABASE,
            create_database_objects_if_missing=True,
        )
    )


def _get_or_create_project(postgres: Any, project_id: str, settings: dict | None = None) -> tuple[Any, bool]:
    name = f"projects/{project_id}"
    try:
        existing = postgres.get_project(name=name)
        if getattr(existing, "delete_time", None):
            # get_project still returns soft-deleted projects; treating one as live makes every
            # synced table fail later with "Project not found".
            raise ProjectSoftDeleted(
                f"Lakebase project {project_id} is soft-deleted (purge at {getattr(existing, 'purge_time', None)}). "
                f"Undelete it (databricks postgres undelete-project {name}), delete it with purge=True, "
                f"or use another suffix.")
        return existing, False
    except ProjectSoftDeleted:
        raise
    except Exception as exc:
        if _is_permission_denied(exc):
            _reraise_entitlement(exc)
        if not _is_not_found(exc):
            raise
    settings = dict(settings or project_settings())

    def create() -> Any:
        return _await_operation(postgres.create_project(project=_build_project(project_id, **settings), project_id=project_id))

    try:
        return create(), True
    except Exception as exc:
        if _is_permission_denied(exc):
            _reraise_entitlement(exc)
        minimum = _suspend_minimum(str(exc))
        if minimum is None or minimum <= settings["suspend_s"]:
            raise
        # Safety net for tiers with a higher auto-suspend minimum when free_edition was not
        # set (e.g. Databricks Free Edition: 300 s). Only this one bound is retried.
        print(f"[provision_lakebase] workspace tier requires auto-suspend >= {minimum} s; retrying "
              f"(pass free_edition=True to skip this)", file=sys.stderr)
        settings["suspend_s"] = minimum
        try:
            return create(), True
        except Exception as retry_exc:
            raise RuntimeError(
                f"Lakebase project creation failed after adjusting auto-suspend to {minimum} s "
                f"(settings {settings}): {retry_exc}") from retry_exc


_SUSPEND_MIN_RE = re.compile(
    r"suspend[^.]*?minimum(?: allowed)?\s*(?:is|of)?\s*\(?\s*(\d+)\s*(?:s\b|sec|seconds)", re.I)


def _suspend_minimum(message: str) -> int | None:
    """Minimum auto-suspend (seconds) from a tier-limit error, or None if the error is unrelated."""
    m = _SUSPEND_MIN_RE.search(message)
    return int(m.group(1)) if m else None


def _ensure_synced_table(
    postgres: Any,
    *,
    catalog: str,
    schema: str,
    table: str,
    project_id: str,
    sync_mode: str = "SNAPSHOT",
) -> bool:
    synced_id = f"{catalog}.{schema}.{table}_sync"
    synced_name = f"synced_tables/{synced_id}"
    try:
        postgres.get_synced_table(name=synced_name)
        return False
    except Exception as exc:
        if _is_permission_denied(exc):
            _reraise_entitlement(exc)
        if not _is_not_found(exc):
            raise
    for attempt in range(PROJECT_PROPAGATION_ATTEMPTS):
        try:
            created = postgres.create_synced_table(
                synced_table=_build_synced_table(
                    catalog=catalog,
                    schema=schema,
                    table=table,
                    project_id=project_id,
                    sync_mode=sync_mode,
                ),
                synced_table_id=synced_id,
            )
            _await_operation(created)
            return True
        except Exception as exc:
            # A just-created project (especially one recreated with the id of a recently deleted
            # project) can take a few seconds to be visible to the sync service.
            if "project not found" in str(exc).lower() and attempt < PROJECT_PROPAGATION_ATTEMPTS - 1:
                time.sleep(PROJECT_PROPAGATION_SLEEP_S)
                continue
            _reraise_entitlement(exc)
            raise
    raise RuntimeError("unreachable")


def provision_lakebase(
    *,
    catalog: str,
    schema: str,
    project_id: str,
    client: Any,
    sync_mode: str = "SNAPSHOT",
    free_edition: bool = False,
    suspend_s: int | None = None,
    max_cu: float | None = None,
) -> dict:
    """get_project or create_project; create four synced tables if missing; wait until ready.

    project_id must match ^navidad-[a-z0-9-]+$. Reuse existing project with that id.
    sync_mode: SNAPSHOT (default: one full copy, no running pipeline, the cheap choice for
    workshops), TRIGGERED, or CONTINUOUS (seconds-fresh, ~80% of the running cost). Only
    applies to synced tables created by this call; the mode of an existing synced table
    cannot be changed (delete and recreate it). With SNAPSHOT, rewriting a Delta table needs
    a manual refresh of its <table>_sync to reach Lakebase.
    free_edition: use the Databricks Free Edition preset (1 CU, 300 s auto-suspend). suspend_s and
    max_cu override individual project settings. Settings only apply when the project is created.
    """
    _validate_names(catalog, schema, project_id)
    sync_mode = sync_mode.upper()
    if sync_mode not in SYNC_MODES:
        raise ValueError(f"sync_mode must be one of {SYNC_MODES}")
    last_completed = None
    postgres = client.postgres
    try:
        _get_or_create_project(postgres, project_id, project_settings(free_edition, suspend_s=suspend_s, max_cu=max_cu))
        last_completed = "project"
        created_sync: list[str] = []
        for table in TABLES:
            created = _ensure_synced_table(
                postgres,
                catalog=catalog,
                schema=schema,
                table=table,
                project_id=project_id,
                sync_mode=sync_mode,
            )
            last_completed = f"sync:{table}"
            if created:
                created_sync.append(table)
        return {
            "status": "ok",
            "project_id": project_id,
            "synced": list(TABLES),
            "created_sync": created_sync,
            "last_completed": last_completed,
        }
    except Exception as exc:
        payload = {
            "step": "provision_lakebase",
            "status": "error",
            "last_completed": last_completed,
            "error": str(exc),
        }
        print(json.dumps(payload), file=sys.stderr)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision Lakebase Autoscaling + inbound sync")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--sync-mode", default="SNAPSHOT", choices=SYNC_MODES, type=str.upper)
    args = parser.parse_args(argv)
    from databricks.sdk import WorkspaceClient

    client = WorkspaceClient(profile=args.profile)
    try:
        result = provision_lakebase(
            catalog=args.catalog,
            schema=args.schema,
            project_id=args.project_id,
            client=client,
            sync_mode=args.sync_mode,
        )
        print(json.dumps({"step": "provision_lakebase", **result}))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "step": "provision_lakebase",
                    "status": "error",
                    "error": str(exc),
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
