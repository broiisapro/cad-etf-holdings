"""Per-fund data quality for the dashboard.

Every number on the panel comes from the public functions of :mod:`etfh.qa`
and :mod:`etfh.analytics`:

- the selected portfolio date and the partial flag from
  :func:`etfh.analytics.concentration`;
- the unmatched non-cash weight share from :func:`etfh.qa.unmatched_shares`,
  selected as of the panel's date (the same definition ``etfh qa`` reports);
- the excluded files for a fund from :func:`etfh.qa.summarize`;
- the fund-metrics figures from :func:`etfh.analytics.metrics_asof`, the flow
  estimates from :func:`etfh.analytics.flows`, and the fund-metrics findings
  reported by :func:`etfh.qa.summarize` (D66, D67).

The only other read is the build's additive ``sources`` metadata table (D53),
through DuckDB's relation API rather than SQL, because no public accessor for
the source kind exists yet. It is display metadata, never an analytic.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import TypedDict, cast

import duckdb
import polars as pl
from polars.datatypes import DataType, DataTypeClass

from etfh import analytics, qa

UNMATCHED_NOTE_THRESHOLD = 0.20
"""Overlap understates when a fund's unmatched weight share exceeds this value."""

STALE_METRICS_DAYS = 30
"""Fund metrics older than this many days are called out in the panel."""

SOURCE_KIND_LABELS: dict[str, str] = {
    "daily_holdings": "Daily holdings",
    "quarterly_disclosure": "Quarterly disclosure",
    "synthetic": "Synthetic",
}

_METRICS_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "fund_ticker": pl.String,
    "as_of_date": pl.Date,
    "known_at": pl.Datetime(time_zone="UTC"),
    "file_version": pl.Int32,
    "nav_per_unit": pl.Float64,
    "shares_outstanding": pl.Float64,
    "aum": pl.Float64,
    "currency": pl.String,
    "source_id": pl.String,
    "raw_file_sha256": pl.String,
}

_FLOW_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "as_of_date": pl.Date,
    "shares_outstanding": pl.Float64,
    "nav_per_unit": pl.Float64,
    "prev_as_of_date": pl.Date,
    "prev_shares": pl.Float64,
    "gap_days": pl.Int32,
    "flow_estimate": pl.Float64,
    "method": pl.String,
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
class MetricsPoint:
    """The figures of the latest fund-metrics date, as the source published them."""

    as_of_date: date
    nav_per_unit: float | None
    shares_outstanding: float | None
    aum: float | None
    currency: str | None


@dataclass(frozen=True, slots=True)
class MetricsFinding:
    """One ``etfh.qa`` finding attached to a fund-metrics input."""

    source_id: str
    check: str
    severity: str
    detail: str


@dataclass(frozen=True, slots=True)
class MetricsQa:
    """Fund-metrics health from :func:`etfh.qa.summarize`.

    ``findings`` covers the fund's own excluded or inconsistent metrics inputs.
    ``errors``/``warnings``/``infos`` are the store-wide metrics counts the QA
    report prints, because the summary aggregates findings on included metrics
    inputs instead of exposing them per fund.
    """

    findings: tuple[MetricsFinding, ...] = ()
    errors: int = 0
    warnings: int = 0
    infos: int = 0
    inputs: int = 0

    @property
    def has_inputs(self) -> bool:
        """True when the build stored at least one fund-metrics input."""
        return self.inputs > 0

    @property
    def has_findings(self) -> bool:
        """True when the QA summary has any fund-metrics finding, anywhere."""
        return bool(self.findings) or bool(self.errors or self.warnings or self.infos)

    @property
    def counts_label(self) -> str:
        """The QA report's finding counts, for the panel caption."""
        return (
            f"{self.errors} error(s), {self.warnings} warning(s), "
            f"{self.infos} info(s) across all fund-metrics inputs"
        )


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
    metrics: MetricsQa = field(default_factory=MetricsQa)

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


@dataclass(frozen=True, slots=True)
class FundMetrics:
    """One fund's metrics history and flow estimates as known on ``asof``.

    ``history`` is :func:`etfh.analytics.metrics_asof` (one row per as-of date),
    ``flows`` is :func:`etfh.analytics.flows` over that history, and ``method``
    is the D67 label every flow carries.
    """

    fund: str
    asof: date
    history: pl.DataFrame
    flows: pl.DataFrame
    method: str

    @property
    def has_metrics(self) -> bool:
        """True when any metrics date is known by ``asof``."""
        return not self.history.is_empty()

    @property
    def n_dates(self) -> int:
        """Number of metrics dates known by ``asof``."""
        return self.history.height

    @property
    def latest(self) -> MetricsPoint | None:
        """The newest metrics point, or ``None`` when the fund has none yet."""
        if self.history.is_empty():
            return None
        row = self.history.row(self.history.height - 1, named=True)
        return MetricsPoint(
            as_of_date=cast(date, row["as_of_date"]),
            nav_per_unit=_float_or_none(row["nav_per_unit"]),
            shares_outstanding=_float_or_none(row["shares_outstanding"]),
            aum=_float_or_none(row["aum"]),
            currency=None if row["currency"] is None else str(row["currency"]),
        )

    @property
    def staleness_days(self) -> int | None:
        """Calendar days between the latest metrics date and the selected date."""
        latest = self.latest
        if latest is None:
            return None
        return max(0, (self.asof - latest.as_of_date).days)

    @property
    def is_stale(self) -> bool:
        """True when the latest metrics date is older than :data:`STALE_METRICS_DAYS`."""
        days = self.staleness_days
        return days is not None and days >= STALE_METRICS_DAYS

    @property
    def staleness_label(self) -> str:
        """Human sentence describing how old the latest metrics date is."""
        days = self.staleness_days
        if days is None:
            return "no metrics as of this date"
        if days == 0:
            return "same day as the selected date"
        if days == 1:
            return "1 day before the selected date"
        return f"{days} days before the selected date"

    @property
    def estimate_rows(self) -> pl.DataFrame:
        """Flow rows that actually carry an estimate."""
        if self.flows.is_empty():
            return self.flows
        return self.flows.filter(pl.col("flow_estimate").is_not_null())

    @property
    def can_estimate_flows(self) -> bool:
        """True when at least two share counts make one flow estimable."""
        return not self.estimate_rows.is_empty()


def _float_or_none(value: object) -> float | None:
    return None if value is None else float(cast(float, value))


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


def _excluded_by_fund(summary: qa.QaSummary) -> dict[str, tuple[ExcludedFile, ...]]:
    """Excluded files grouped by fund, from the QA summary."""
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


def _metrics_qa(summary: qa.QaSummary, fund: str) -> MetricsQa:
    """Metrics findings for ``fund`` plus the store-wide finding counts.

    ``qa.summarize`` exposes findings for excluded and inconsistent metrics
    inputs per fund; findings on included inputs survive only as counts, so the
    panel shows both the fund's own problem files and the report's totals.
    """
    entries: tuple[qa.MetricsFile | qa.InconsistentMetricsFile, ...] = (
        *summary.metrics.excluded_files,
        *summary.metrics.inconsistent_files,
    )
    findings = {
        MetricsFinding(
            source_id=entry.source_id,
            check=check,
            severity=severity,
            detail=detail,
        )
        for entry in entries
        if fund in entry.fund_tickers
        for check, severity, detail in entry.findings
    }
    return MetricsQa(
        findings=tuple(
            sorted(
                findings,
                key=lambda item: (
                    item.source_id,
                    item.check,
                    item.severity,
                    item.detail,
                ),
            )
        ),
        errors=summary.metrics.errors,
        warnings=summary.metrics.warnings,
        infos=summary.metrics.infos,
        inputs=summary.metrics.files_included + summary.metrics.files_excluded,
    )


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
    metrics: MetricsQa,
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
        metrics=metrics,
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
    summary = qa.summarize(con)
    return _quality(
        con,
        fund,
        asof,
        shares=_share_frame(con, asof),
        metadata=source_metadata(con),
        excluded=_excluded_by_fund(summary),
        metrics=_metrics_qa(summary, fund),
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
    summary = qa.summarize(con)
    excluded = _excluded_by_fund(summary)
    return {
        fund: _quality(
            con,
            fund,
            asof,
            shares=shares,
            metadata=metadata,
            excluded=excluded,
            metrics=_metrics_qa(summary, fund),
        )
        for fund in funds
    }


def fund_metrics(
    con: duckdb.DuckDBPyConnection,
    fund: str,
    asof: date,
) -> FundMetrics:
    """Latest metrics, metrics history and flow estimates for ``fund``.

    Metrics are additive (D66): a fund with holdings but no metrics rows gets
    an empty history instead of raising, so the dashboard can say "no metrics
    yet" rather than treating the fund as unknown.
    """
    history = _metrics_history(con, fund, asof)
    return FundMetrics(
        fund=fund,
        asof=asof,
        history=history,
        flows=_flow_history(con, fund, history, asof),
        method=analytics.FLOW_METHOD,
    )


def _metrics_history(con: duckdb.DuckDBPyConnection, fund: str, asof: date) -> pl.DataFrame:
    """``analytics.metrics_asof``, or an empty metrics frame when there are none."""
    try:
        return analytics.metrics_asof(con, fund, asof)
    except (analytics.FundNotFoundError, duckdb.Error):
        return pl.DataFrame(schema=_METRICS_SCHEMA)


def _flow_history(
    con: duckdb.DuckDBPyConnection,
    fund: str,
    history: pl.DataFrame,
    asof: date,
) -> pl.DataFrame:
    """``analytics.flows`` over the known metrics dates, empty below two dates."""
    if history.height < 2:
        return pl.DataFrame(schema=_FLOW_SCHEMA)
    start = cast(date, history["as_of_date"].min())
    end = cast(date, history["as_of_date"].max())
    try:
        return analytics.flows(con, fund, start, end, asof=asof)
    except (analytics.FundNotFoundError, duckdb.Error):
        return pl.DataFrame(schema=_FLOW_SCHEMA)


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
