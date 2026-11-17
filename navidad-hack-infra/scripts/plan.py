"""Pure, side-effect-free description of every mutation the Navidad pipeline performs.

Use `format_plan(build_plan(...))` only if the user asks to review the plan first.
Default setup does not require showing this.
"""

from __future__ import annotations

import re
from typing import Any

SLUG_RE = re.compile(r"^[a-z0-9]+$")
TABLES = ("households", "people", "preferences", "journeys")
DEFAULT_MODEL = "system.ai.claude-opus-5"
DEFAULT_SEED = 20260106


def slugify(username: str) -> str:
    local = username.split("@", 1)[0].lower()
    return re.sub(r"[^a-z0-9]", "", local)


def workspace_source_path(user: str, prefix: str) -> str:
    return f"/Workspace/Users/{user}/navidad-hackathon/{prefix}-app"


def build_plan(
    *,
    catalog: str,
    slug: str,
    user: str,
    scale: float = 1.0,
    seed: int = DEFAULT_SEED,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    if not SLUG_RE.match(slug):
        raise ValueError(f"slug {slug!r} must be lowercase alphanumeric")
    schema = f"navidad_{slug}"
    prefix = f"navidad-{slug}"
    project_id = prefix
    app_name = f"{prefix}-app"
    return {
        "inputs": {
            "catalog": catalog,
            "slug": slug,
            "scale": scale,
            "seed": seed,
            "model": model,
        },
        "names": {
            "schema": schema,
            "prefix": prefix,
            "project_id": project_id,
            "app_name": app_name,
        },
        "steps": [
            {
                "step": "write_uc",
                "function": "scripts/write_uc.py::write_uc",
                "creates": [f"schema {catalog}.{schema} (if missing)"],
                "overwrites": [f"table {catalog}.{schema}.{t}" for t in TABLES],
                "never": [f"create catalog {catalog}"],
            },
            {
                "step": "provision_lakebase",
                "function": "scripts/provision_lakebase.py::provision_lakebase",
                "creates": [f"Lakebase Autoscaling project {project_id} (reused if it exists)"]
                + [f"synced table {catalog}.{schema}.{t}_sync (skipped if it exists)" for t in TABLES],
                "overwrites": [],
                "never": ["delete projects", "outbound / reverse sync"],
            },
            {
                "step": "deploy_app",
                "function": "scripts/deploy_app.py::deploy_app",
                "creates": [f"Databricks App {app_name} (updated in place if it exists)"],
                "overwrites": [f"workspace files under {workspace_source_path(user, prefix)}"],
                "grants": [
                    f"Postgres USAGE on schema {schema} and SELECT on its tables "
                    f"to the {app_name} service principal"
                ],
                "never": ["touch other apps", "write outside the path above"],
            },
        ],
    }


def format_plan(plan: dict[str, Any]) -> str:
    names = plan["names"]
    inputs = plan["inputs"]
    lines = [
        "Navidad hackathon setup plan",
        "",
        f"Catalog: {inputs['catalog']}  |  scale {inputs['scale']}  |  seed {inputs['seed']}",
        f"Model: {inputs['model']}",
        "",
    ]
    for item in plan["steps"]:
        lines.append(f"{item['step']} ({item['function']})")
        for key in ("creates", "overwrites", "grants", "never"):
            for entry in item.get(key, []):
                lines.append(f"  {key}: {entry}")
        lines.append("")
    lines.append(f"Schema {names['schema']}, Lakebase {names['project_id']}, app {names['app_name']}.")
    return "\n".join(lines)
