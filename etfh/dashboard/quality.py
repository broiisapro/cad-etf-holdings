"""Per-fund data quality for the dashboard.

Every number on the panel comes from the public functions of :mod:`etfh.qa`
and :mod:`etfh.analytics`:

- the selected portfolio date and the partial flag from
  :func:`etfh.analytics.concentration`;
- the unmatched non-cash weight share from :func:`etfh.qa.unmatched_shares`,
  selected as of the panel's date (the same definition ``etfh qa`` reports);
- the excluded files for a fund from :func:`etfh.qa.summarize`.

The only other read is the build's additive ``sources`` metadata table (D53),
through DuckDB's relation API rather than SQL, because no public accessor for
the source kind exists yet. It is display metadata, never an analytic.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import TypedDict, cast

import duckdb
import polars as pl

from etfh import analytics, qa

UNMATCHED_NOTE_THRESHOLD = 0.20
"""Overlap understates when a fund's unmatched weight share exceeds this value."""

SOURCE_KIND_LABELS: dict[str, str] = {
    "daily_holdings": "Daily holdings",
    "quarterly_disclosure": "Quarterly disclosure",
    "synthetic": "Synthetic",
}


class _ShareRow(TypedDict):
    fund_ticker: str
    as_of_date: date
    source_id: str
    unmatched_weight: float
    total_weight: float
    share: float


@dataclass(frozen=True, slots=True)
class ExcludedFile:
    """One file the build excluded for the fund, with its error checks."""

    as_of_date: date | None
    source_id: str
    checks: tuple[str, ...]
    findings: tuple[tuple[str, str, str], ...]

    @property
    def as_of_label(self) -> str:
        """The portfolio date as an ISO string, or ``-`` when the file has none."""
        return self.as_of_date.isoformat() if self.as_of_date is not None else "-"

    @property
    def reason(self) -> str:
        """The error checks joined for display, or ``error`` when unlabelled."""
        return ", ".join(self.checks) if self.checks else "error"


@dataclass(frozen=True, slots=True)
class FundQuality:
    """Data-quality state of one fund as of one dashboard date."""

    fund: str
    asof: date
    as_of_date: date | None
    source_id: str | None
    source_kind: str | None
    source_format: str | None
    partial: bool
    unmatched_share: float | None
    unmatched_weight: float | None
    total_weight: float | None
    excluded_files: tuple[ExcludedFile, ...] = ()

    @property
    def source_label(self) -> str:
        """Human-readable source kind, with the file format when known."""
        if self.source_kind is None:
            return "Unknown"
        label = SOURCE_KIND_LABELS.get(self.source_kind, self.source_kind.replace("_", " ").title())
        if self.source_format:
            return f"{label} ({self.source_format.upper()})"
        return label

    @property
    def coverage_label(self) -> str:
        """Whether the selected file covers the whole portfolio or a subset."""
        return "Top holdings only" if self.partial else "Full portfolio"

    @property
    def understates_overlap(self) -> bool:
        """True when unmatched weight is high enough to distort overlap views."""
        return self.unmatched_share is not None and self.unmatched_share > UNMATCHED_NOTE_THRESHOLD


def source_metadata(con: duckdb.DuckDBPyConnection) -> dict[str, tuple[str | None, str | None]]:
    """``source_id -> (kind, format)`` from the build's ``sources`` table (D53).

    Returns an empty mapping when the table is absent (an older or empty
    database), so the panel degrades to ``Unknown`` instead of failing.
    """
    try:
        frame = con.table("sources").select("source_id", "kind", "format").pl()
    except duckdb.Error:
        return {}
    metadata: dict[str, tuple[str | None, str | None]] = {}
    for row in frame.iter_rows(named=True):
        kind = row["kind"]
        source_format = row["format"]
        metadata[str(row["source_id"])] = (
            str(kind) if kind is not None else None,
            str(source_format) if source_format is not None else None,
        )
    return metadata


def _share_frame(con: duckdb.DuckDBPyConnection, asof: date) -> pl.DataFrame:
    """``etfh.qa.unmatched_shares`` as of ``asof``, or an empty frame on error."""
    try:
        return qa.unmatched_shares(con, asof)
    except duckdb.Error:
        return pl.DataFrame(
            schema={
                "fund_ticker": pl.String,
                "as_of_date": pl.Date,
                "source_id": pl.String,
                "unmatched_weight": pl.Float64,
                "total_weight": pl.Float64,
                "share": pl.Float64,
            }
        )


def _excluded_by_fund(con: duckdb.DuckDBPyConnection) -> dict[str, tuple[ExcludedFile, ...]]:
    """Excluded files grouped by fund, from the QA summary."""
    summary = qa.summarize(con)
    grouped: dict[str, list[ExcludedFile]] = {}
    for entry in summary.excluded_files:
        checks = tuple(
            sorted({check for check, severity, _detail in entry.findings if severity == "error"})
        )
        grouped.setdefault(entry.fund_ticker, []).append(
            ExcludedFile(
                as_of_date=entry.as_of_date,
                source_id=entry.source_id,
                checks=checks,
                findings=entry.findings,
            )
        )
    result: dict[str, tuple[ExcludedFile, ...]] = {}
    for fund, files in grouped.items():
        result[fund] = tuple(
            sorted(files, key=lambda item: (item.as_of_date or date.min, item.source_id))
        )
    return result


def _selected_source_id(holdings: pl.DataFrame) -> str | None:
    """The source of the selected version, read from its holdings rows."""
    if holdings.is_empty() or "source_id" not in holdings.columns:
        return None
    sources = holdings["source_id"].unique().to_list()
    if len(sources) != 1 or sources[0] is None:
        return None
    return str(sources[0])


def _unmatched_for(
    shares: pl.DataFrame,
    fund: str,
    as_of_date: date | None,
    source_id: str | None,
) -> _ShareRow | None:
    """The unmatched-share row of the selected fund version, if present."""
    if shares.is_empty() or as_of_date is None or source_id is None:
        return None
    match = shares.filter(
        (pl.col("fund_ticker") == fund)
        & (pl.col("as_of_date") == as_of_date)
        & (pl.col("source_id") == source_id)
    )
    if match.is_empty():
        return None
    return cast(_ShareRow, match.row(0, named=True))


def _quality(
    con: duckdb.DuckDBPyConnection,
    fund: str,
    asof: date,
    *,
    shares: pl.DataFrame,
    metadata: dict[str, tuple[str | None, str | None]],
    excluded: dict[str, tuple[ExcludedFile, ...]],
    holdings: pl.DataFrame | None = None,
) -> FundQuality:
    concentration = analytics.concentration(con, fund, asof)
    rows = analytics.holdings_asof(con, fund, asof) if holdings is None else holdings
    source_id = _selected_source_id(rows)
    kind, source_format = metadata.get(source_id, (None, None)) if source_id else (None, None)
    share_row = _unmatched_for(shares, fund, concentration.as_of_date, source_id)
    return FundQuality(
        fund=fund,
        asof=asof,
        as_of_date=concentration.as_of_date,
        source_id=source_id,
        source_kind=kind,
        source_format=source_format,
        partial=concentration.partial,
        unmatched_share=float(share_row["share"]) if share_row is not None else None,
        unmatched_weight=(float(share_row["unmatched_weight"]) if share_row is not None else None),
        total_weight=float(share_row["total_weight"]) if share_row is not None else None,
        excluded_files=excluded.get(fund, ()),
    )


def fund_quality(
    con: duckdb.DuckDBPyConnection,
    fund: str,
    asof: date,
    *,
    holdings: pl.DataFrame | None = None,
) -> FundQuality:
    """Data quality for ``fund`` as of ``asof``.

    ``holdings`` may carry the frame the caller already fetched for the fund,
    saving one query; it must be the as-of-selected frame
    (:func:`etfh.analytics.holdings_asof`).
    """
    return _quality(
        con,
        fund,
        asof,
        shares=_share_frame(con, asof),
        metadata=source_metadata(con),
        excluded=_excluded_by_fund(con),
        holdings=holdings,
    )


def fund_qualities(
    con: duckdb.DuckDBPyConnection,
    funds: Sequence[str],
    asof: date,
) -> dict[str, FundQuality]:
    """Quality for several funds, reading the shared QA tables once."""
    shares = _share_frame(con, asof)
    metadata = source_metadata(con)
    excluded = _excluded_by_fund(con)
    return {
        fund: _quality(
            con,
            fund,
            asof,
            shares=shares,
            metadata=metadata,
            excluded=excluded,
        )
        for fund in funds
    }


def understated_note(qualities: Sequence[FundQuality]) -> str | None:
    """A warning sentence naming funds whose unmatched weight distorts overlap.

    Returns ``None`` when every fund is fully identified (or unknown, which the
    panel reports separately).
    """
    understated = [item for item in qualities if item.understates_overlap]
    if not understated:
        return None
    parts = [
        f"{item.fund} has {item.unmatched_share:.2%} of its weight unmatched"
        for item in understated
        if item.unmatched_share is not None
    ]
    return (
        "Overlap is understated: "
        + "; ".join(parts)
        + ". Unmatched holdings cannot match any other fund, so only identified weight overlaps."
    )
