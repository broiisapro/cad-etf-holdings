"""Hand-built DuckDB fixtures for the large-addition event tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import Any

import duckdb
import polars as pl

from etfh.build import DDL_SOURCES
from etfh.schema import DDL_FILE_VERSIONS, DDL_HOLDINGS

D0 = date(2026, 10, 1)
D1 = date(2026, 10, 2)
D2 = date(2026, 10, 5)
D3 = date(2026, 10, 6)
Q1 = date(2026, 6, 30)
Q2 = date(2026, 9, 30)

K0 = datetime(2026, 10, 1, 22, 0, tzinfo=UTC)
K1 = datetime(2026, 10, 2, 22, 0, tzinfo=UTC)
K2 = datetime(2026, 10, 5, 22, 0, tzinfo=UTC)
K3 = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)

_HOLDINGS_SCHEMA: dict[str, Any] = {
    "fund_ticker": pl.String,
    "as_of_date": pl.Date,
    "known_at": pl.Datetime("us", "UTC"),
    "file_version": pl.Int32,
    "security_id": pl.String,
    "security_name": pl.String,
    "security_ticker": pl.String,
    "exchange": pl.String,
    "isin": pl.String,
    "cusip": pl.String,
    "asset_class": pl.String,
    "sector": pl.String,
    "country": pl.String,
    "currency": pl.String,
    "weight": pl.Float64,
    "shares": pl.Float64,
    "market_value": pl.Float64,
    "source_id": pl.String,
    "raw_file_sha256": pl.String,
}

_FILE_VERSIONS_SCHEMA: dict[str, Any] = {
    "source_id": pl.String,
    "fund_ticker": pl.String,
    "as_of_date": pl.Date,
    "file_version": pl.Int32,
    "known_at": pl.Datetime("us", "UTC"),
    "raw_file_sha256": pl.String,
    "path": pl.String,
    "status": pl.String,
}

_SOURCES_SCHEMA: dict[str, Any] = {
    "source_id": pl.String,
    "issuer": pl.String,
    "kind": pl.String,
    "format": pl.String,
}

_DEFAULT_SOURCES: tuple[tuple[str, str, str, str], ...] = (
    ("daily", "tests", "daily_holdings", "csv"),
    ("quarterly", "tests", "quarterly_disclosure", "pdf"),
    ("syn_pdf", "synthetic", "synthetic", "pdf"),
)


def holding(
    security_id: str | None,
    weight: float,
    *,
    name: str | None = None,
    ticker: str | None = None,
    exchange: str | None = None,
    asset_class: str = "equity",
    currency: str = "CAD",
) -> dict[str, Any]:
    """One parsed holding row (version fields are filled in by :func:`portfolio`)."""
    return {
        "security_id": security_id,
        "security_name": name if name is not None else (security_id or "Unnamed"),
        "security_ticker": ticker,
        "exchange": exchange,
        "asset_class": asset_class,
        "sector": "Technology",
        "country": "CA",
        "currency": currency,
        "weight": weight,
    }


def portfolio(
    fund: str,
    as_of: date,
    known_at: datetime,
    rows: list[dict[str, Any]],
    *,
    file_version: int = 1,
    source_id: str = "daily",
    status: str = "included",
    sha: str | None = None,
) -> dict[str, Any]:
    """One stored file version and its holdings rows."""
    return {
        "fund_ticker": fund,
        "as_of_date": as_of,
        "known_at": known_at,
        "file_version": file_version,
        "source_id": source_id,
        "status": status,
        "sha": sha or _sha(source_id, fund, as_of, file_version),
        "rows": rows,
    }


def _sha(source_id: str, fund: str, as_of: date, file_version: int) -> str:
    material = f"{source_id}|{fund}|{as_of.isoformat()}|{file_version}"
    return hashlib.sha256(material.encode()).hexdigest()


def make_con(
    portfolios: list[dict[str, Any]],
    *,
    sources: list[tuple[str, str, str, str]] | None = None,
) -> duckdb.DuckDBPyConnection:
    """A database with ``file_versions``, ``sources`` and ``holdings`` seeded."""
    con = duckdb.connect(":memory:")
    for ddl in (DDL_FILE_VERSIONS, DDL_SOURCES, DDL_HOLDINGS):
        con.execute(ddl)

    source_rows = list(_DEFAULT_SOURCES)
    for extra in sources or []:
        source_rows = [row for row in source_rows if row[0] != extra[0]]
        source_rows.append(extra)
    sources_frame = pl.DataFrame(source_rows, schema=_SOURCES_SCHEMA, orient="row")
    con.register("_source_rows", sources_frame)
    con.execute("INSERT INTO sources SELECT * FROM _source_rows")
    con.unregister("_source_rows")

    version_rows: list[dict[str, Any]] = []
    holding_rows: list[dict[str, Any]] = []
    for item in portfolios:
        version_rows.append(
            {
                "source_id": item["source_id"],
                "fund_ticker": item["fund_ticker"],
                "as_of_date": item["as_of_date"],
                "file_version": item["file_version"],
                "known_at": item["known_at"],
                "raw_file_sha256": item["sha"],
                "path": f"raw/tests/{item['fund_ticker']}/{item['as_of_date']}.test",
                "status": item["status"],
            }
        )
        for row in item["rows"]:
            holding_rows.append(
                {
                    "fund_ticker": item["fund_ticker"],
                    "as_of_date": item["as_of_date"],
                    "known_at": item["known_at"],
                    "file_version": item["file_version"],
                    "security_id": row["security_id"],
                    "security_name": row["security_name"],
                    "security_ticker": row["security_ticker"],
                    "exchange": row["exchange"],
                    "isin": None,
                    "cusip": None,
                    "asset_class": row["asset_class"],
                    "sector": row["sector"],
                    "country": row["country"],
                    "currency": row["currency"],
                    "weight": row["weight"],
                    "shares": None,
                    "market_value": None,
                    "source_id": item["source_id"],
                    "raw_file_sha256": item["sha"],
                }
            )

    versions = pl.DataFrame(version_rows, schema=_FILE_VERSIONS_SCHEMA)
    con.register("_version_rows", versions)
    con.execute("INSERT INTO file_versions SELECT * FROM _version_rows")
    con.unregister("_version_rows")

    holdings = pl.DataFrame(holding_rows, schema=_HOLDINGS_SCHEMA)
    con.register("_holding_rows", holdings)
    con.execute("INSERT INTO holdings SELECT * FROM _holding_rows")
    con.unregister("_holding_rows")
    return con
