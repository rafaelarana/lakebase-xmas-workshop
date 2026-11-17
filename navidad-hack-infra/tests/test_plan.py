from plan import build_plan, format_plan, slugify, workspace_source_path


def test_plan_names_every_mutation_and_target():
    plan = build_plan(
        catalog="main",
        slug="agarcia",
        user="ana.garcia@example.com",
        scale=0.01,
    )
    assert plan["names"] == {
        "schema": "navidad_agarcia",
        "prefix": "navidad-agarcia",
        "project_id": "navidad-agarcia",
        "app_name": "navidad-agarcia-app",
    }
    text = format_plan(plan)
    for needle in (
        "main.navidad_agarcia.journeys",
        "navidad_agarcia.households_sync",
        "navidad-agarcia-app",
        "/Workspace/Users/ana.garcia@example.com/navidad-hackathon/navidad-agarcia-app",
        "create catalog",
        "SELECT",
    ):
        assert needle in text


def test_plan_rejects_bad_slug():
    try:
        build_plan(catalog="c", slug="Rob_W", user="u")
        raise AssertionError("should reject")
    except ValueError:
        pass


def test_slugify_and_source_path():
    assert slugify("ana.garcia@example.com") == "anagarcia"
    assert workspace_source_path("u@x.com", "navidad-a") == "/Workspace/Users/u@x.com/navidad-hackathon/navidad-a-app"
