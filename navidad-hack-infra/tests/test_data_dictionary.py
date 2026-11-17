from pathlib import Path

DICT_PATH = Path(__file__).resolve().parents[1] / "references" / "data-dictionary.md"

REQUIRED_TABLES = {
    "households": [
        "household_id", "label", "city", "province", "autonomous_community",
        "lat", "lon", "postcode_area", "timezone", "household_size",
        "first_seen_year", "nochebuena_bringer", "visits_reyes",
    ],
    "people": ["person_id", "household_id", "given_name", "age_years", "role"],
    "preferences": [
        "person_id", "naughty_nice_score", "gift_affinity", "dietary_notes",
        "window_policy", "favourite_king", "last_updated_year",
    ],
    "journeys": [
        "year", "campaign", "route_id", "stop_number", "household_id",
        "bringer", "arrived_at_utc", "dwell_minutes", "is_revisit",
    ],
}


def test_dictionary_lists_every_column_in_order():
    text = DICT_PATH.read_text()
    for table, cols in REQUIRED_TABLES.items():
        assert f"## `{table}`" in text
        for col in cols:
            assert f"`{col}`" in text.split(f"## `{table}`", 1)[1].split("## `", 1)[0]


def test_skill_md_has_frontmatter_and_order():
    text = Path(__file__).resolve().parents[1].joinpath("SKILL.md").read_text()
    assert "name: navidad-hack-infra" in text
    assert "write_uc.py" in text
    assert "provision_lakebase.py" in text
    assert text.index("write_uc.py") < text.index("provision_lakebase.py")
    assert text.index("provision_lakebase.py") < text.index("deploy_app.py")
    assert "data-dictionary.md" in text
    assert "serverless" in text.lower()
    assert "subprocess" in text
    assert "## Interview (one turn max)" in text
    assert "No plan display, no function-body review" in text
    assert "One \"yes\"" in text
    assert "Never retry via a different route" in text
    assert "Read and display" not in text
    assert "Approve this plan" not in text
    assert "%pip install psycopg2-binary \"databricks-sdk==0.122.0\"" in text
    assert "slugify(" in text
    assert "saveAsTable" in text or "spark" in text.lower()
    assert "navidad-hack-build-assistant" in text
    assert "## Handoff template" in text
    assert "**Workbench ready:** [navidad-<slug>-app](<app url>)" in text
    assert "Assets created:" in text
    assert "Never guess or construct the URL" in text
    assert "hack-build.md" not in text
    assert "hack-routes.md" not in text
    assert "hack-segmentation.md" not in text
