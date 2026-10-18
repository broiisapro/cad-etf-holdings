"""As-of-aware analytics over the tables written by :mod:`etfh.build`.

Every function takes an open DuckDB connection to a built database and an
``asof`` date, and uses only file versions whose ``known_at`` is strictly
before ``cutoff_utc(asof)`` (D30, D7, D53). There is no lookahead: the version
is selected from the rows already filtered by ``known_at``.

Version selection per fund (D42, D53):

1. only versions known before the cutoff compete;
2. the source kind recorded in the build's ``sources`` table decides priority:
   ``daily_holdings`` and synthetic daily files win over
   ``quarterly_disclosure`` (and synthetic PDFs), so a fund's daily holdings
   are used whenever any daily version is known;
3. among the winners, the latest ``as_of_date``, then the highest
   ``file_version``, then the lowest ``source_id``.

A fund whose selected version is a quarterly disclosure, or a daily file the
build flagged with a ``partial_list`` info event, is reported with
``partial=True``; the dashboard labels it "top holdings only". The flag never
comes from a weight-sum heuristic.

Non-security lines (``CASH:``, ``DERIV:``) and unmatched rows (null
``security_id``) never enter overlap, and only real security rows enter the
concentration metrics; unmatched rows still count because they are securities
with an unknown identifier.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

import duckdb
import polars as pl

from etfh.asof import cutoff_utc
from etfh.types import FileFormat, SourceKind

__all__ = [
    "Concentration",
    "FundNotFoundError",
    "OverlapResult",
    "breakdown",
    "changes",
    "concentration",
    "funds_available",
    "holdings_asof",
    "overlap",
    "overlap_matrix",
]

_NON_SECURITY_PREFIXES = ("CASH:", "DERIV:")


class FundNotFoundError(KeyError):
    """Raised when a fund has no rows at all in the built database."""

    def __init__(self, fund: str) -> None:
        self.fund = fund
        super().__init__(fund)


@dataclass(frozen=True)
class OverlapResult:
    """Pairwise overlap of two funds as of one date.

    ``overlap`` is ``sum(min(w_a, w_b))`` over common security ids; unmatched
    rows and non-security ids (``CASH:``, ``DERIV:``) are excluded. ``common``
    has one row per common security with columns ``security_id``,
    ``security_name``, ``weight_a``, ``weight_b``. ``partial`` is true when
    either fund's selected version is a quarterly top-holdings list.
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
    Metrics cover real security rows only (unmatched rows included, non-security
    lines excluded), and ``partial`` is true for a quarterly top-holdings list.
    """

    fund: str
    asof: date
    as_of_date: date | None
    n_holdings: int
    top10_weight: float
    hhi: float
    partial: bool


@dataclass(frozen=True)
class _Selection:
    as_of_date: date
    file_version: int
    source_id: str
    partial: bool


def holdings_asof(con: duckdb.DuckDBPyConnection, fund: str, asof: date) -> pl.DataFrame:
    """Rows of ``holdings`` for ``fund`` from the version known by ``asof``.

    Returns an empty frame (with the ``holdings`` columns) when the fund exists
    but no version is known by ``asof``.
    """
    selection = _select_version(con, fund, asof)
    if selection is None:
        return _empty_holdings(con)
    rows = _selected_rows(con, fund, selection)
    return rows.sort(["weight", "security_id", "security_name"], descending=[True, False, False])


def overlap(con: duckdb.DuckDBPyConnection, fund_a: str, fund_b: str, asof: date) -> OverlapResult:
    """Pairwise overlap between two funds as of ``asof``.

    A fund known by ``asof`` but with no data at all outweighs nothing: the
    overlap is 0.0 and ``common`` is empty.
    """
    selection_a, securities_a = _securities(con, fund_a, asof)
    selection_b, securities_b = _securities(con, fund_b, asof)
    common = (
        securities_a.join(securities_b, on="security_id", how="inner", suffix="_b")
        .select(
            "security_id",
            "security_name",
            pl.col("weight").alias("weight_a"),
            pl.col("weight_b").alias("weight_b"),
        )
        .sort("security_id")
    )
    value = 0.0
    if not common.is_empty():
        value = float(common.select(pl.min_horizontal("weight_a", "weight_b").sum()).item())
    partial = any(
        selection is not None and selection.partial for selection in (selection_a, selection_b)
    )
    return OverlapResult(
        fund_a=fund_a,
        fund_b=fund_b,
        asof=asof,
        overlap=value,
        common=common,
        partial=partial,
    )


def concentration(con: duckdb.DuckDBPyConnection, fund: str, asof: date) -> Concentration:
    """Top-10 weight, Herfindahl index and holding count for ``fund``."""
    selection, securities = _securities(con, fund, asof)
    if selection is None:
        return Concentration(
            fund=fund,
            asof=asof,
            as_of_date=None,
            n_holdings=0,
            top10_weight=0.0,
            hhi=0.0,
            partial=False,
        )
    weights = securities["weight"]
    top10 = float(weights.sort(descending=True).head(10).sum()) if securities.height else 0.0
    hhi = float((weights**2).sum()) if securities.height else 0.0
    return Concentration(
        fund=fund,
        asof=asof,
        as_of_date=selection.as_of_date,
        n_holdings=securities.height,
        top10_weight=top10,
        hhi=hhi,
        partial=selection.partial,
    )


def breakdown(
    con: duckdb.DuckDBPyConnection,
    fund: str,
    asof: date,
    by: Literal["sector", "country"],
) -> pl.DataFrame:
    """Weight per ``sector`` or ``country`` for ``fund`` as of ``asof``.

    Columns are ``key`` and ``weight``; every row of the selected version
    counts, including non-security lines. Sorted by weight descending.
    """
    selection = _select_version(con, fund, asof)
    if selection is None:
        return pl.DataFrame(schema={"key": pl.String, "weight": pl.Float64})
    rows = _selected_rows(con, fund, selection)
    return (
        rows.group_by(pl.col(by).alias("key"))
        .agg(pl.col("weight").sum())
        .sort(["weight", "key"], descending=[True, False])
    )


def overlap_matrix(
    con: duckdb.DuckDBPyConnection, funds: Sequence[str], asof: date
) -> pl.DataFrame:
    """All pairwise overlaps among ``funds`` as of ``asof``.

    Every ordered pair is present, including the diagonal; the input order of
    ``funds`` is preserved in ``fund_a`` and ``fund_b``.
    """
    securities = {fund: _securities(con, fund, asof)[1] for fund in funds}
    rows: list[dict[str, object]] = []
    for fund_a in funds:
        for fund_b in funds:
            common = securities[fund_a].join(
                securities[fund_b], on="security_id", how="inner", suffix="_b"
            )
            value = 0.0
            if not common.is_empty():
                value = float(common.select(pl.min_horizontal("weight", "weight_b").sum()).item())
            rows.append({"fund_a": fund_a, "fund_b": fund_b, "overlap": value})
    return pl.DataFrame(
        rows, schema={"fund_a": pl.String, "fund_b": pl.String, "overlap": pl.Float64}
    )


def changes(con: duckdb.DuckDBPyConnection, fund: str, start: date, end: date) -> pl.DataFrame:
    """Rows of ``holdings_changes`` observed within ``[start, end]``.

    A change is observed on its ``to_as_of`` date, so its ``from_as_of`` must be
    at or after ``start`` and its ``to_as_of`` at or before ``end``.
    """
    return con.execute(
        """
        SELECT fund_ticker, from_as_of, to_as_of, security_id, change_type,
               weight_before, weight_after, delta
        FROM holdings_changes
        WHERE fund_ticker = ? AND from_as_of >= ? AND to_as_of <= ?
        ORDER BY from_as_of, to_as_of, change_type, security_id
        """,
        [fund, start, end],
    ).pl()


def funds_available(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Fund tickers present in the database, sorted."""
    frame = con.execute("SELECT DISTINCT fund_ticker FROM holdings ORDER BY fund_ticker").pl()
    return [str(value) for value in frame["fund_ticker"].to_list()]


def _select_version(con: duckdb.DuckDBPyConnection, fund: str, asof: date) -> _Selection | None:
    exists = con.execute("SELECT 1 FROM holdings WHERE fund_ticker = ? LIMIT 1", [fund]).fetchone()
    if exists is None:
        raise FundNotFoundError(fund)
    cutoff = cutoff_utc(asof)
    versions = con.execute(
        """
        SELECT h.as_of_date, h.file_version, h.source_id, h.raw_file_sha256,
               s.kind AS kind, s.format AS format
        FROM holdings h
        LEFT JOIN sources s ON s.source_id = h.source_id
        WHERE h.fund_ticker = ? AND h.known_at < ?
        GROUP BY 1, 2, 3, 4, 5, 6
        """,
        [fund, cutoff],
    ).pl()
    if versions.is_empty():
        return None
    ranked = versions.with_columns(_source_priority_expr().alias("_priority")).sort(
        ["_priority", "as_of_date", "file_version", "source_id"],
        descending=[False, True, True, False],
    )
    row = ranked.row(0, named=True)
    partial = bool(row["_priority"] == 1) or row["raw_file_sha256"] in _partial_list_shas(con)
    return _Selection(
        as_of_date=row["as_of_date"],
        file_version=row["file_version"],
        source_id=row["source_id"],
        partial=partial,
    )


def _source_priority_expr() -> pl.Expr:
    """0 for daily holdings, 1 for quarterly disclosures and synthetic PDFs (D53)."""
    quarterly = pl.col("kind") == SourceKind.quarterly_disclosure.value
    synthetic_pdf = (pl.col("kind") == SourceKind.synthetic.value) & (
        pl.col("format") == FileFormat.pdf.value
    )
    return pl.when(quarterly | synthetic_pdf).then(1).otherwise(0)


def _partial_list_shas(con: duckdb.DuckDBPyConnection) -> frozenset[str]:
    """SHAs the build flagged with a ``partial_list`` info event (D52)."""
    frame = con.execute(
        'SELECT DISTINCT raw_file_sha256 FROM qa_events WHERE "check" = ?',
        ["partial_list"],
    ).pl()
    return frozenset(str(sha) for sha in frame["raw_file_sha256"].to_list())


def _selected_rows(
    con: duckdb.DuckDBPyConnection, fund: str, selection: _Selection
) -> pl.DataFrame:
    return con.execute(
        """
        SELECT * FROM holdings
        WHERE fund_ticker = ? AND as_of_date = ? AND file_version = ? AND source_id = ?
        """,
        [fund, selection.as_of_date, selection.file_version, selection.source_id],
    ).pl()


def _empty_holdings(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    return con.execute("SELECT * FROM holdings LIMIT 0").pl()


def _securities(
    con: duckdb.DuckDBPyConnection, fund: str, asof: date
) -> tuple[_Selection | None, pl.DataFrame]:
    selection = _select_version(con, fund, asof)
    if selection is None:
        return None, pl.DataFrame(
            schema={"security_id": pl.String, "weight": pl.Float64, "security_name": pl.String}
        )
    rows = _selected_rows(con, fund, selection)
    securities = (
        rows.filter(pl.col("security_id").is_not_null())
        .filter(
            ~pl.col("security_id").str.starts_with("CASH:")
            & ~pl.col("security_id").str.starts_with("DERIV:")
        )
        .group_by("security_id")
        .agg(pl.col("weight").sum(), pl.col("security_name").sort().first())
        .select("security_id", "weight", "security_name")
    )
    return selection, securities
