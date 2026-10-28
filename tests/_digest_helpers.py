"""Hand-made tables and registries for the digest tests.

A digest needs four things from the database: ``holdings`` rows with their
point-in-time keys, the ``sources`` table that tells daily files from quarterly
disclosures, ``file_versions`` for the quality counts, and ``fund_metrics`` for
the flow estimates. The helpers build each one explicitly, so a test can pin the
``known_at``, ``file_version`` and as-of date of every snapshot and check the
comparison against hand-computed numbers.

Everything here is fictional: issuer ``tests``, tickers ``TESTA``/``TESTB``,
ISIN-style ids that are not real securities.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

import duckdb
import polars as pl

from etfh.build import DDL_SOURCES
from etfh.registry import Fund, Source, TermsRecord
from etfh.schema import (
    DDL_FILE_VERSIONS,
    DDL_FUND_METRICS,
    DDL_HOLDINGS,
    DDL_HOLDINGS_CHANGES,
    DDL_QA_EVENTS,
)
from etfh.types import AssetClass, Decision, FileFormat, SourceKind

DAY = date(2026, 10, 30)
"""A Friday: the week end most tests use."""

WEEK_START = date(2026, 10, 23)
WEEK_END = DAY

DAILY = "daily"
QUARTERLY = "quarterly"
METRICS = "metrics"

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

_FILE_SCHEMA: dict[str, Any] = {
    "source_id": pl.String,
    "fund_ticker": pl.String,
    "as_of_date": pl.Date,
    "file_version": pl.Int32,
    "known_at": pl.Datetime("us", "UTC"),
    "raw_file_sha256": pl.String,
    "path": pl.String,
    "status": pl.String,
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

_SOURCES_SCHEMA: dict[str, Any] = {
    "source_id": pl.String,
    "issuer": pl.String,
    "kind": pl.String,
    "format": pl.String,
}

_EVENTS_SCHEMA: dict[str, Any] = {
    "raw_file_sha256": pl.String,
    "source_id": pl.String,
    "fund_ticker": pl.String,
    "as_of_date": pl.Date,
    "check": pl.String,
    "severity": pl.String,
    "detail": pl.String,
    "excluded": pl.Boolean,
}

#: Known-at time of a file published on its own as-of date: 22:00 UTC, which is
#: after the 04:00/05:00 UTC cutoff of that day and before the next one.
_PUBLISH_HOUR = 22


def published(as_of: date) -> datetime:
    """The ``known_at`` of a file published on its own as-of date."""
    return datetime(as_of.year, as_of.month, as_of.day, _PUBLISH_HOUR, tzinfo=UTC)


def version_sha(source_id: str, fund: str, as_of: date, file_version: int = 1) -> str:
    """Deterministic, valid SHA-256-shaped hash for one file version."""
    text = f"{source_id}|{fund}|{as_of.isoformat()}|{file_version}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def holding(
    fund: str,
    security_id: str | None,
    weight: float,
    *,
    as_of: date = DAY,
    known_at: datetime | None = None,
    file_version: int = 1,
    source_id: str = DAILY,
    name: str | None = None,
    ticker: str | None = None,
    exchange: str | None = None,
    asset_class: str = "equity",
    sector: str = "Technology",
    country: str = "CA",
    currency: str = "CAD",
) -> dict[str, Any]:
    """One ``holdings`` row; ``name`` defaults to the id so tests stay readable."""
    return {
        "fund_ticker": fund,
        "as_of_date": as_of,
        "known_at": published(as_of) if known_at is None else known_at,
        "file_version": file_version,
        "security_id": security_id,
        "security_name": name if name is not None else (security_id or "Unnamed"),
        "security_ticker": ticker,
        "exchange": exchange,
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
        "raw_file_sha256": version_sha(source_id, fund, as_of, file_version),
    }


def cash(fund: str, weight: float, **kwargs: Any) -> dict[str, Any]:
    """A cash line (D33): never a security, never part of counts or turnover."""
    currency = kwargs.pop("currency", "CAD")
    return holding(
        fund,
        f"CASH:{currency}",
        weight,
        name="Cash",
        asset_class="cash",
        sector="Unknown",
        country="Unknown",
        currency=currency,
        **kwargs,
    )


def metric(
    fund: str,
    as_of: date,
    *,
    nav: float | None = None,
    shares: float | None = None,
    known_at: datetime | None = None,
    file_version: int = 1,
    source_id: str = METRICS,
) -> dict[str, Any]:
    """One ``fund_metrics`` row (D66)."""
    return {
        "fund_ticker": fund,
        "as_of_date": as_of,
        "known_at": published(as_of) if known_at is None else known_at,
        "file_version": file_version,
        "nav_per_unit": nav,
        "shares_outstanding": shares,
        "aum": None,
        "currency": "CAD",
        "source_id": source_id,
        "raw_file_sha256": version_sha(source_id, fund, as_of, file_version),
    }


def source(
    source_id: str = DAILY,
    *,
    issuer: str = "tests",
    kind: SourceKind = SourceKind.daily_holdings,
    file_format: FileFormat = FileFormat.csv,
    redistribution: bool = False,
) -> Source:
    """A registry ``Source``; ``redistribution`` is the D79 terms flag."""
    return Source(
        id=source_id,
        issuer=issuer,
        kind=kind,
        format=file_format,
        url_patterns=[f"https://fixtures.invalid/{source_id}/<TICKER>"],
        update_frequency="daily",
        supported=True,
        terms=TermsRecord(
            url=f"https://fixtures.invalid/{source_id}/terms",
            reviewer="test fixture",
            reviewed_on=date(2026, 10, 16),
            decision=Decision.allowed,
            redistribution_allowed=redistribution,
        ),
    )


def fund(
    ticker: str,
    *,
    issuer: str = "tests",
    name: str | None = None,
    source_ids: Sequence[str] = (DAILY,),
    asset_class: AssetClass = AssetClass.equity,
) -> Fund:
    """A registry ``Fund``."""
    return Fund(
        ticker=ticker,
        issuer=issuer,
        name=name if name is not None else f"{ticker} Fund",
        asset_class=asset_class,
        source_ids=tuple(source_ids),
    )


def sources_registry(*extra: Source) -> dict[str, Source]:
    """The three test sources, optionally plus extras, keyed by id."""
    base = [
        source(DAILY),
        source(
            QUARTERLY,
            kind=SourceKind.quarterly_disclosure,
            file_format=FileFormat.pdf,
        ),
        source(METRICS, kind=SourceKind.fund_metrics, file_format=FileFormat.csv),
    ]
    registry = {item.id: item for item in base}
    for item in extra:
        registry[item.id] = item
    return registry


def make_con(
    rows: Sequence[Mapping[str, Any]] | None = None,
    *,
    metrics: Sequence[Mapping[str, Any]] | None = None,
    sources: Sequence[tuple[str, str, str, str]] | None = None,
    excluded: Iterable[tuple[str, date]] = (),
    excluded_source_id: str = DAILY,
    partial_shas: Sequence[str] = (),
) -> duckdb.DuckDBPyConnection:
    """A database with the build's tables and a matching ``file_versions`` log.

    ``excluded`` adds an excluded ``file_versions`` row per ``(fund, as_of)``,
    the way the build records a file that failed validation; ``partial_shas``
    adds ``partial_list`` info events. ``sources`` defaults to the three test
    sources (``daily``, ``quarterly``, ``metrics``).
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

    holdings_rows = [dict(row) for row in rows or []]
    _insert(con, "holdings", pl.DataFrame(holdings_rows, schema=_HOLDINGS_SCHEMA))
    _insert(
        con,
        "fund_metrics",
        pl.DataFrame([dict(row) for row in metrics or []], schema=_METRICS_SCHEMA),
    )

    table = sources or [
        (DAILY, "tests", "daily_holdings", "csv"),
        (QUARTERLY, "tests", "quarterly_disclosure", "pdf"),
        (METRICS, "tests", "fund_metrics", "csv"),
    ]
    _insert(
        con,
        "sources",
        pl.DataFrame(list(table), schema=_SOURCES_SCHEMA, orient="row"),
    )

    versions: dict[tuple[str, str, date, int], dict[str, Any]] = {}
    for row in holdings_rows:
        key = (row["source_id"], row["fund_ticker"], row["as_of_date"], row["file_version"])
        versions.setdefault(
            key,
            {
                "source_id": key[0],
                "fund_ticker": key[1],
                "as_of_date": key[2],
                "file_version": key[3],
                "known_at": row["known_at"],
                "raw_file_sha256": row["raw_file_sha256"],
                "path": f"{key[1]}/{key[2].isoformat()}/{key[3]}.csv",
                "status": "included",
            },
        )
    for fund_ticker, as_of in excluded:
        versions[(excluded_source_id, fund_ticker, as_of, 1)] = {
            "source_id": excluded_source_id,
            "fund_ticker": fund_ticker,
            "as_of_date": as_of,
            "file_version": 1,
            "known_at": published(as_of),
            "raw_file_sha256": version_sha(excluded_source_id, fund_ticker, as_of),
            "path": f"{fund_ticker}/{as_of.isoformat()}/excluded.csv",
            "status": "excluded",
        }
    _insert(con, "file_versions", pl.DataFrame(list(versions.values()), schema=_FILE_SCHEMA))

    events = [
        {
            "raw_file_sha256": sha,
            "source_id": DAILY,
            "fund_ticker": "",
            "as_of_date": None,
            "check": "partial_list",
            "severity": "info",
            "detail": "partial_list=true",
            "excluded": False,
        }
        for sha in partial_shas
    ]
    _insert(con, "qa_events", pl.DataFrame(events, schema=_EVENTS_SCHEMA))
    return con


def _insert(con: duckdb.DuckDBPyConnection, table: str, frame: pl.DataFrame) -> None:
    con.register(f"_{table}_rows", frame)
    con.execute(f"INSERT INTO {table} SELECT * FROM _{table}_rows")
    con.unregister(f"_{table}_rows")


def two_snapshot_store() -> list[dict[str, Any]]:
    """``TESTA`` held A/B/C at week start and B/C/D at week end.

    Weights: A 0.5 -> 0 (drop), D 0 -> 0.1 (add), B 0.3 -> 0.25 (-0.05),
    C 0.15 -> 0.65 (+0.50). Turnover is 0.5 * (0.5 + 0.1 + 0.05 + 0.5) = 0.575.
    """
    return [
        holding("TESTA", "ISIN:AA0000000001", 0.50, as_of=WEEK_START, name="Alpha Corp"),
        holding("TESTA", "ISIN:AA0000000002", 0.30, as_of=WEEK_START, name="Bravo Corp"),
        holding("TESTA", "ISIN:AA0000000003", 0.15, as_of=WEEK_START, name="Charlie Corp"),
        cash("TESTA", 0.05, as_of=WEEK_START),
        holding("TESTA", "ISIN:AA0000000002", 0.25, as_of=DAY, name="Bravo Corp"),
        holding("TESTA", "ISIN:AA0000000003", 0.65, as_of=DAY, name="Charlie Corp"),
        holding("TESTA", "ISIN:AA0000000004", 0.10, as_of=DAY, name="Delta Corp"),
    ]


def overlapping_store() -> list[dict[str, Any]]:
    """Three funds whose pairwise overlaps move by 0.10, 0.10 and -0.05.

    Comparing the two snapshots gives ``TESTA``/``TESTB`` +0.10,
    ``TESTB``/``TESTC`` +0.10 and ``TESTA``/``TESTC`` -0.05, so the document's
    ordering rule (size, then ticker pair) is observable.
    """
    rows: list[dict[str, Any]] = []
    rows += [
        holding("TESTA", "ISIN:AA0000000001", 0.50, as_of=WEEK_START),
        holding("TESTA", "ISIN:AA0000000002", 0.30, as_of=WEEK_START),
        holding("TESTA", "ISIN:AA0000000003", 0.15, as_of=WEEK_START),
        holding("TESTA", "ISIN:AA0000000002", 0.25, as_of=DAY),
        holding("TESTA", "ISIN:AA0000000003", 0.65, as_of=DAY),
        holding("TESTA", "ISIN:AA0000000004", 0.10, as_of=DAY),
    ]
    rows += [
        holding("TESTB", "ISIN:AA0000000001", 0.20, as_of=WEEK_START),
        holding("TESTB", "ISIN:AA0000000002", 0.60, as_of=WEEK_START),
        holding("TESTB", "ISIN:AA0000000003", 0.20, as_of=WEEK_START),
        holding("TESTB", "ISIN:AA0000000001", 0.10, as_of=DAY),
        holding("TESTB", "ISIN:AA0000000002", 0.40, as_of=DAY),
        holding("TESTB", "ISIN:AA0000000003", 0.50, as_of=DAY),
    ]
    rows += [
        holding("TESTC", "ISIN:AA0000000001", 0.30, as_of=WEEK_START),
        holding("TESTC", "ISIN:AA0000000002", 0.10, as_of=WEEK_START),
        holding("TESTC", "ISIN:AA0000000003", 0.60, as_of=WEEK_START),
        holding("TESTC", "ISIN:AA0000000001", 0.50, as_of=DAY),
        holding("TESTC", "ISIN:AA0000000002", 0.10, as_of=DAY),
        holding("TESTC", "ISIN:AA0000000003", 0.40, as_of=DAY),
    ]
    return rows
