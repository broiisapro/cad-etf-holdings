"""Helpers for dashboard tests: a fake analytics module and a DuckDB file."""

from __future__ import annotations

import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import duckdb
import polars as pl

from etfh import analytics as _real_analytics

_REAL_FLOW_METHOD = _real_analytics.FLOW_METHOD
_REAL_FUND_NOT_FOUND = _real_analytics.FundNotFoundError

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = REPO_ROOT / "etfh" / "dashboard" / "app.py"

AS_OF = date(2026, 11, 2)
FUNDS = ["SYNA", "SYNB", "SYNC"]
PARTIAL_FUNDS = frozenset({"SYNC"})

METRICS_HISTORY: dict[str, list[dict[str, object]]] = {
    "SYNA": [
        {
            "as_of_date": date(2026, 10, 30),
            "nav_per_unit": 25.0,
            "shares_outstanding": 1_000_000.0,
            "aum": 25_000_000.0,
            "currency": "CAD",
        },
        {
            "as_of_date": AS_OF,
            "nav_per_unit": 25.5,
            "shares_outstanding": 1_040_000.0,
            "aum": 26_520_000.0,
            "currency": "CAD",
        },
    ],
}
"""A two-date metrics look-alike for one fake fund; other funds have none."""

HOLDINGS: dict[str, list[tuple[str, str, float]]] = {
    "SYNA": [
        ("ISIN:CA600000AA15", "Aurora Materials Inc.", 0.60),
        ("TKR:XTSE:BRZ", "Boreal Zinc Corp.", 0.40),
    ],
    "SYNB": [
        ("ISIN:CA600000AA15", "Aurora Materials Inc.", 0.50),
        ("TKR:XNYS:CDR", "Cedar Systems Corp.", 0.50),
    ],
    "SYNC": [
        ("ISIN:CA600001AA97", "Cascade Utilities Ltd.", 0.70),
        ("CASH:CAD", "Cash", 0.30),
    ],
}

CHANGES_ROWS: list[dict[str, object]] = [
    {
        "fund_ticker": "SYNA",
        "from_as_of": date(2026, 10, 30),
        "to_as_of": date(2026, 11, 2),
        "security_id": "TKR:XTSE:BRZ",
        "change_type": "add",
        "weight_before": None,
        "weight_after": 0.40,
        "delta": 0.40,
    },
    {
        "fund_ticker": "SYNA",
        "from_as_of": date(2026, 10, 30),
        "to_as_of": date(2026, 11, 2),
        "security_id": "ISIN:CA600000AA15",
        "change_type": "weight_change",
        "weight_before": 0.55,
        "weight_after": 0.60,
        "delta": 0.05,
    },
]

SECTORS = pl.DataFrame({"key": ["Materials", "Industrials"], "weight": [0.60, 0.40]})
COUNTRIES = pl.DataFrame({"key": ["Canada", "United States"], "weight": [0.70, 0.30]})


@dataclass(frozen=True)
class FakeConcentration:
    fund: str
    asof: date
    as_of_date: date | None
    n_holdings: int
    top10_weight: float
    hhi: float
    partial: bool


@dataclass(frozen=True)
class FakeOverlap:
    fund_a: str
    fund_b: str
    asof: date
    overlap: float
    common: pl.DataFrame
    partial: bool


def _weights(fund: str) -> dict[str, float]:
    return {security_id: weight for security_id, _, weight in HOLDINGS[fund]}


def _names(fund: str) -> dict[str, str]:
    return {security_id: name for security_id, name, _ in HOLDINGS[fund]}


def funds_available(con: Any) -> list[str]:
    """Fake ``etfh.analytics.funds_available``."""
    return list(FUNDS)


def holdings_asof(con: Any, fund: str, asof: date) -> pl.DataFrame:
    """Fake ``etfh.analytics.holdings_asof``."""
    return pl.DataFrame(
        {
            "security_id": [security_id for security_id, _, _ in HOLDINGS[fund]],
            "security_name": [name for _, name, _ in HOLDINGS[fund]],
            "weight": [weight for _, _, weight in HOLDINGS[fund]],
        }
    )


def concentration(con: Any, fund: str, asof: date) -> FakeConcentration:
    """Fake ``etfh.analytics.concentration``."""
    weights = list(_weights(fund).values())
    return FakeConcentration(
        fund=fund,
        asof=asof,
        as_of_date=AS_OF,
        n_holdings=len(weights),
        top10_weight=sum(sorted(weights, reverse=True)[:10]),
        hhi=sum(weight * weight for weight in weights),
        partial=fund in PARTIAL_FUNDS,
    )


def breakdown(con: Any, fund: str, asof: date, by: str) -> pl.DataFrame:
    """Fake ``etfh.analytics.breakdown``."""
    return SECTORS if by == "sector" else COUNTRIES


def overlap(con: Any, fund_a: str, fund_b: str, asof: date) -> FakeOverlap:
    """Fake ``etfh.analytics.overlap``."""
    weights_a = _weights(fund_a)
    weights_b = _weights(fund_b)
    names = _names(fund_a) | _names(fund_b)
    common_ids = sorted(set(weights_a) & set(weights_b))
    common = pl.DataFrame(
        {
            "security_id": common_ids,
            "security_name": [names[security_id] for security_id in common_ids],
            "weight_a": [weights_a[security_id] for security_id in common_ids],
            "weight_b": [weights_b[security_id] for security_id in common_ids],
        }
    )
    return FakeOverlap(
        fund_a=fund_a,
        fund_b=fund_b,
        asof=asof,
        overlap=sum(
            min(weights_a[security_id], weights_b[security_id]) for security_id in common_ids
        ),
        common=common,
        partial=fund_a in PARTIAL_FUNDS or fund_b in PARTIAL_FUNDS,
    )


def overlap_matrix(con: Any, funds: Sequence[str], asof: date) -> pl.DataFrame:
    """Fake ``etfh.analytics.overlap_matrix``."""
    return pl.DataFrame(
        {
            "fund_a": [fund_a for fund_a in funds for _ in funds],
            "fund_b": [fund_b for _ in funds for fund_b in funds],
            "overlap": [
                overlap(con, fund_a, fund_b, asof).overlap for fund_a in funds for fund_b in funds
            ],
        }
    )


def changes(con: Any, fund: str, start: date, end: date) -> pl.DataFrame:
    """Fake ``etfh.analytics.changes``."""
    rows = [row for row in CHANGES_ROWS if row["fund_ticker"] == fund]
    return pl.DataFrame(rows)


def _business_days_between(previous: date, current: date) -> int:
    """Weekdays in ``(previous, current]``, like the D67 gap."""
    days = 0
    day = previous
    while day < current:
        day += timedelta(days=1)
        if day.weekday() < 5:
            days += 1
    return days


def metrics_asof(con: Any, fund: str, asof: date) -> pl.DataFrame:
    """Fake ``etfh.analytics.metrics_asof`` over :data:`METRICS_HISTORY`."""
    rows = METRICS_HISTORY.get(fund)
    if not rows:
        raise _REAL_FUND_NOT_FOUND(fund)
    return pl.DataFrame(
        {
            "fund_ticker": [fund] * len(rows),
            "as_of_date": [row["as_of_date"] for row in rows],
            "known_at": [datetime(2026, 11, 2, 22, tzinfo=UTC)] * len(rows),
            "file_version": [1] * len(rows),
            "nav_per_unit": [row["nav_per_unit"] for row in rows],
            "shares_outstanding": [row["shares_outstanding"] for row in rows],
            "aum": [row["aum"] for row in rows],
            "currency": [row["currency"] for row in rows],
            "source_id": ["syn_metrics"] * len(rows),
            "raw_file_sha256": ["a" * 64] * len(rows),
        }
    )


def flows(
    con: Any,
    fund: str,
    start: date,
    end: date,
    *,
    asof: date | None = None,
) -> pl.DataFrame:
    """Fake ``etfh.analytics.flows`` with the D67 formula over the fake history."""
    history = METRICS_HISTORY.get(fund)
    if not history:
        raise _REAL_FUND_NOT_FOUND(fund)
    share_dates = [
        (row["as_of_date"], row["shares_outstanding"])
        for row in history
        if row["shares_outstanding"] is not None
    ]
    rows: list[dict[str, object]] = []
    for row in history:
        as_of = row["as_of_date"]
        if not isinstance(as_of, date) or not start <= as_of <= end:
            continue
        previous = [point for point in share_dates if point[0] < as_of]
        previous_date, previous_shares = previous[-1] if previous else (None, None)
        shares = row["shares_outstanding"]
        nav = row["nav_per_unit"]
        estimate = None
        if previous_shares is not None and shares is not None and nav is not None:
            estimate = (float(shares) - float(previous_shares)) * float(nav)
        rows.append(
            {
                "as_of_date": as_of,
                "shares_outstanding": shares,
                "nav_per_unit": nav,
                "prev_as_of_date": previous_date,
                "prev_shares": previous_shares,
                "gap_days": (
                    None if previous_date is None else _business_days_between(previous_date, as_of)
                ),
                "flow_estimate": estimate,
                "method": _REAL_FLOW_METHOD,
            }
        )
    return pl.DataFrame(
        rows,
        schema={
            "as_of_date": pl.Date,
            "shares_outstanding": pl.Float64,
            "nav_per_unit": pl.Float64,
            "prev_as_of_date": pl.Date,
            "prev_shares": pl.Float64,
            "gap_days": pl.Int32,
            "flow_estimate": pl.Float64,
            "method": pl.String,
        },
    )


def build_fake_analytics() -> ModuleType:
    """Return a module object satisfying the analytics contract with fixed data."""
    module = ModuleType("etfh.analytics")
    module.funds_available = funds_available
    module.holdings_asof = holdings_asof
    module.concentration = concentration
    module.breakdown = breakdown
    module.overlap = overlap
    module.overlap_matrix = overlap_matrix
    module.changes = changes
    module.metrics_asof = metrics_asof
    module.flows = flows
    module.FLOW_METHOD = _REAL_FLOW_METHOD
    module.FundNotFoundError = _REAL_FUND_NOT_FOUND
    return module


@contextmanager
def install_fake_analytics() -> Iterator[ModuleType]:
    """Install a fake ``etfh.analytics`` module for the duration of the block.

    Modules that bind ``etfh.analytics`` at import time (the dashboard app and
    its quality panel) are evicted on entry and exit, so neither a cached fake
    nor a cached real module leaks into the next test.
    """
    import etfh

    saved_module = sys.modules.get("etfh.analytics")
    had_attr = hasattr(etfh, "analytics")
    saved_attr = getattr(etfh, "analytics", None)
    fake = build_fake_analytics()
    sys.modules["etfh.analytics"] = fake
    if had_attr:
        delattr(etfh, "analytics")
    _evict_dashboard_modules()
    try:
        yield fake
    finally:
        _evict_dashboard_modules()
        if saved_module is None:
            sys.modules.pop("etfh.analytics", None)
        else:
            sys.modules["etfh.analytics"] = saved_module
        if getattr(etfh, "analytics", None) is fake:
            if saved_attr is not None:
                etfh.analytics = saved_attr
            else:
                delattr(etfh, "analytics")


def _evict_dashboard_modules() -> None:
    """Drop dashboard modules that hold a bound ``etfh.analytics`` reference."""
    import etfh.dashboard

    for name in ("app", "quality"):
        sys.modules.pop(f"etfh.dashboard.{name}", None)
        if getattr(etfh.dashboard, name, None) is not None:
            delattr(etfh.dashboard, name)


def make_empty_db(path: Path) -> Path:
    """Create a valid, empty DuckDB file the app can open read-only."""
    duckdb.connect(str(path)).close()
    return path


QUALITY_AS_OF = date(2026, 11, 2)
"""The as-of date the hand-made quality database is designed for."""

QUALITY_FUNDS = ["EXC", "HIGH", "LATE", "LOW", "QTR"]
"""Fund tickers in the quality database, in the order the dashboard lists them."""

_SHA = {
    "high": "1" * 64,
    "low": "2" * 64,
    "qtr": "3" * 64,
    "exc_ok": "4" * 64,
    "exc_bad": "5" * 64,
    "late": "6" * 64,
}

_HOLDINGS_COLUMNS = (
    "fund_ticker",
    "as_of_date",
    "known_at",
    "file_version",
    "security_id",
    "security_name",
    "security_ticker",
    "exchange",
    "isin",
    "cusip",
    "asset_class",
    "sector",
    "country",
    "currency",
    "weight",
    "shares",
    "market_value",
    "source_id",
    "raw_file_sha256",
)


def _quality_holding(
    fund: str,
    source: str,
    sha: str,
    *,
    security_id: str | None,
    name: str,
    weight: float,
    as_of: date = QUALITY_AS_OF,
    known_at: datetime = datetime(2026, 11, 2, 22, 0, tzinfo=UTC),
    asset_class: str = "equity",
) -> tuple[object, ...]:
    values: dict[str, object] = {
        "fund_ticker": fund,
        "as_of_date": as_of,
        "known_at": known_at,
        "file_version": 1,
        "security_id": security_id,
        "security_name": name,
        "security_ticker": None,
        "exchange": None,
        "isin": None,
        "cusip": None,
        "asset_class": asset_class,
        "sector": "Unknown",
        "country": "CA",
        "currency": "CAD",
        "weight": weight,
        "shares": None,
        "market_value": None,
        "source_id": source,
        "raw_file_sha256": sha,
    }
    return tuple(values[column] for column in _HOLDINGS_COLUMNS)


def make_quality_db(path: Path) -> Path:
    """A build-shaped database covering every data-quality state.

    ``HIGH`` has 60% unmatched weight, ``LOW`` 5%, ``QTR`` is a quarterly
    disclosure (50% unmatched), ``EXC`` has an excluded newer file and ``LATE``
    is only known after :data:`QUALITY_AS_OF`.
    """
    from etfh.build import DDL_SOURCES
    from etfh.schema import (
        DDL_FILE_VERSIONS,
        DDL_HOLDINGS,
        DDL_HOLDINGS_CHANGES,
        DDL_QA_EVENTS,
    )

    con = duckdb.connect(str(path))
    try:
        for ddl in (
            DDL_FILE_VERSIONS,
            DDL_SOURCES,
            DDL_HOLDINGS,
            DDL_QA_EVENTS,
            DDL_HOLDINGS_CHANGES,
        ):
            con.execute(ddl)
        sources = [
            ("alpha_daily", "tests", "daily_holdings", "csv"),
            ("beta_daily", "tests", "daily_holdings", "csv"),
            ("gamma_qpd", "tests", "quarterly_disclosure", "pdf"),
            ("delta_daily", "tests", "daily_holdings", "csv"),
            ("late_daily", "tests", "daily_holdings", "csv"),
        ]
        con.executemany("INSERT INTO sources VALUES (?, ?, ?, ?)", sources)
        versions = [
            (
                "alpha_daily",
                "HIGH",
                QUALITY_AS_OF,
                1,
                datetime(2026, 11, 2, 22, tzinfo=UTC),
                _SHA["high"],
                "raw/high.csv",
                "included",
            ),
            (
                "beta_daily",
                "LOW",
                QUALITY_AS_OF,
                1,
                datetime(2026, 11, 2, 22, tzinfo=UTC),
                _SHA["low"],
                "raw/low.csv",
                "included",
            ),
            (
                "gamma_qpd",
                "QTR",
                date(2026, 9, 30),
                1,
                datetime(2026, 10, 15, 12, tzinfo=UTC),
                _SHA["qtr"],
                "raw/qtr.pdf",
                "included",
            ),
            (
                "delta_daily",
                "EXC",
                date(2026, 10, 30),
                1,
                datetime(2026, 10, 30, 22, tzinfo=UTC),
                _SHA["exc_ok"],
                "raw/exc-ok.csv",
                "included",
            ),
            (
                "delta_daily",
                "EXC",
                QUALITY_AS_OF,
                2,
                datetime(2026, 11, 2, 22, tzinfo=UTC),
                _SHA["exc_bad"],
                "raw/exc-bad.csv",
                "excluded",
            ),
            (
                "late_daily",
                "LATE",
                date(2026, 11, 5),
                1,
                datetime(2026, 11, 6, 12, tzinfo=UTC),
                _SHA["late"],
                "raw/late.csv",
                "included",
            ),
        ]
        con.executemany("INSERT INTO file_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", versions)
        holdings = [
            _quality_holding(
                "HIGH",
                "alpha_daily",
                _SHA["high"],
                security_id=None,
                name="Mystery Corp.",
                weight=0.60,
            ),
            _quality_holding(
                "HIGH",
                "alpha_daily",
                _SHA["high"],
                security_id="ISIN:CA600000AA15",
                name="Aurora Materials Inc.",
                weight=0.40,
            ),
            _quality_holding(
                "LOW",
                "beta_daily",
                _SHA["low"],
                security_id=None,
                name="Mystery Corp.",
                weight=0.05,
            ),
            _quality_holding(
                "LOW",
                "beta_daily",
                _SHA["low"],
                security_id="ISIN:CA600000AA15",
                name="Aurora Materials Inc.",
                weight=0.95,
            ),
            _quality_holding(
                "QTR",
                "gamma_qpd",
                _SHA["qtr"],
                security_id=None,
                name="Cascade Utilities Ltd.",
                weight=0.50,
                as_of=date(2026, 9, 30),
                known_at=datetime(2026, 10, 15, 12, tzinfo=UTC),
            ),
            _quality_holding(
                "QTR",
                "gamma_qpd",
                _SHA["qtr"],
                security_id="ISIN:CA600001AA97",
                name="Cascade Utilities Ltd.",
                weight=0.50,
                as_of=date(2026, 9, 30),
                known_at=datetime(2026, 10, 15, 12, tzinfo=UTC),
            ),
            _quality_holding(
                "EXC",
                "delta_daily",
                _SHA["exc_ok"],
                security_id="ISIN:CA600002AA88",
                name="Granite Systems Inc.",
                weight=1.0,
                as_of=date(2026, 10, 30),
                known_at=datetime(2026, 10, 30, 22, tzinfo=UTC),
            ),
            _quality_holding(
                "LATE",
                "late_daily",
                _SHA["late"],
                security_id="ISIN:CA600003AA62",
                name="Harbour Lines Ltd.",
                weight=1.0,
                as_of=date(2026, 11, 5),
                known_at=datetime(2026, 11, 6, 12, tzinfo=UTC),
            ),
        ]
        placeholders = ", ".join("?" for _ in _HOLDINGS_COLUMNS)
        con.executemany(f"INSERT INTO holdings VALUES ({placeholders})", holdings)
        con.execute(
            "INSERT INTO qa_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                _SHA["exc_bad"],
                "delta_daily",
                "EXC",
                QUALITY_AS_OF,
                "weight_sum",
                "error",
                "sum=0.8500 expected=1±0.005",
                True,
            ],
        )
    finally:
        con.close()
    return path


METRICS_DB_AS_OF = date(2026, 10, 22)
"""The as-of date the hand-made metrics database is designed for."""

METRICS_DB_STALE_AS_OF = date(2026, 9, 1)
"""The one metrics date of the ``ONE`` fund, deliberately stale."""

METRICS_DB_FUNDS = ["BAD", "FLOW", "HOLD", "ONE"]
"""Fund tickers in the metrics database, in the order the dashboard lists them."""

_METRICS_DB_SHA = {
    "holdings_bad": "1" * 64,
    "holdings_flow": "2" * 64,
    "holdings_hold": "3" * 64,
    "holdings_one": "4" * 64,
    "metrics_flow": "5" * 64,
    "metrics_one": "6" * 64,
    "metrics_bad": "7" * 64,
}
_METRICS_KNOWN_AT = datetime(2026, 10, 22, 22, tzinfo=UTC)


def make_metrics_db(path: Path) -> Path:
    """A build-shaped database with a two-day metrics look-alike.

    ``FLOW`` has holdings and two metrics dates (so a flow is estimable),
    ``ONE`` has one stale metrics date, ``HOLD`` has holdings only and ``BAD``
    has an excluded metrics input with an error finding. A warning on
    ``FLOW``'s metrics input exercises the store-wide QA counts.
    """
    from etfh.build import DDL_SOURCES
    from etfh.schema import (
        DDL_FILE_VERSIONS,
        DDL_FUND_METRICS,
        DDL_HOLDINGS,
        DDL_HOLDINGS_CHANGES,
        DDL_QA_EVENTS,
    )

    con = duckdb.connect(str(path))
    try:
        for ddl in (
            DDL_FILE_VERSIONS,
            DDL_SOURCES,
            DDL_HOLDINGS,
            DDL_QA_EVENTS,
            DDL_HOLDINGS_CHANGES,
            DDL_FUND_METRICS,
        ):
            con.execute(ddl)
        con.executemany(
            "INSERT INTO sources VALUES (?, ?, ?, ?)",
            [
                ("flow_daily", "tests", "daily_holdings", "csv"),
                ("flow_metrics", "tests", "fund_metrics", "json"),
                ("bad_metrics", "tests", "fund_metrics", "json"),
            ],
        )
        con.executemany(
            "INSERT INTO file_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "flow_daily",
                    "BAD",
                    METRICS_DB_AS_OF,
                    1,
                    _METRICS_KNOWN_AT,
                    _METRICS_DB_SHA["holdings_bad"],
                    "raw/bad.csv",
                    "included",
                ),
                (
                    "flow_daily",
                    "FLOW",
                    METRICS_DB_AS_OF,
                    1,
                    _METRICS_KNOWN_AT,
                    _METRICS_DB_SHA["holdings_flow"],
                    "raw/flow.csv",
                    "included",
                ),
                (
                    "flow_daily",
                    "HOLD",
                    METRICS_DB_AS_OF,
                    1,
                    _METRICS_KNOWN_AT,
                    _METRICS_DB_SHA["holdings_hold"],
                    "raw/hold.csv",
                    "included",
                ),
                (
                    "flow_daily",
                    "ONE",
                    METRICS_DB_AS_OF,
                    1,
                    _METRICS_KNOWN_AT,
                    _METRICS_DB_SHA["holdings_one"],
                    "raw/one.csv",
                    "included",
                ),
                (
                    "flow_metrics",
                    "FLOW",
                    METRICS_DB_AS_OF,
                    1,
                    _METRICS_KNOWN_AT,
                    _METRICS_DB_SHA["metrics_flow"],
                    "raw/flow-metrics.json",
                    "included",
                ),
                (
                    "flow_metrics",
                    "ONE",
                    METRICS_DB_STALE_AS_OF,
                    1,
                    datetime(2026, 9, 1, 22, tzinfo=UTC),
                    _METRICS_DB_SHA["metrics_one"],
                    "raw/one-metrics.json",
                    "included",
                ),
                (
                    "bad_metrics",
                    "BAD",
                    METRICS_DB_AS_OF,
                    1,
                    _METRICS_KNOWN_AT,
                    _METRICS_DB_SHA["metrics_bad"],
                    "raw/bad-metrics.json",
                    "excluded",
                ),
            ],
        )
        holdings = [
            _quality_holding(
                "BAD",
                "flow_daily",
                _METRICS_DB_SHA["holdings_bad"],
                security_id="ISIN:CA600004AA54",
                name="Granite Systems Inc.",
                weight=1.0,
                as_of=METRICS_DB_AS_OF,
                known_at=_METRICS_KNOWN_AT,
            ),
            _quality_holding(
                "FLOW",
                "flow_daily",
                _METRICS_DB_SHA["holdings_flow"],
                security_id="ISIN:CA600005AA38",
                name="Harbour Lines Ltd.",
                weight=1.0,
                as_of=METRICS_DB_AS_OF,
                known_at=_METRICS_KNOWN_AT,
            ),
            _quality_holding(
                "HOLD",
                "flow_daily",
                _METRICS_DB_SHA["holdings_hold"],
                security_id="ISIN:CA600006AA11",
                name="Juniper Networks Ltd.",
                weight=1.0,
                as_of=METRICS_DB_AS_OF,
                known_at=_METRICS_KNOWN_AT,
            ),
            _quality_holding(
                "ONE",
                "flow_daily",
                _METRICS_DB_SHA["holdings_one"],
                security_id="ISIN:CA600007AA95",
                name="Kestrel Energy Corp.",
                weight=1.0,
                as_of=METRICS_DB_AS_OF,
                known_at=_METRICS_KNOWN_AT,
            ),
        ]
        placeholders = ", ".join("?" for _ in _HOLDINGS_COLUMNS)
        con.executemany(f"INSERT INTO holdings VALUES ({placeholders})", holdings)
        con.executemany(
            "INSERT INTO fund_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "FLOW",
                    date(2026, 10, 21),
                    _METRICS_KNOWN_AT,
                    1,
                    25.0,
                    1_000_000.0,
                    25_000_000.0,
                    "CAD",
                    "flow_metrics",
                    _METRICS_DB_SHA["metrics_flow"],
                ),
                (
                    "FLOW",
                    METRICS_DB_AS_OF,
                    _METRICS_KNOWN_AT,
                    1,
                    25.5,
                    1_050_000.0,
                    26_775_000.0,
                    "CAD",
                    "flow_metrics",
                    _METRICS_DB_SHA["metrics_flow"],
                ),
                (
                    "ONE",
                    METRICS_DB_STALE_AS_OF,
                    datetime(2026, 9, 1, 22, tzinfo=UTC),
                    1,
                    10.0,
                    500_000.0,
                    5_000_000.0,
                    "CAD",
                    "flow_metrics",
                    _METRICS_DB_SHA["metrics_one"],
                ),
            ],
        )
        con.executemany(
            "INSERT INTO qa_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    _METRICS_DB_SHA["metrics_flow"],
                    "flow_metrics",
                    "FLOW",
                    METRICS_DB_AS_OF,
                    "aum_consistency",
                    "warning",
                    "aum 26,775,000.00 vs nav x shares 26,775,000.00 (0.0%)",
                    False,
                ),
                (
                    _METRICS_DB_SHA["metrics_bad"],
                    "bad_metrics",
                    "BAD",
                    METRICS_DB_AS_OF,
                    "nav_positive",
                    "error",
                    "nav_per_unit=-1.0000 must be > 0",
                    True,
                ),
            ],
        )
    finally:
        con.close()
    return path


def build_synthetic_db(out_dir: Path) -> Path:
    """Build the committed synthetic raw store into ``out_dir``; return the DB."""
    from etfh.build import build

    build(REPO_ROOT / "tests" / "fixtures" / "raw_synthetic", out_dir)
    return out_dir / "etfh.duckdb"
