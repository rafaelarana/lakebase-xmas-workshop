from pathlib import Path

from deploy_app import AppsEntitlementError, deploy_app


def test_refuses_before_sync(tmp_path: Path):
    try:
        deploy_app(
            prefix="navidad-rob",
            catalog="c",
            schema="navidad_rob",
            project_id="navidad-rob",
            model="system.ai.claude-opus-5",
            source_template=tmp_path / "missing",
            dest_dir=tmp_path / "dest",
            client=object(),
            synced_ready=False,
        )
        raise AssertionError("should refuse")
    except RuntimeError as exc:
        assert "sync" in str(exc).lower()


def test_copy_and_update_in_place(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("ok")
    created = []

    class Apps:
        def __init__(self):
            self.names = set()

        def get(self, name):
            if name not in self.names:
                raise KeyError(name)
            return {"name": name, "url": f"https://apps.example/{name}"}

        def create(self, name):
            if name in self.names:
                raise RuntimeError("duplicate")
            self.names.add(name)
            created.append("create")
            return {"name": name, "url": f"https://apps.example/{name}"}

        def deploy(self, name):
            created.append(f"deploy:{name}")
            return {"name": name}

    class W:
        def __init__(self):
            self.apps = Apps()

    w = W()
    dest = tmp_path / "dest"
    first = deploy_app(
        prefix="navidad-rob",
        catalog="c",
        schema="navidad_rob",
        project_id="navidad-rob",
        model="system.ai.claude-opus-5",
        source_template=src,
        dest_dir=dest,
        client=w,
        synced_ready=True,
    )
    second = deploy_app(
        prefix="navidad-rob",
        catalog="c",
        schema="navidad_rob",
        project_id="navidad-rob",
        model="system.ai.claude-opus-5",
        source_template=src,
        dest_dir=dest,
        client=w,
        synced_ready=True,
    )
    assert (dest / "app.py").read_text() == "ok"
    assert created.count("create") == 1
    assert any(x.startswith("deploy:") for x in created)
    assert first["app_name"] == "navidad-rob-app"
    assert second["app_name"] == "navidad-rob-app"
    yaml_text = (dest / "app.yaml").read_text()
    assert "NAVIDAD_MODEL" in yaml_text
    assert "system.ai.claude-opus-5" in yaml_text
    assert "NAVIDAD_SCHEMA" in yaml_text
    assert "navidad_rob" in yaml_text
    assert "NAVIDAD_PROJECT_ID" in yaml_text
    assert "endpoints/primary" in yaml_text
    assert "navidad-pg" in yaml_text


class PermissionDenied(Exception):
    pass


def test_missing_apps_entitlement(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("ok")

    class Apps:
        def get(self, name):
            raise PermissionDenied("apps not enabled")

        def create(self, name):
            raise PermissionDenied("apps not enabled")

        def deploy(self, name):
            raise PermissionDenied("apps not enabled")

    class W:
        def __init__(self):
            self.apps = Apps()

    try:
        deploy_app(
            prefix="navidad-rob",
            catalog="c",
            schema="navidad_rob",
            project_id="navidad-rob",
            model="system.ai.claude-opus-5",
            source_template=src,
            dest_dir=tmp_path / "dest",
            client=W(),
            synced_ready=True,
        )
        raise AssertionError("should refuse")
    except AppsEntitlementError as exc:
        assert "Apps entitlement missing" in str(exc)


def test_fatal_never_deployed_app_is_reported_not_redeployed():
    from types import SimpleNamespace

    from deploy_app import AppFatalError, _raise_if_fatal

    broken = SimpleNamespace(
        compute_status=SimpleNamespace(state="ERROR", message="grant timed out"),
        active_deployment=None,
    )
    try:
        _raise_if_fatal(broken, "navidad-rob-app")
        raise AssertionError("should raise")
    except AppFatalError as exc:
        assert "Ask the user before deleting" in str(exc)
    healthy = SimpleNamespace(compute_status=SimpleNamespace(state="STOPPED"), active_deployment=None)
    _raise_if_fatal(healthy, "navidad-rob-app")


def test_wake_endpoint_retries_then_succeeds(monkeypatch):
    import deploy_app

    calls = {"n": 0}

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql):
            assert sql == "SELECT 1"

    class Conn:
        def cursor(self):
            return Cur()

        def close(self):
            pass

    def fake_connect(client, project_id):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("endpoint waking up")
        return Conn()

    monkeypatch.setattr(deploy_app, "_pg_connect", fake_connect)
    monkeypatch.setattr(deploy_app.time, "sleep", lambda s: None)
    deploy_app._wake_endpoint(object(), "navidad-rob")
    assert calls["n"] == 3
