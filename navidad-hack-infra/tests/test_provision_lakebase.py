import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from provision_lakebase import provision_lakebase


class FakePostgres:
    def __init__(self):
        self.projects = {}
        self.synced = {}
        self.create_project_calls = 0

    def get_project(self, name):
        pid = name.split("/", 1)[1]
        if pid not in self.projects:
            raise KeyError(name)
        return self.projects[pid]

    def create_project(self, **kwargs):
        self.create_project_calls += 1
        pid = kwargs["project_id"]
        self.projects[pid] = {"id": pid}
        return self.projects[pid]

    def get_synced_table(self, name):
        if name not in self.synced:
            raise KeyError(name)
        return self.synced[name]

    def create_synced_table(self, **kwargs):
        sid = kwargs["synced_table_id"]
        self.synced[f"synced_tables/{sid}"] = kwargs
        return kwargs


class FakeW:
    def __init__(self):
        self.postgres = FakePostgres()


def test_reuses_existing_project():
    w = FakeW()
    w.postgres.projects["navidad-rob"] = {"id": "navidad-rob"}
    provision_lakebase(catalog="c", schema="navidad_rob", project_id="navidad-rob", client=w)
    provision_lakebase(catalog="c", schema="navidad_rob", project_id="navidad-rob", client=w)
    assert w.postgres.create_project_calls == 0
    assert len(w.postgres.synced) == 4


def test_creates_once_then_skips_synced_tables():
    w = FakeW()
    provision_lakebase(catalog="c", schema="navidad_rob", project_id="navidad-rob", client=w)
    assert w.postgres.create_project_calls == 1
    provision_lakebase(catalog="c", schema="navidad_rob", project_id="navidad-rob", client=w)
    assert w.postgres.create_project_calls == 1


def test_project_creation_adapts_to_free_edition_suspend_minimum():
    import provision_lakebase as pl

    seen = []

    class Op:
        def wait(self, opts=None):
            return "project"

    class Postgres:
        def get_project(self, name):
            raise KeyError(name)

        def create_project(self, project, project_id):
            secs = project.spec.default_endpoint_settings.suspend_timeout_duration.seconds
            seen.append(secs)
            if secs < 300:
                raise Exception("BadRequest: Requested auto-suspend timeout (60 seconds) is below the minimum allowed (300 seconds) for this workspace tier.")
            return Op()

    assert pl._get_or_create_project(Postgres(), "navidad-x") == ("project", True)
    assert seen == [60, 300]


def test_suspend_minimum_parsing_is_tolerant_and_specific():
    import provision_lakebase as pl

    for msg in (
        "BadRequest: Requested auto-suspend timeout (60 seconds) is below the minimum allowed (300 seconds) for this workspace tier.",
        "Requested suspend timeout (60 seconds) is below the minimum allowed (300 seconds)",
        "suspend_timeout_duration must be at least the minimum of 300s for this tier",
    ):
        assert pl._suspend_minimum(msg) == 300, msg
    for msg in (
        "Maximum number of projects reached for this account: current usage at most 1",
        "Requested autoscaling max CU (8) exceeds the maximum allowed (1) for this workspace tier.",
        "INVALID_PARAMETER_VALUE: execute failed",
    ):
        assert pl._suspend_minimum(msg) is None, msg


def _fake_postgres(limit_s=300, fail_after_retry=False, seen=None):
    seen = [] if seen is None else seen

    class Op:
        def wait(self, opts=None):
            return "project"

    class Postgres:
        def get_project(self, name):
            raise KeyError(name)

        def create_project(self, project, project_id):
            d = project.spec.default_endpoint_settings
            seen.append((d.suspend_timeout_duration.seconds, d.autoscaling_limit_max_cu))
            if d.suspend_timeout_duration.seconds < limit_s:
                raise Exception(f"Requested auto-suspend timeout is below the minimum allowed ({limit_s} seconds)")
            if fail_after_retry:
                raise Exception("Maximum number of projects reached")
            return Op()

    return Postgres(), seen


def test_free_edition_preset_creates_without_retry():
    import provision_lakebase as pl

    pg, seen = _fake_postgres()
    assert pl._get_or_create_project(pg, "navidad-x", pl.project_settings(free_edition=True)) == ("project", True)
    assert seen == [(300, 1.0)]


def test_project_settings_validate_and_override():
    import pytest
    import provision_lakebase as pl

    assert pl.project_settings()["suspend_s"] == 60 and pl.project_settings()["max_cu"] == 8.0
    assert pl.project_settings(True, max_cu=2)["max_cu"] == 2 and pl.project_settings(True)["suspend_s"] == 300
    with pytest.raises(ValueError):
        pl.project_settings(max_cu=0.25)  # below min_cu 0.5


def test_retry_failure_keeps_the_cause():
    import pytest
    import provision_lakebase as pl

    pg, seen = _fake_postgres(fail_after_retry=True)
    with pytest.raises(RuntimeError) as err:
        pl._get_or_create_project(pg, "navidad-x")
    assert "Maximum number of projects" in str(err.value) and err.value.__cause__ is not None
    assert seen == [(60, 8.0), (300, 8.0)]


def test_synced_table_creation_waits_for_project_propagation(monkeypatch):
    import provision_lakebase as pl

    monkeypatch.setattr(pl, "PROJECT_PROPAGATION_SLEEP_S", 0)
    calls = []

    class Op:
        def wait(self, opts=None):
            return "ok"

    class Postgres:
        def get_synced_table(self, name):
            raise KeyError(name)

        def create_synced_table(self, synced_table, synced_table_id):
            calls.append(synced_table_id)
            if len(calls) < 3:
                raise Exception("NotFound: Project not found")
            return Op()

    assert pl._ensure_synced_table(Postgres(), catalog="c", schema="navidad_x", table="households",
                                   project_id="navidad-x", sync_mode="SNAPSHOT") is True
    assert len(calls) == 3


def test_soft_deleted_project_is_not_treated_as_live():
    import pytest
    import provision_lakebase as pl

    class Proj:
        delete_time = "2026-11-13T15:27:56Z"
        purge_time = "2026-11-20T15:27:56Z"

    class Postgres:
        def get_project(self, name):
            return Proj()

        def create_project(self, **kw):
            raise AssertionError("must not create over a soft-deleted project")

    with pytest.raises(pl.ProjectSoftDeleted) as err:
        pl._get_or_create_project(Postgres(), "navidad-x")
    assert "undelete-project" in str(err.value) and "purge" in str(err.value)
