"""Helpers for dashboard tests: a fake analytics module and a DuckDB file."""

from __future__ import annotations

import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import duckdb
import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = REPO_ROOT / "etfh" / "dashboard" / "app.py"

AS_OF = date(2026, 11, 2)
FUNDS = ["SYNA", "SYNB", "SYNC"]
PARTIAL_FUNDS = frozenset({"SYNC"})

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
    return module


@contextmanager
def install_fake_analytics() -> Iterator[ModuleType]:
    """Install a fake ``etfh.analytics`` module for the duration of the block."""
    import etfh

    saved_module = sys.modules.get("etfh.analytics")
    had_attr = hasattr(etfh, "analytics")
    saved_attr = getattr(etfh, "analytics", None)
    fake = build_fake_analytics()
    sys.modules["etfh.analytics"] = fake
    if had_attr:
        delattr(etfh, "analytics")
    try:
        yield fake
    finally:
        if saved_module is None:
            sys.modules.pop("etfh.analytics", None)
        else:
            sys.modules["etfh.analytics"] = saved_module
        if getattr(etfh, "analytics", None) is fake:
            if saved_attr is not None:
                etfh.analytics = saved_attr
            else:
                delattr(etfh, "analytics")


def make_empty_db(path: Path) -> Path:
    """Create a valid, empty DuckDB file the app can open read-only."""
    duckdb.connect(str(path)).close()
    return path
