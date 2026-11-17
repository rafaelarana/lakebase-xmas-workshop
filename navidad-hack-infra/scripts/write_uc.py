from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from typing import Any

from generate_data import COLUMN_ORDER, GenerateConfig, NavidadDataset, generate

SCHEMA_RE = re.compile(r"^navidad_[a-z0-9_]+$")
IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")
TABLES = ("households", "people", "preferences", "journeys")
COLUMN_TYPES: dict[str, dict[str, str]] = {
    "households": {
        "household_id": "BIGINT",
        "label": "STRING",
        "city": "STRING",
        "province": "STRING",
        "autonomous_community": "STRING",
        "lat": "DOUBLE",
        "lon": "DOUBLE",
        "postcode_area": "STRING",
        "timezone": "STRING",
        "household_size": "INT",
        "first_seen_year": "INT",
        "nochebuena_bringer": "STRING",
        "visits_reyes": "BOOLEAN",
    },
    "people": {
        "person_id": "BIGINT",
        "household_id": "BIGINT",
        "given_name": "STRING",
        "age_years": "INT",
        "role": "STRING",
    },
    "preferences": {
        "person_id": "BIGINT",
        "naughty_nice_score": "INT",
        "gift_affinity": "STRING",
        "dietary_notes": "STRING",
        "window_policy": "STRING",
        "favourite_king": "STRING",
        "last_updated_year": "INT",
    },
    "journeys": {
        "year": "INT",
        "campaign": "STRING",
        "route_id": "BIGINT",
        "stop_number": "INT",
        "household_id": "BIGINT",
        "bringer": "STRING",
        "arrived_at_utc": "TIMESTAMP",
        "dwell_minutes": "INT",
        "is_revisit": "BOOLEAN",
    },
}


class CatalogNotFoundError(RuntimeError):
    """Catalog is missing; this skill never creates catalogs."""


def _quote_ident(name: str, *, label: str) -> str:
    if not IDENT_RE.match(name):
        raise ValueError(f"{label} {name!r} is not a valid identifier")
    return name


def _validate_names(catalog: str, schema: str) -> tuple[str, str]:
    catalog = _quote_ident(catalog, label="catalog")
    if not SCHEMA_RE.match(schema):
        raise ValueError(f"schema {schema!r} must match {SCHEMA_RE.pattern}")
    schema = _quote_ident(schema, label="schema")
    return catalog, schema


def records_for(table: str, rows: list[Any]) -> list[dict[str, Any]]:
    columns = COLUMN_ORDER[table]
    types = COLUMN_TYPES[table]
    out: list[dict[str, Any]] = []
    for row in rows:
        rec: dict[str, Any] = {}
        for col in columns:
            value = getattr(row, col)
            if types[col] == "TIMESTAMP" and isinstance(value, str):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            rec[col] = value
        out.append(rec)
    return out


def spark_schema_ddl(table: str) -> str:
    return ", ".join(f"{col} {COLUMN_TYPES[table][col]}" for col in COLUMN_ORDER[table])


def pandas_for(table: str, rows: list[Any]) -> Any:
    """Columnar pandas frame with explicit dtypes: createDataFrame(pdf, schema) ships it as Arrow
    batches instead of inferring a schema from millions of Python dicts (Spark Connect / serverless)."""
    import pandas as pd

    types = COLUMN_TYPES[table]
    data: dict[str, Any] = {}
    for col in COLUMN_ORDER[table]:
        values = [getattr(row, col) for row in rows]
        kind = types[col]
        if kind == "TIMESTAMP":
            data[col] = pd.to_datetime(values, utc=True)
        elif kind in ("BIGINT", "INT"):
            data[col] = pd.array(values, dtype="Int64" if kind == "BIGINT" else "Int32")
        elif kind == "DOUBLE":
            data[col] = pd.array(values, dtype="Float64")
        elif kind == "BOOLEAN":
            data[col] = pd.array(values, dtype="boolean")
        else:
            data[col] = pd.array(values, dtype="object")
    return pd.DataFrame(data)


def dataset_tables(dataset: NavidadDataset) -> dict[str, list[Any]]:
    return {
        "households": dataset.households,
        "people": dataset.people,
        "preferences": dataset.preferences,
        "journeys": dataset.journeys,
    }


def _normalize_tables(tables: str | tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    """Validate the subset of tables to overwrite (default: all four)."""
    if tables is None:
        return TABLES
    if isinstance(tables, str):
        tables = (tables,)
    only = tuple(dict.fromkeys(t.strip().lower() for t in tables if t and t.strip()))
    unknown = [t for t in only if t not in TABLES]
    if unknown or not only:
        raise ValueError(f"tables must be a non-empty subset of {TABLES}; got {tables!r}")
    return only


def _check_households_match(spark: Any, fqn: str, dataset: NavidadDataset) -> None:
    """Partial rewrites must reuse the scale/seed of the households already in UC, or joins lose rows."""
    try:
        row = spark.sql(f"SELECT count(*) AS n, max(household_id) AS m FROM {fqn}").first()
    except Exception as exc:
        # only a missing table means «write all four first»; permissions or transient errors keep their own
        # message, so nobody overwrites households/people/preferences by mistake
        if "TABLE_OR_VIEW_NOT_FOUND" not in str(exc):
            raise
        raise ValueError(
            f"{fqn} not found: write all four tables first (tables=None)"
        ) from exc
    expected = (len(dataset.households), max((h.household_id for h in dataset.households), default=None))
    found = (int(row["n"]), row["m"])
    if found != expected:
        raise ValueError(
            f"{fqn} has (rows, max id) {found} but this GenerateConfig produces {expected}: "
            "use the same scale/seed as the existing tables, or rewrite all four"
        )


def write_uc(
    *,
    catalog: str,
    schema: str,
    cfg: GenerateConfig,
    spark: Any,
    tables: str | tuple[str, ...] | list[str] | None = None,
    arrow: bool = True,
) -> dict[str, int]:
    """Create schema if needed and overwrite CDF-enabled Delta tables via Spark.

    tables: subset to overwrite (default all four), e.g. ("journeys",) to regenerate routes only.
    A partial rewrite requires the existing households table to match cfg (same scale/seed).
    Lakebase synced tables (<table>_sync) in SNAPSHOT/TRIGGERED mode do not pick up the new
    rows on their own: refresh them after a rewrite.
    arrow: ship each table as a typed pandas/Arrow frame (fast); False = list of dicts (legacy).
    Does not INSERT row-by-row. Raises CatalogNotFoundError without creating a catalog.
    """
    catalog, schema = _validate_names(catalog, schema)
    only = _normalize_tables(tables)
    dataset: NavidadDataset = generate(cfg)
    rows_by_table = dataset_tables(dataset)
    try:
        spark.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")
    except CatalogNotFoundError:
        raise
    except Exception as exc:
        raise CatalogNotFoundError(
            f"Catalog {catalog} does not exist or is not usable; "
            "the skill will not create catalogs"
        ) from exc
    if "households" not in only:
        _check_households_match(spark, f"{catalog}.{schema}.households", dataset)

    counts: dict[str, int] = {}
    for table in only:
        rows = rows_by_table[table]
        fqn = f"{catalog}.{schema}.{table}"
        if arrow:
            df = spark.createDataFrame(pandas_for(table, rows), schema=spark_schema_ddl(table))
        else:
            df = spark.createDataFrame(records_for(table, rows))
        (
            df.write.format("delta")
            .mode("overwrite")
            .option("overwriteSchema", "true")
            .saveAsTable(fqn)
        )
        spark.sql(
            f"ALTER TABLE {fqn} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)"
        )
        counts[table] = len(rows)
    return counts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Write Navidad hackathon tables to Unity Catalog via Spark"
    )
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=20260106)
    parser.add_argument(
        "--tables",
        default=",".join(TABLES),
        help="comma-separated subset to overwrite, e.g. journeys (needs matching scale/seed)",
    )
    args = parser.parse_args(argv)
    spark = globals().get("spark")
    if spark is None:
        print(
            json.dumps(
                {
                    "step": "write_uc",
                    "status": "error",
                    "error": (
                        "write_uc requires a Spark session. In a Databricks notebook, "
                        "read this file, then: "
                        "from write_uc import write_uc; "
                        "write_uc(spark=spark, catalog=..., schema=..., cfg=GenerateConfig(...))"
                    ),
                }
            )
        )
        raise SystemExit(1)
    try:
        counts = write_uc(
            catalog=args.catalog,
            schema=args.schema,
            cfg=GenerateConfig(scale=args.scale, seed=args.seed),
            spark=spark,
            tables=args.tables.split(","),
        )
        print(json.dumps({"step": "write_uc", "status": "ok", "counts": counts}))
    except Exception as exc:
        print(json.dumps({"step": "write_uc", "status": "error", "error": str(exc)}))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main(sys.argv[1:])
