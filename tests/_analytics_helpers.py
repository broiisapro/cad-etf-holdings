"""Hand-made tables for the analytics unit tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import duckdb
import polars as pl

from etfh.schema import (
    DDL_FILE_VERSIONS,
    DDL_HOLDINGS,
    DDL_HOLDINGS_CHANGES,
    DDL_QA_EVENTS,
)

AS_OF = date(2026, 10, 30)
KNOWN_AT = datetime(2026, 10, 30, 22, 0, tzinfo=UTC)
SHA = "a" * 64

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

_CHANGES_SCHEMA: dict[str, Any] = {
    "fund_ticker": pl.String,
    "from_as_of": pl.Date,
    "to_as_of": pl.Date,
    "security_id": pl.String,
    "change_type": pl.String,
    "weight_before": pl.Float64,
    "weight_after": pl.Float64,
    "delta": pl.Float64,
}


def holding(
    fund: str,
    security_id: str | None,
    weight: float,
    *,
    as_of: date = AS_OF,
    known_at: datetime = KNOWN_AT,
    file_version: int = 1,
    source_id: str = "daily",
    name: str | None = None,
    sector: str = "Technology",
    country: str = "CA",
    asset_class: str = "equity",
    currency: str = "CAD",
) -> dict[str, Any]:
    return {
        "fund_ticker": fund,
        "as_of_date": as_of,
        "known_at": known_at,
        "file_version": file_version,
        "security_id": security_id,
        "security_name": name if name is not None else (security_id or "Unnamed"),
        "security_ticker": None,
        "exchange": None,
        "isin": None,
        "cusip": None,
        "asset_class": asset_class,
        "sector": sector,
        "country": country,
        "currency": currency,
        "weight": weight,
        "shares": None,
        "market_value": None,
        "source_id": source_id,
        "raw_file_sha256": SHA,
    }


def change(
    fund: str,
    from_as_of: date,
    to_as_of: date,
    security_id: str | None,
    change_type: str,
    weight_before: float | None,
    weight_after: float | None,
    delta: float | None,
) -> dict[str, Any]:
    return {
        "fund_ticker": fund,
        "from_as_of": from_as_of,
        "to_as_of": to_as_of,
        "security_id": security_id,
        "change_type": change_type,
        "weight_before": weight_before,
        "weight_after": weight_after,
        "delta": delta,
    }


def make_con(
    rows: list[dict[str, Any]] | None = None,
    changes: list[dict[str, Any]] | None = None,
) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    for ddl in (DDL_FILE_VERSIONS, DDL_HOLDINGS, DDL_QA_EVENTS, DDL_HOLDINGS_CHANGES):
        con.execute(ddl)
    holdings = pl.DataFrame(rows or [], schema=_HOLDINGS_SCHEMA)
    con.register("_holdings_rows", holdings)
    con.execute("INSERT INTO holdings SELECT * FROM _holdings_rows")
    con.unregister("_holdings_rows")
    changes_frame = pl.DataFrame(changes or [], schema=_CHANGES_SCHEMA)
    con.register("_change_rows", changes_frame)
    con.execute("INSERT INTO holdings_changes SELECT * FROM _change_rows")
    con.unregister("_change_rows")
    return con


def standard_rows() -> list[dict[str, Any]]:
    """Funds X/Y (complete), Z (quarterly top holdings), P, Q (version cases)."""
    rows = [
        holding("XFUND", "ISIN:A", 0.50, name="Alpha", sector="Technology", country="CA"),
        holding("XFUND", "ISIN:B", 0.30, name="Bravo", sector="Energy", country="CA"),
        holding("XFUND", "ISIN:C", 0.15, name="Charlie", sector="Technology", country="US"),
        holding(
            "XFUND",
            "CASH:CAD",
            0.05,
            name="Cash",
            sector="Unknown",
            country="Unknown",
            asset_class="cash",
        ),
        holding("YFUND", "ISIN:A", 0.25, name="Alpha"),
        holding("YFUND", "ISIN:B", 0.25, name="Bravo", sector="Energy"),
        holding("YFUND", "ISIN:D", 0.45, name="Delta", sector="Technology", country="US"),
        holding(
            "YFUND",
            "CASH:CAD",
            0.05,
            name="Cash",
            sector="Unknown",
            country="Unknown",
            asset_class="cash",
        ),
        holding("ZFUND", "ISIN:A", 0.40, name="Alpha", source_id="quarterly"),
        holding("ZFUND", "ISIN:B", 0.20, name="Bravo", sector="Energy", source_id="quarterly"),
    ]
    rows += [
        holding(
            "PFUND",
            "ISIN:A",
            0.60,
            as_of=date(2026, 10, 29),
            known_at=datetime(2026, 10, 29, 22, tzinfo=UTC),
        ),
        holding(
            "PFUND",
            "ISIN:B",
            0.40,
            as_of=date(2026, 10, 29),
            known_at=datetime(2026, 10, 29, 22, tzinfo=UTC),
        ),
        holding(
            "PFUND",
            "ISIN:A",
            0.35,
            as_of=date(2026, 10, 31),
            known_at=datetime(2026, 10, 31, 22, tzinfo=UTC),
            source_id="quarterly",
        ),
        holding(
            "PFUND",
            "ISIN:B",
            0.25,
            as_of=date(2026, 10, 31),
            known_at=datetime(2026, 10, 31, 22, tzinfo=UTC),
            source_id="quarterly",
        ),
    ]
    rows += [
        holding(
            "QFUND",
            "ISIN:A",
            0.70,
            as_of=date(2026, 11, 1),
            known_at=datetime(2026, 11, 1, 22, tzinfo=UTC),
        ),
        holding(
            "QFUND",
            "ISIN:B",
            0.30,
            as_of=date(2026, 11, 1),
            known_at=datetime(2026, 11, 1, 22, tzinfo=UTC),
        ),
        holding(
            "QFUND",
            "ISIN:A",
            0.10,
            as_of=date(2026, 11, 5),
            known_at=datetime(2026, 11, 6, 12, tzinfo=UTC),
        ),
        holding(
            "QFUND",
            "ISIN:B",
            0.90,
            as_of=date(2026, 11, 5),
            known_at=datetime(2026, 11, 6, 12, tzinfo=UTC),
        ),
    ]
    return rows
