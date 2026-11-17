import sys
from pathlib import Path

from fastapi.testclient import TestClient

APP_DIR = Path(__file__).resolve().parents[1] / "templates" / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from app import _message_text, create_app  # noqa: E402


def test_health_and_sample_and_model(monkeypatch):
    monkeypatch.setenv("NAVIDAD_SCHEMA", "navidad_dev_agarcia")
    seen = {}

    def execute_sql(query: str):
        seen["query"] = query
        assert "households_sync" in query
        assert "navidad_dev_agarcia" in query
        return [{"city": "Madrid", "n": 3}]

    def complete(prompt: str):
        assert "conectado" in prompt.lower()
        return "Estoy conectado."

    client = TestClient(create_app(execute_sql=execute_sql, complete=complete))
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/api/sample-households").json()["rows"][0]["city"] == "Madrid"
    assert "conectado" in client.post("/api/model-ping").json()["text"].lower()
    page = client.get("/")
    assert page.status_code == 200
    assert "Taller de Navidad" in page.text
    assert 'lang="es"' in page.text
    assert "if (!res.ok)" in page.text


def test_message_text_extracts_gateway_text_blocks():
    content = [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": ""}]},
        {"type": "text", "text": "I'm wired up and ready to go."},
    ]
    assert _message_text(content) == "I'm wired up and ready to go."
    assert _message_text("plain") == "plain"


def test_navidad_fx_assets_are_served_and_wired():
    client = TestClient(create_app(execute_sql=lambda q: [], complete=lambda p: ""))
    page = client.get("/").text
    assert "/static/navidad-fx.css" in page
    assert "/static/navidad-fx.js" in page
    assert "data-navidad-header" in page
    assert "data-navidad-logo" in page

    css = client.get("/static/navidad-fx.css")
    assert css.status_code == 200
    for name in ("snow-fall-contained", "santa-fly", "sleigh-sparkle", "shooting-star"):
        assert f"@keyframes {name}" in css.text
    assert "prefers-reduced-motion" in css.text

    js = client.get("/static/navidad-fx.js")
    assert js.status_code == 200
    for marker in ("santa-flight", "reindeer", "snowman", "snowfall", "navidad-fx"):
        assert marker in js.text

    assert client.get("/static/databricks-symbol.svg").status_code == 200


def test_execute_sql_from_pg_accepts_params():
    import inspect
    from app import execute_sql_from_pg

    sig = inspect.signature(execute_sql_from_pg)
    assert list(sig.parameters) == ["query", "params"] and sig.parameters["params"].default is None


def test_complete_routes_gateway_vs_serving_endpoint(monkeypatch):
    import app as app_mod

    calls = []

    class Cfg:
        host = "https://ws.example.com"

        def authenticate(self):
            return {"Authorization": "Bearer x"}

    class W:
        config = Cfg()

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "Estoy conectado."}}]}

    class Client:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers, json):
            calls.append((url, dict(json)))
            return Resp()

    monkeypatch.setattr(app_mod, "WorkspaceClient", lambda: W())
    monkeypatch.setattr(app_mod.httpx, "Client", Client)
    assert app_mod.complete_from_gateway("system.ai.claude-opus-5", "hola") == "Estoy conectado."
    assert app_mod.complete_from_gateway("databricks-gpt-oss-120b", "hola") == "Estoy conectado."
    assert calls[0][0].endswith("/ai-gateway/mlflow/v1/chat/completions") and calls[0][1]["model"] == "system.ai.claude-opus-5"
    assert calls[1][0] == "https://ws.example.com/serving-endpoints/databricks-gpt-oss-120b/invocations"
    assert "model" not in calls[1][1]
    assert calls[0][1]["max_tokens"] == 64 and calls[1][1]["max_tokens"] == 256
