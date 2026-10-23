"""Hand-made tables for the analytics unit tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import duckdb
import polars as pl

from etfh.build import DDL_SOURCES
from etfh.schema import (
    DDL_FILE_VERSIONS,
    DDL_FUND_METRICS,
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

_METRICS_SCHEMA: dict[str, Any] = {
    "fund_ticker": pl.String,
    "as_of_date": pl.Date,
    "known_at": pl.Datetime("us", "UTC"),
    "file_version": pl.Int32,
    "nav_per_unit": pl.Float64,
    "shares_outstanding": pl.Float64,
    "aum": pl.Float64,
    "currency": pl.String,
    "source_id": pl.String,
    "raw_file_sha256": pl.String,
}


def metric(
    fund: str,
    as_of: date,
    *,
    nav: float | None = None,
    shares: float | None = None,
    aum: float | None = None,
    known_at: datetime = KNOWN_AT,
    file_version: int = 1,
    source_id: str = "metrics",
    currency: str | None = "CAD",
) -> dict[str, Any]:
    return {
        "fund_ticker": fund,
        "as_of_date": as_of,
        "known_at": known_at,
        "file_version": file_version,
        "nav_per_unit": nav,
        "shares_outstanding": shares,
        "aum": aum,
        "currency": currency,
        "source_id": source_id,
        "raw_file_sha256": SHA,
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


_SOURCES_SCHEMA: dict[str, Any] = {
    "source_id": pl.String,
    "issuer": pl.String,
    "kind": pl.String,
    "format": pl.String,
}

_QA_EVENTS_SCHEMA: dict[str, Any] = {
    "raw_file_sha256": pl.String,
    "source_id": pl.String,
    "fund_ticker": pl.String,
    "as_of_date": pl.Date,
    "check": pl.String,
    "severity": pl.String,
    "detail": pl.String,
    "excluded": pl.Boolean,
}


def make_con(
    rows: list[dict[str, Any]] | None = None,
    changes: list[dict[str, Any]] | None = None,
    partial_list_shas: list[str] | None = None,
    metrics: list[dict[str, Any]] | None = None,
) -> duckdb.DuckDBPyConnection:
    """A database with the build's tables, the two test sources seeded.

    ``daily`` is a ``daily_holdings`` CSV source and ``quarterly`` a
    ``quarterly_disclosure`` PDF source, matching the source ids the hand-made
    rows use. ``partial_list_shas`` adds ``partial_list`` info events, the way
    the build records a daily source that publishes only part of the portfolio.
    ``metrics`` fills the additive ``fund_metrics`` table.
    """
    con = duckdb.connect(":memory:")
    for ddl in (
        DDL_FILE_VERSIONS,
        DDL_SOURCES,
        DDL_HOLDINGS,
        DDL_QA_EVENTS,
        DDL_HOLDINGS_CHANGES,
        DDL_FUND_METRICS,
    ):
        con.execute(ddl)
    holdings = pl.DataFrame(rows or [], schema=_HOLDINGS_SCHEMA)
    con.register("_holdings_rows", holdings)
    con.execute("INSERT INTO holdings SELECT * FROM _holdings_rows")
    con.unregister("_holdings_rows")
    changes_frame = pl.DataFrame(changes or [], schema=_CHANGES_SCHEMA)
    con.register("_change_rows", changes_frame)
    con.execute("INSERT INTO holdings_changes SELECT * FROM _change_rows")
    con.unregister("_change_rows")
    metrics_frame = pl.DataFrame(metrics or [], schema=_METRICS_SCHEMA)
    con.register("_metrics_rows", metrics_frame)
    con.execute("INSERT INTO fund_metrics SELECT * FROM _metrics_rows")
    con.unregister("_metrics_rows")
    sources = pl.DataFrame(
        [
            ("daily", "tests", "daily_holdings", "csv"),
            ("quarterly", "tests", "quarterly_disclosure", "pdf"),
        ],
        schema=_SOURCES_SCHEMA,
        orient="row",
    )
    con.register("_source_rows", sources)
    con.execute("INSERT INTO sources SELECT * FROM _source_rows")
    con.unregister("_source_rows")
    events = pl.DataFrame(
        [
            {
                "raw_file_sha256": sha,
                "source_id": "daily",
                "fund_ticker": "",
                "as_of_date": None,
                "check": "partial_list",
                "severity": "info",
                "detail": "partial_list=true",
                "excluded": False,
            }
            for sha in (partial_list_shas or [])
        ],
        schema=_QA_EVENTS_SCHEMA,
    )
    con.register("_qa_rows", events)
    con.execute("INSERT INTO qa_events SELECT * FROM _qa_rows")
    con.unregister("_qa_rows")
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
