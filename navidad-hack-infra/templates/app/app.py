from __future__ import annotations

import os
from urllib.parse import quote
from pathlib import Path
from typing import Any, Callable

import httpx
import psycopg2
from databricks.sdk import WorkspaceClient
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg2.extras import RealDictCursor

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_MODEL = "system.ai.claude-opus-5"
DEFAULT_DATABASE = "databricks_postgres"
WIRED_UP_PROMPT = "Responde en una sola frase, en español, que estás conectado."
GATEWAY_CHAT_PATH = "/ai-gateway/mlflow/v1/chat/completions"
SAMPLE_SQL = (
    "SELECT city, autonomous_community, count(*) AS n FROM {schema}.households_sync "
    "GROUP BY city, autonomous_community ORDER BY n DESC LIMIT {limit}"
)


def pg_config_from_env() -> dict[str, str]:
    """Map NAVIDAD_PG_* overrides, then Databricks Apps PG* vars, then legacy names."""

    def first(*names: str, default: str | None = None) -> str | None:
        for name in names:
            value = os.environ.get(name)
            if value:
                return value
        return default

    host = first("NAVIDAD_PG_HOST", "PGHOST", "DATABRICKS_DATABASE_CONNECTION_HOST")
    user = first("NAVIDAD_PG_USER", "PGUSER", "DATABRICKS_DATABASE_CONNECTION_USER")
    database = first(
        "NAVIDAD_PG_DATABASE",
        "PGDATABASE",
        "DATABRICKS_DATABASE_CONNECTION_DATABASE",
        default=DEFAULT_DATABASE,
    )
    port = first("NAVIDAD_PG_PORT", "PGPORT", "DATABRICKS_DATABASE_CONNECTION_PORT", default="5432")
    sslmode = first("PGSSLMODE", default="require")
    password = first("NAVIDAD_PG_PASSWORD", "PGPASSWORD", "DATABRICKS_DATABASE_CONNECTION_PASSWORD")
    endpoint = first("NAVIDAD_PG_ENDPOINT", "LAKEBASE_ENDPOINT", "ENDPOINT_NAME")
    cfg: dict[str, str] = {
        "host": host or "",
        "user": user or "",
        "dbname": database or DEFAULT_DATABASE,
        "port": port or "5432",
        "sslmode": sslmode or "require",
    }
    if password:
        cfg["password"] = password
    if endpoint:
        cfg["endpoint"] = endpoint
    return cfg


def _quote_ident(name: str) -> str:
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError(f"unsafe identifier {name!r}")
    return name


def _endpoint_path(raw: str | None, project_id: str | None) -> str | None:
    if raw and "/endpoints/" in raw:
        return raw
    if project_id:
        return f"projects/{project_id}/branches/production/endpoints/primary"
    if raw and "/branches/" in raw:
        branch = raw.split("/databases/")[0]
        return f"{branch}/endpoints/primary"
    return raw


def _oauth_password(endpoint: str) -> str:
    credential = WorkspaceClient().postgres.generate_database_credential(endpoint=endpoint)
    return credential.token


def execute_sql_from_pg(query: str, params: tuple | None = None) -> list[dict[str, Any]]:
    cfg = pg_config_from_env()
    endpoint = _endpoint_path(cfg.get("endpoint"), os.environ.get("NAVIDAD_PROJECT_ID"))
    connect_kwargs = {
        "host": cfg["host"],
        "user": cfg["user"],
        "dbname": cfg["dbname"],
        "port": cfg["port"],
        "sslmode": cfg["sslmode"],
    }
    if cfg.get("password"):
        connect_kwargs["password"] = cfg["password"]
    elif endpoint:
        connect_kwargs["password"] = _oauth_password(endpoint)
        if not connect_kwargs["host"]:
            endpoint_obj = WorkspaceClient().postgres.get_endpoint(name=endpoint)
            connect_kwargs["host"] = endpoint_obj.status.hosts.host
        if not connect_kwargs["user"]:
            me = WorkspaceClient().current_user.me()
            connect_kwargs["user"] = me.user_name or me.application_id or ""
    connection = psycopg2.connect(**connect_kwargs)
    connection.autocommit = True
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(query, params)
            if cursor.description is None:
                return []
            return [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text") or "")
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts).strip()
    return str(content or "")


def complete_from_gateway(model: str, prompt: str) -> str:
    w = WorkspaceClient()
    host = (w.config.host or "").rstrip("/")
    headers = w.config.authenticate()
    headers["Content-Type"] = "application/json"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 64,
    }
    # Unity Catalog model names (system.ai.*) go through the AI Gateway; a plain serving
    # endpoint name (e.g. databricks-gpt-oss-120b, as in Databricks Free Edition) is
    # invoked directly, with the same OpenAI-style chat payload.
    if "." in model:
        url, route = f"{host}{GATEWAY_CHAT_PATH}", "Gateway chat completions"
    else:
        payload.pop("model")
        payload["max_tokens"] = 256  # reasoning models (gpt-oss) spend tokens before the text block
        url, route = f"{host}/serving-endpoints/{quote(model, safe='')}/invocations", f"Serving endpoint {model}"
    with httpx.Client(timeout=60.0) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"{route} returned no choices")
    message = choices[0].get("message") or {}
    text = _message_text(message.get("content")) or choices[0].get("text") or ""
    if not text:
        raise RuntimeError(f"{route} returned empty content")
    return text


def create_app(
    execute_sql: Callable[..., list[dict[str, Any]]] | None = None,
    complete: Callable[[str], str] | None = None,
) -> FastAPI:
    run_sql = execute_sql or execute_sql_from_pg
    run_complete = complete or (
        lambda prompt: complete_from_gateway(
            os.environ.get("NAVIDAD_MODEL", DEFAULT_MODEL),
            prompt,
        )
    )

    app = FastAPI(title="Taller de Navidad")

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/sample-households")
    def sample_households() -> dict[str, list[dict[str, Any]]]:
        limit = max(1, int(os.environ.get("NAVIDAD_SAMPLE_LIMIT", "20")))
        schema = _quote_ident(os.environ.get("NAVIDAD_SCHEMA", "public"))
        try:
            rows = run_sql(SAMPLE_SQL.format(schema=schema, limit=limit))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"rows": rows}

    @app.post("/api/model-ping")
    def model_ping() -> dict[str, str]:
        try:
            return {"text": run_complete(WIRED_UP_PROMPT)}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # navidad-fx.css/.js, logo and any assets participants add.
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


app = create_app()
