from generate_data import GenerateConfig
from write_uc import CatalogNotFoundError, records_for, write_uc


class FakeWriter:
    def __init__(self, spark: "FakeSpark", records: list) -> None:
        self.spark = spark
        self.records = records
        self._format = None
        self._mode = None
        self.options: dict[str, str] = {}

    def format(self, name: str) -> "FakeWriter":
        self._format = name
        return self

    def mode(self, name: str) -> "FakeWriter":
        self._mode = name
        return self

    def option(self, key: str, value: str) -> "FakeWriter":
        self.options[key] = value
        return self

    def saveAsTable(self, name: str) -> None:
        self.spark.saved.append(
            {
                "name": name,
                "format": self._format,
                "mode": self._mode,
                "options": dict(self.options),
                "n": len(self.records),
            }
        )


class FakeDF:
    def __init__(self, spark: "FakeSpark", records: list) -> None:
        self.write = FakeWriter(spark, records)


class FakeSpark:
    def __init__(self, catalog_exists: bool = True, households_row: dict | None = None) -> None:
        self.catalog_exists = catalog_exists
        self.households_row = households_row  # None = households table missing
        self.statements: list[str] = []
        self.saved: list[dict] = []

    def sql(self, statement: str):
        self.statements.append(statement)
        if (not self.catalog_exists) and "CREATE SCHEMA" in statement.upper():
            raise RuntimeError("Catalog not found")
        if statement.startswith("SELECT count(*)"):
            if self.households_row is None:
                raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND")
            row = self.households_row
            return type("R", (), {"first": lambda _self: row})()
        return None

    def createDataFrame(self, records: list, schema: object | None = None) -> FakeDF:
        del schema
        return FakeDF(self, records)


def test_refuses_missing_catalog():
    fake = FakeSpark(catalog_exists=False)
    try:
        write_uc(
            catalog="nope",
            schema="navidad_x",
            cfg=GenerateConfig(scale=0.0001),
            spark=fake,
        )
        raise AssertionError("should have failed")
    except CatalogNotFoundError as exc:
        assert "will not create catalogs" in str(exc)
    assert fake.saved == []


def test_overwrites_four_delta_tables_with_cdf():
    fake = FakeSpark()
    counts = write_uc(
        catalog="main",
        schema="navidad_test",
        cfg=GenerateConfig(scale=0.0001),
        spark=fake,
    )
    joined = "\n".join(fake.statements)
    assert "CREATE SCHEMA IF NOT EXISTS" in joined
    assert "INSERT INTO" not in joined
    names = [item["name"] for item in fake.saved]
    for table in ("households", "people", "preferences", "journeys"):
        fqn = f"main.navidad_test.{table}"
        assert fqn in names
        assert f"ALTER TABLE {fqn} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)" in joined
    assert {item["format"] for item in fake.saved} == {"delta"}
    assert {item["mode"] for item in fake.saved} == {"overwrite"}
    assert counts["households"] == 50
    assert fake.saved[0]["n"] == 50


def test_rejects_schema_not_matching_navidad_prefix():
    fake = FakeSpark()
    try:
        write_uc(
            catalog="main",
            schema="not_navidad",
            cfg=GenerateConfig(scale=0.0001),
            spark=fake,
        )
        raise AssertionError("should have failed")
    except ValueError as exc:
        assert r"^navidad_[a-z0-9_]+$" in str(exc)
    assert fake.statements == []
    assert fake.saved == []


def test_records_for_parses_journey_timestamps():
    from generate_data import generate

    ds = generate(GenerateConfig(scale=0.0001))
    recs = records_for("journeys", ds.journeys)
    assert recs
    assert recs[0]["arrived_at_utc"].tzinfo is not None


def _households_row(scale: float) -> dict:
    from generate_data import generate

    hh = generate(GenerateConfig(scale=scale)).households
    return {"n": len(hh), "m": max(h.household_id for h in hh)}


def test_tables_string_and_unknown_names_fail_before_any_work():
    import pytest

    for bad in ("jour", ("journeys", "routes"), ()):
        fake = FakeSpark()
        with pytest.raises(ValueError, match="subset of"):
            write_uc(catalog="c", schema="navidad_x", cfg=GenerateConfig(scale=0.0001), spark=fake, tables=bad)
        assert fake.statements == [] and fake.saved == []


def test_journeys_only_rewrite_accepts_a_single_string_and_checks_households():
    fake = FakeSpark(households_row=_households_row(0.0001))
    counts = write_uc(catalog="c", schema="navidad_x", cfg=GenerateConfig(scale=0.0001), spark=fake, tables="journeys")
    assert list(counts) == ["journeys"]
    assert [item["name"] for item in fake.saved] == ["c.navidad_x.journeys"]


def test_journeys_only_rewrite_refuses_a_different_scale():
    import pytest

    fake = FakeSpark(households_row=_households_row(0.0002))
    with pytest.raises(ValueError, match="same scale/seed"):
        write_uc(catalog="c", schema="navidad_x", cfg=GenerateConfig(scale=0.0001), spark=fake, tables=("journeys",))
    assert fake.saved == []


def test_journeys_only_rewrite_needs_existing_households():
    import pytest

    fake = FakeSpark(households_row=None)
    with pytest.raises(ValueError, match="write all four tables first"):
        write_uc(catalog="c", schema="navidad_x", cfg=GenerateConfig(scale=0.0001), spark=fake, tables=["journeys"])
    assert fake.saved == []


def test_pandas_frames_are_typed_like_the_ddl():
    from generate_data import generate
    from write_uc import COLUMN_ORDER, dataset_tables, pandas_for, spark_schema_ddl

    tables = dataset_tables(generate(GenerateConfig(scale=0.0001)))
    for name, rows in tables.items():
        pdf = pandas_for(name, rows)
        assert list(pdf.columns) == list(COLUMN_ORDER[name]) and len(pdf) == len(rows)
        assert spark_schema_ddl(name).split(", ")[0].split(" ")[0] == COLUMN_ORDER[name][0]
    j = pandas_for("journeys", tables["journeys"])
    assert str(j["arrived_at_utc"].dtype).startswith("datetime64[") and str(j["arrived_at_utc"].dt.tz) == "UTC"
    assert str(j["route_id"].dtype) == "Int64" and str(j["is_revisit"].dtype) == "boolean"


def test_journeys_only_rewrite_keeps_other_errors():
    import pytest

    class NoPermission(FakeSpark):
        def sql(self, statement):
            if statement.startswith("SELECT count(*)"):
                raise RuntimeError("PERMISSION_DENIED: User does not have SELECT on Table")
            return super().sql(statement)

    fake = NoPermission()
    with pytest.raises(RuntimeError, match="PERMISSION_DENIED"):
        write_uc(catalog="c", schema="navidad_x", cfg=GenerateConfig(scale=0.0001), spark=fake, tables=["journeys"])
    assert fake.saved == []
