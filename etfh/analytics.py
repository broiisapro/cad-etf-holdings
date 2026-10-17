"""As-of-aware analytics over the tables written by :mod:`etfh.build`.

Every function takes an open DuckDB connection to a built database and an
``asof`` date, and uses only file versions whose ``known_at`` is at or before
``cutoff_utc(asof)`` (D30). For a fund with both daily and quarterly data, the
daily source wins; quarterly disclosures (top holdings only) are used only for
funds without daily data and are reported with ``partial=True`` (D42).

The implementations land in the analytics issue; this module currently fixes
the shared interface (dataclasses and signatures) that the dashboard and CLI
build against.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

import duckdb
import polars as pl

__all__ = [
    "Concentration",
    "OverlapResult",
    "breakdown",
    "changes",
    "concentration",
    "funds_available",
    "holdings_asof",
    "overlap",
    "overlap_matrix",
]


@dataclass(frozen=True)
class OverlapResult:
    """Pairwise overlap of two funds as of one date.

    ``overlap`` is ``sum(min(w_a, w_b))`` over common security ids; unmatched
    rows and non-security ids (``CASH:``, ``DERIV:``) are excluded. ``common``
    has one row per common security with columns ``security_id``,
    ``security_name``, ``weight_a``, ``weight_b``. ``partial`` is true when
    either fund's data as of the date is a quarterly top-holdings disclosure.
    """

    fund_a: str
    fund_b: str
    asof: date
    overlap: float
    common: pl.DataFrame
    partial: bool


@dataclass(frozen=True)
class Concentration:
    """Concentration metrics for one fund as of one date.

    ``as_of_date`` is the portfolio date of the version used, or ``None`` when
    nothing is known by ``asof``; the metric fields are zero in that case.
    ``partial`` is true for quarterly-only data.
    """

    fund: str
    asof: date
    as_of_date: date | None
    n_holdings: int
    top10_weight: float
    hhi: float
    partial: bool


def holdings_asof(con: duckdb.DuckDBPyConnection, fund: str, asof: date) -> pl.DataFrame:
    """Rows of ``holdings`` for ``fund`` from the version known by ``asof``."""
    raise NotImplementedError


def overlap(con: duckdb.DuckDBPyConnection, fund_a: str, fund_b: str, asof: date) -> OverlapResult:
    """Pairwise overlap between two funds as of ``asof``."""
    raise NotImplementedError


def concentration(con: duckdb.DuckDBPyConnection, fund: str, asof: date) -> Concentration:
    """Top-10 weight, Herfindahl index and holding count for ``fund``."""
    raise NotImplementedError


def breakdown(
    con: duckdb.DuckDBPyConnection,
    fund: str,
    asof: date,
    by: Literal["sector", "country"],
) -> pl.DataFrame:
    """Weight per ``sector`` or ``country`` for ``fund`` as of ``asof``."""
    raise NotImplementedError


def overlap_matrix(
    con: duckdb.DuckDBPyConnection, funds: Sequence[str], asof: date
) -> pl.DataFrame:
    """All pairwise overlaps among ``funds`` as of ``asof``."""
    raise NotImplementedError


def changes(con: duckdb.DuckDBPyConnection, fund: str, start: date, end: date) -> pl.DataFrame:
    """Rows of ``holdings_changes`` for ``fund`` with ``from_as_of`` in range."""
    raise NotImplementedError


def funds_available(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Fund tickers present in the database, sorted."""
    raise NotImplementedError
