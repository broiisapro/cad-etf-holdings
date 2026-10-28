"""QA summary, reports and exit code for a built database.

Reads ``file_versions``, ``qa_events`` and (when present) ``holdings`` and
``fund_metrics`` from a DuckDB built by ``etfh build``; the additive ``sources``
table (D53) supplies the issuer, source kind and format when it exists.

The report keeps daily holdings and quarterly portfolio disclosures in
separate quality sections: each gets its own per-issuer median unmatched weight
share and its own worst-funds list (only funds with a share above zero, so a
clean portfolio does not pad the list), and the shares are derived from the
source kind, never from weight-sum heuristics.

Non-security rows never count toward the unmatched share: cash, FX and
derivative rows (D33), the documented balancing lines such as "Other Assets
less Liabilities" (D80c) and physical commodity holdings flagged with the
parser's ``commodity_quantity`` note (D82). The last two are also counted on
their own report lines, so their weight is visible without inflating the
unmatched share.

Fund metrics (D66, interfaces wave 06 §4) get their own section: how many
metrics inputs were included or excluded, how many funds have metrics, the
latest metrics date per issuer, the finding counts, and every excluded or
inconsistent metrics input with its findings. Findings belong to metrics when
their source is a ``fund_metrics`` source or when the build recorded them as a
``metrics_parse_error`` against the raw file's own source (there is no metrics
version to attach in that case). Metrics findings never change a holdings
file's status, and their errors are counted in the exit code the same way: an
errored metrics input must be excluded (or listed as inconsistent) for QA to
pass.

``summarize(..., asof=D)`` makes every section point-in-time: file counts,
excluded and inconsistent files, findings, unmatched rows, unmatched shares,
worst funds and the fund-metrics section consider only versions whose
``known_at`` is before ``cutoff_utc(D)`` (D7/D30). Without ``asof`` the report
describes the whole store exactly as before.

:func:`render` produces the human-readable text report, :func:`render_markdown`
a publish-safe summary for a workflow job summary (counts, shares, fund/issuer
tickers and check names only: no security names, ids, weights or file paths,
D77/D79) and :func:`render_json` the same numbers for machines.

Rules the report and exit code encode:

- a *file* is one stored raw input per source (SHA-256 + source id), so a
  metrics history that yields many dated versions still counts once; the
  top-line totals cover holdings inputs, and fund metrics are counted in their
  own section;
- every errored holdings file is listed; a file that is errored but not
  excluded (or has no ``file_versions`` row at all) is additionally reported
  under ``inconsistent:`` and keeps exit code 1;
- every errored metrics input is listed under ``fund metrics``; one that is
  included, has no metrics version, or disagrees with its versions is reported
  as inconsistent there and keeps exit code 1;
- exit code 1 means the tables are missing or the database is inconsistent;
- exit code 3 means a ``--max-unmatched-share`` threshold was exceeded by an
  included daily file (quarterly disclosures are partial by nature and never
  trip it);
- otherwise 0: every errored file is excluded and reported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from statistics import median
from typing import Any

import duckdb
import polars as pl
from polars.datatypes import DataType, DataTypeClass

from etfh import analytics
from etfh.asof import cutoff_utc
from etfh.types import SourceKind
from etfh.validate import BALANCING_LINE_NAMES

__all__ = [
    "ExcludedFile",
    "InconsistentFile",
    "InconsistentMetricsFile",
    "LineUsage",
    "MetricsFile",
    "MetricsSummary",
    "QaSummary",
    "UnmatchedQuality",
    "UnmatchedShare",
    "exit_code",
    "render",
    "render_json",
    "render_markdown",
    "summarize",
    "unmatched_shares",
]

_DAILY_KIND = "daily_holdings"
_QUARTERLY_KIND = "quarterly_disclosure"
_METRICS_KIND = SourceKind.fund_metrics.value
_PDF_FORMAT = "pdf"
_QUALITY_DAILY = "daily"
_QUALITY_QUARTERLY = "quarterly"
# Fixed order: the report must not depend on dict or database row order.
_QUALITY_KINDS = (_QUALITY_DAILY, _QUALITY_QUARTERLY)
_MAX_WORST_FUNDS = 10
_NON_SECURITY_CLASSES = ("cash", "fx", "derivative")
_COMMODITY_CHECK = "commodity_quantity"
_BALANCING_NAMES = list(BALANCING_LINE_NAMES)
#: Checks only the fund-metrics validator emits (interfaces wave 06 §4). They
#: identify a metrics source even when its recorded kind predates ``fund_metrics``
#: (a test config, or a database built before the kind existed).
_METRICS_ROW_CHECKS = frozenset(
    {"nav_positive", "shares_nonnegative", "aum_consistency", "duplicate_date"}
)
#: The build records a metrics parser failure against the raw file's own
#: source, so the check name is the only way to tell it apart (D66).
_METRICS_PARSE_CHECK = "metrics_parse_error"
_METRICS_CHECKS = _METRICS_ROW_CHECKS | {_METRICS_PARSE_CHECK}
_SHARE_COLUMNS = (
    "fund_ticker",
    "as_of_date",
    "source_id",
    "unmatched_weight",
    "total_weight",
    "share",
)
_SHARE_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "fund_ticker": pl.String,
    "as_of_date": pl.Date,
    "source_id": pl.String,
    "unmatched_weight": pl.Float64,
    "total_weight": pl.Float64,
    "share": pl.Float64,
}


@dataclass(frozen=True, slots=True)
class ExcludedFile:
    """One excluded file and all of its recorded findings."""

    fund_ticker: str
    as_of_date: date | None
    source_id: str
    findings: tuple[tuple[str, str, str], ...]  # (check, severity, detail)


@dataclass(frozen=True, slots=True)
class InconsistentFile:
    """An errored raw file that is not excluded and reported."""

    fund_ticker: str
    as_of_date: date | None
    source_id: str
    sha256: str
    reason: str
    findings: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True, slots=True)
class UnmatchedShare:
    """One fund/as-of/source pair's share of unmatched non-cash weight."""

    fund_ticker: str
    as_of_date: date
    source_id: str
    unmatched_weight: float
    total_weight: float
    share: float


@dataclass(frozen=True, slots=True)
class UnmatchedQuality:
    """Unmatched-weight quality for one source kind (``daily``/``quarterly``)."""

    kind: str
    issuer_medians: tuple[tuple[str, float], ...] = ()
    worst_funds: tuple[UnmatchedShare, ...] = ()


@dataclass(frozen=True, slots=True)
class LineUsage:
    """How many non-security rows a category has and their signed weight sum."""

    rows: int = 0
    weight_sum: float = 0.0


@dataclass(frozen=True, slots=True)
class MetricsFile:
    """One excluded fund-metrics input (raw file + metrics source)."""

    source_id: str
    fund_tickers: tuple[str, ...]
    as_of_date: date | None
    versions: int
    findings: tuple[tuple[str, str, str], ...]  # (check, severity, detail)


@dataclass(frozen=True, slots=True)
class InconsistentMetricsFile:
    """An errored fund-metrics input that is not accounted for by exclusion."""

    source_id: str
    fund_tickers: tuple[str, ...]
    as_of_date: date | None
    versions: int
    reason: str
    findings: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True, slots=True)
class MetricsSummary:
    """Fund-metrics health read from ``file_versions``, ``qa_events`` and ``fund_metrics``."""

    files_included: int = 0
    files_excluded: int = 0
    excluded_files: tuple[MetricsFile, ...] = ()
    inconsistent_files: tuple[InconsistentMetricsFile, ...] = ()
    funds: tuple[str, ...] = ()
    latest_by_issuer: tuple[tuple[str, date], ...] = ()
    errors: int = 0
    warnings: int = 0
    infos: int = 0


@dataclass
class _Input:
    """Working state for one stored input (raw file + source) while summarizing."""

    source_id: str
    fund_tickers: set[str] = field(default_factory=set)
    dates: list[date] = field(default_factory=list)
    versions: int = 0
    statuses: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class QaSummary:
    """Totals, files and unmatched-weight measurements read from the database."""

    files_included: int = 0
    files_excluded: int = 0
    excluded_files: tuple[ExcludedFile, ...] = ()
    inconsistent_files: tuple[InconsistentFile, ...] = ()
    warnings: int = 0
    unmatched_rows: int = 0
    unmatched_examples: tuple[str, ...] = ()
    unmatched_shares: tuple[UnmatchedShare, ...] = ()
    unmatched_quality: tuple[UnmatchedQuality, ...] = ()
    max_unmatched_share: float | None = None
    unmatched_over_threshold: tuple[UnmatchedShare, ...] = ()
    metrics: MetricsSummary = field(default_factory=MetricsSummary)
    asof: date | None = None
    balancing_lines: LineUsage = field(default_factory=LineUsage)
    physical_commodity_lines: LineUsage = field(default_factory=LineUsage)
    tables_missing: bool = False


def _missing_tables() -> QaSummary:
    return QaSummary(tables_missing=True)


def _balancing_predicate(column: str) -> str:
    """SQL predicate that is true for rows that are not balancing lines (D80c)."""
    names = ", ".join(f"'{name}'" for name in BALANCING_LINE_NAMES)
    return rf"lower(trim(regexp_replace({column}, '\s+', ' ', 'g'))) NOT IN ({names})"


def _commodity_predicate(class_column: str, sha_column: str) -> str:
    """SQL predicate that is true for rows that are not physical commodity lines (D82)."""
    return (
        f"NOT (lower({class_column}) = 'commodity' AND {sha_column} IN "
        f"(SELECT raw_file_sha256 FROM qa_events WHERE \"check\" = '{_COMMODITY_CHECK}'))"
    )


def _commodity_shas(con: duckdb.DuckDBPyConnection) -> frozenset[str]:
    """SHAs whose parser recorded a ``commodity_quantity`` note (D82)."""
    try:
        frame = con.execute(
            'SELECT DISTINCT raw_file_sha256 FROM qa_events WHERE "check" = ?',
            [_COMMODITY_CHECK],
        ).pl()
    except duckdb.Error:
        return frozenset()
    return frozenset(str(sha) for sha in frame["raw_file_sha256"].to_list())


def _commodity_mask(frame: pl.DataFrame, commodity_shas: frozenset[str]) -> pl.Series:
    """Rows of a physical commodity file (D82): note plus the commodity asset class."""
    if not commodity_shas:
        return pl.Series("_commodity", [False] * frame.height, dtype=pl.Boolean)
    return (frame["asset_class"].str.to_lowercase() == "commodity") & frame[
        "raw_file_sha256"
    ].is_in(sorted(commodity_shas))


def _balancing_mask(frame: pl.DataFrame) -> pl.Series:
    """Rows whose name is a documented balancing line (D80c)."""
    normalized = (
        frame["security_name"].fill_null("").str.to_lowercase().str.strip_chars()
    ).str.replace_all(r"\s+", " ")
    return normalized.is_in(_BALANCING_NAMES)


def _security_rows(frame: pl.DataFrame, commodity_shas: frozenset[str]) -> pl.DataFrame:
    """The rows that count as securities for the unmatched share.

    Cash/FX/derivative rows (D33), balancing lines (D80c) and physical
    commodity lines (D82) are non-securities and are removed.
    """
    if frame.is_empty():
        return frame
    cash_like = frame["asset_class"].str.to_lowercase().is_in(_NON_SECURITY_CLASSES)
    commodity = _commodity_mask(frame, commodity_shas)
    balancing = (
        _balancing_mask(frame)
        if "security_name" in frame.columns
        else pl.Series("_balancing", [False] * frame.height, dtype=pl.Boolean)
    )
    return frame.filter(~cash_like & ~commodity & ~balancing)


def _line_usage(frame: pl.DataFrame, commodity_shas: frozenset[str]) -> tuple[LineUsage, LineUsage]:
    """Counts and signed weight sums for balancing and physical commodity lines.

    Returns ``(balancing, physical_commodity)``. A row is counted once: a
    commodity row is matched by its file-level note and asset class, a
    balancing row by its documented name. Both are non-security rows.
    """
    if frame.is_empty():
        return LineUsage(), LineUsage()
    commodity = _commodity_mask(frame, commodity_shas)
    balancing = ~commodity & _balancing_mask(frame)
    commodity_rows = frame.filter(commodity)
    balancing_rows = frame.filter(balancing)
    return (
        LineUsage(
            rows=balancing_rows.height,
            weight_sum=float(balancing_rows["weight"].sum()),
        ),
        LineUsage(
            rows=commodity_rows.height,
            weight_sum=float(commodity_rows["weight"].sum()),
        ),
    )


_LATEST_INCLUDED_CTE = """
    SELECT source_id, fund_ticker, as_of_date, max(file_version) AS file_version
    FROM file_versions
    WHERE status = 'included'
    GROUP BY source_id, fund_ticker, as_of_date
"""

_MEASURED_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "fund_ticker": pl.String,
    "as_of_date": pl.Date,
    "source_id": pl.String,
    "security_id": pl.String,
    "security_name": pl.String,
    "asset_class": pl.String,
    "weight": pl.Float64,
    "raw_file_sha256": pl.String,
}


def _measured_holdings(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Holdings rows of the latest included version per fund/as-of/source."""
    return (
        con.execute(
            f"""
        WITH latest AS ({_LATEST_INCLUDED_CTE})
        SELECT h.fund_ticker, h.as_of_date, h.source_id, h.security_id,
               h.security_name, h.asset_class, h.weight, h.raw_file_sha256
        FROM holdings h
        JOIN latest v
          ON h.source_id = v.source_id
         AND h.fund_ticker = v.fund_ticker
         AND h.as_of_date = v.as_of_date
         AND h.file_version = v.file_version
        """
        )
        .pl()
        .select(list(_MEASURED_SCHEMA))
    )


def _asof_holdings(con: duckdb.DuckDBPyConnection, asof: date) -> pl.DataFrame:
    """The holdings rows a point-in-time query at ``asof`` would select, per fund."""
    frames: list[pl.DataFrame] = []
    for fund in analytics.funds_available(con):
        selected = analytics.holdings_asof(con, fund, asof)
        if not selected.is_empty():
            frames.append(selected)
    if not frames:
        return pl.DataFrame(schema=_MEASURED_SCHEMA)
    return pl.concat(frames, how="vertical").select(list(_MEASURED_SCHEMA))


def unmatched_shares(con: duckdb.DuckDBPyConnection, asof: date | None = None) -> pl.DataFrame:
    """Unmatched non-cash weight share per included file version.

    Without ``asof``, every ``(fund_ticker, as_of_date, source_id)`` uses its
    latest included ``file_version`` (so a republished file is measured once,
    not accumulated). With ``asof``, the frame instead holds one row per fund
    for exactly the version :func:`etfh.analytics.holdings_asof` selects: only
    versions whose ``known_at`` is before ``cutoff_utc(asof)`` compete, daily
    holdings win over quarterly disclosures (D42, D53), then the latest
    ``as_of_date`` and the highest ``file_version``. Reading the selected rows
    from analytics keeps the two views in lockstep, so a file republished after
    ``asof`` cannot change the share reported for that date. Like analytics, the
    ``asof`` path needs the build's additive ``sources`` table (D53), which
    every build writes.

    In both cases ``unmatched`` is the sum of ``abs(weight)`` over rows with a
    null ``security_id`` that are securities, and ``total`` is the same sum over
    every security row. Cash/FX/derivative rows (D33), balancing lines (D80c)
    and physical commodity lines (D82) are non-securities and count in neither.
    The share is 0 when the total is 0. Returns the columns ``fund_ticker,
    as_of_date, source_id, unmatched_weight, total_weight, share``.
    """
    if asof is not None:
        return _unmatched_shares_asof(con, asof)
    rows = con.execute(
        f"""
        WITH latest AS ({_LATEST_INCLUDED_CTE})
        SELECT h.fund_ticker,
               h.as_of_date,
               h.source_id,
               COALESCE(SUM(ABS(h.weight)) FILTER (
                   WHERE h.security_id IS NULL
                     AND lower(h.asset_class) NOT IN ('cash', 'fx', 'derivative')
                     AND {_balancing_predicate("h.security_name")}
                     AND {_commodity_predicate("h.asset_class", "h.raw_file_sha256")}
               ), 0.0) AS unmatched_weight,
               COALESCE(SUM(ABS(h.weight)) FILTER (
                   WHERE lower(h.asset_class) NOT IN ('cash', 'fx', 'derivative')
                     AND {_balancing_predicate("h.security_name")}
                     AND {_commodity_predicate("h.asset_class", "h.raw_file_sha256")}
               ), 0.0) AS total_weight
        FROM holdings h
        JOIN latest v
          ON h.source_id = v.source_id
         AND h.fund_ticker = v.fund_ticker
         AND h.as_of_date = v.as_of_date
         AND h.file_version = v.file_version
        GROUP BY h.fund_ticker, h.as_of_date, h.source_id
        ORDER BY h.fund_ticker, h.as_of_date, h.source_id
        """
    ).pl()
    return rows.with_columns(
        pl.when(pl.col("total_weight") > 0)
        .then(pl.col("unmatched_weight") / pl.col("total_weight"))
        .otherwise(0.0)
        .alias("share")
    ).select(_SHARE_COLUMNS)


def _unmatched_shares_asof(con: duckdb.DuckDBPyConnection, asof: date) -> pl.DataFrame:
    """One row per fund for the version analytics selects as of ``asof``."""
    rows: list[dict[str, object]] = []
    commodity_shas = _commodity_shas(con)
    for fund in analytics.funds_available(con):
        selected = analytics.holdings_asof(con, fund, asof)
        if selected.is_empty():
            continue
        rows.append(_asof_share_row(selected, commodity_shas))
    return pl.DataFrame(rows, schema=_SHARE_SCHEMA)


def _asof_share_row(selected: pl.DataFrame, commodity_shas: frozenset[str]) -> dict[str, object]:
    """The unmatched share of one analytics-selected holdings frame."""
    securities = _security_rows(selected, commodity_shas)
    unmatched_weight = float(
        securities.filter(pl.col("security_id").is_null())["weight"].abs().sum()
    )
    total_weight = float(securities["weight"].abs().sum())
    first = selected.row(0, named=True)
    return {
        "fund_ticker": str(first["fund_ticker"]),
        "as_of_date": first["as_of_date"],
        "source_id": str(first["source_id"]),
        "unmatched_weight": unmatched_weight,
        "total_weight": total_weight,
        "share": unmatched_weight / total_weight if total_weight > 0 else 0.0,
    }


def _source_metadata(con: duckdb.DuckDBPyConnection) -> dict[str, tuple[str, str, str]]:
    """``source_id -> (issuer, kind, format)`` from the additive ``sources`` table.

    When the table does not exist (older database), every source is treated as
    an issuer named after itself with kind ``daily_holdings``, so the report
    and the threshold stay deterministic without guessing. A legacy table
    without the ``format`` column is read too; its format is then unknown.
    """
    queries = (
        "SELECT source_id, issuer, kind, format FROM sources",
        "SELECT source_id, issuer, kind, NULL AS format FROM sources",
    )
    rows: list[tuple[object, ...]] | None = None
    for query in queries:
        try:
            rows = con.execute(query).fetchall()
        except duckdb.Error:
            continue
        break
    if rows is None:
        return {}
    metadata: dict[str, tuple[str, str, str]] = {}
    for source_id, issuer, kind, source_format in rows:
        metadata[str(source_id)] = (
            str(issuer) if issuer is not None else str(source_id),
            str(kind) if kind is not None else _DAILY_KIND,
            str(source_format) if source_format is not None else "",
        )
    return metadata


def _quality_of(metadata: dict[str, tuple[str, str, str]], source_id: str) -> str:
    """``daily`` or ``quarterly`` for a source, from its recorded kind/format."""
    _issuer, kind, source_format = metadata.get(source_id, (source_id, _DAILY_KIND, ""))
    if kind == _QUARTERLY_KIND or source_format == _PDF_FORMAT:
        return _QUALITY_QUARTERLY
    return _QUALITY_DAILY


def _issuer_of(metadata: dict[str, tuple[str, str, str]], source_id: str) -> str:
    return metadata.get(source_id, (source_id, _DAILY_KIND, ""))[0]


def _is_metrics_check(check: str, source_id: str, metrics_sources: set[str]) -> bool:
    """Whether one ``qa_events`` row belongs to the fund-metrics path (D66).

    Row-level findings carry the metrics source id, so the ``sources`` table
    (or the rows in ``fund_metrics``) identifies them. A metrics parser failure
    has no metrics source to attach: the build records it against the raw
    file's own source, which is why the check name is matched too.
    """
    return source_id in metrics_sources or check in _METRICS_CHECKS


def _metrics_source_ids(
    con: duckdb.DuckDBPyConnection, metadata: dict[str, tuple[str, str, str]]
) -> set[str]:
    """Every source that produced or is declared to produce fund metrics.

    The ``sources`` table gives the declared kind; the ``fund_metrics`` table
    covers databases whose config predates the ``fund_metrics`` kind (and
    hand-made databases without a ``sources`` table). A missing
    ``fund_metrics`` table simply contributes nothing.
    """
    ids = {
        source_id
        for source_id, (_issuer, kind, _source_format) in metadata.items()
        if kind == _METRICS_KIND
    }
    try:
        rows = con.execute("SELECT DISTINCT source_id FROM fund_metrics").fetchall()
    except duckdb.Error:
        return ids
    ids.update(str(row[0]) for row in rows if row[0] is not None)
    return ids


def _metrics_universe(
    con: duckdb.DuckDBPyConnection,
    metadata: dict[str, tuple[str, str, str]],
    *,
    asof: date | None = None,
) -> tuple[tuple[str, ...], tuple[tuple[str, date], ...]]:
    """Funds with metrics rows and the latest metrics date per issuer.

    With ``asof``, only ``fund_metrics`` rows known before ``cutoff_utc(asof)``
    are considered, so the section is point-in-time like every other one.
    Returns empty tuples when the database has no ``fund_metrics`` table, so
    the report degrades to "no metrics" instead of failing.
    """
    cutoff = cutoff_utc(asof) if asof is not None else None
    date_filter = "WHERE known_at < ?" if cutoff is not None else ""
    params: list[object] = [cutoff] if cutoff is not None else []
    try:
        fund_rows = con.execute(
            f"SELECT DISTINCT fund_ticker FROM fund_metrics {date_filter} ORDER BY fund_ticker",
            params,
        ).fetchall()
        date_rows = con.execute(
            f"SELECT source_id, max(as_of_date) FROM fund_metrics {date_filter} GROUP BY source_id",
            params,
        ).fetchall()
    except duckdb.Error:
        return (), ()
    funds = tuple(str(row[0]) for row in fund_rows)
    latest: dict[str, date] = {}
    for source_id, latest_date in date_rows:
        if latest_date is None:
            continue
        issuer = _issuer_of(metadata, str(source_id))
        current = latest.get(issuer)
        if current is None or latest_date > current:
            latest[issuer] = latest_date
    return funds, tuple(sorted(latest.items()))


def _input_key(sha: object, source_id: object) -> tuple[str, str]:
    """The (SHA-256, source) input a ``file_versions`` or ``qa_events`` row belongs to."""
    return (str(sha), str(source_id))


def _share_rows(frame: pl.DataFrame) -> list[UnmatchedShare]:
    return [
        UnmatchedShare(
            fund_ticker=str(row["fund_ticker"]),
            as_of_date=row["as_of_date"],
            source_id=str(row["source_id"]),
            unmatched_weight=float(row["unmatched_weight"]),
            total_weight=float(row["total_weight"]),
            share=float(row["share"]),
        )
        for row in frame.iter_rows(named=True)
    ]


def _worst_rank(row: UnmatchedShare) -> tuple[float, str, str]:
    """Deterministic tie-break for choosing one file per fund."""
    return (row.share, row.as_of_date.isoformat(), row.source_id)


def _worst_sort_key(row: UnmatchedShare) -> tuple[float, str, str, str]:
    """Worst first; stable when shares tie."""
    return (-row.share, row.fund_ticker, row.as_of_date.isoformat(), row.source_id)


def summarize(
    con: duckdb.DuckDBPyConnection,
    max_unmatched_share: float | None = None,
    *,
    asof: date | None = None,
) -> QaSummary:
    """Read the built tables and collect everything the report needs.

    ``max_unmatched_share``, when set, flags included ``daily_holdings`` inputs
    whose unmatched weight share exceeds it; :func:`exit_code` then returns 3.
    A *file* is one stored raw input per source (SHA-256 + source id): a
    republished file is a new input, while a metrics history that yields many
    dated versions is still one. The top-line counts cover holdings inputs only;
    fund-metrics inputs have their own section (D66).

    With ``asof``, every section uses only versions whose ``known_at`` is before
    ``cutoff_utc(asof)`` (D7/D30): file counts, findings, excluded/inconsistent
    files, unmatched rows and shares, worst funds and fund-metrics rows. A
    republished file whose second version is known after ``asof`` therefore
    cannot change the report for that date.
    """
    if max_unmatched_share is not None and not 0.0 <= max_unmatched_share <= 1.0:
        raise ValueError("max_unmatched_share must be between 0 and 1")
    cutoff = cutoff_utc(asof) if asof is not None else None
    file_filter = "WHERE known_at < ?" if cutoff is not None else ""
    params: list[object] = [cutoff] if cutoff is not None else []
    try:
        file_rows = con.execute(
            "SELECT raw_file_sha256, source_id, fund_ticker, as_of_date, status "
            f"FROM file_versions {file_filter}",
            params,
        ).fetchall()
        event_rows = con.execute(
            'SELECT raw_file_sha256, source_id, fund_ticker, as_of_date, "check", '
            "severity, detail FROM qa_events"
        ).fetchall()
    except duckdb.Error:
        return _missing_tables()

    if cutoff is not None:
        known_shas = {str(row[0]) for row in file_rows}
        event_rows = [row for row in event_rows if str(row[0]) in known_shas]

    metadata = _source_metadata(con)
    metrics_sources = _metrics_source_ids(con, metadata)
    # A metrics-only check identifies its source even when the config's kind
    # predates ``fund_metrics`` (or the sources table is absent).
    metrics_sources.update(
        str(source_id)
        for _sha, source_id, _fund_ticker, _as_of, check, _severity, _detail in event_rows
        if str(check) in _METRICS_ROW_CHECKS
    )

    # Sort before grouping: the report must not depend on table row order.
    file_rows.sort(
        key=lambda row: (
            *_input_key(row[0], row[1]),
            str(row[2]),
            row[3] or date.min,
            str(row[4]),
        )
    )
    event_rows.sort(
        key=lambda row: (
            *_input_key(row[0], row[1]),
            str(row[2]),
            row[3] or date.min,
            str(row[4]),
            str(row[5]),
            str(row[6]),
        )
    )

    warnings = 0
    metric_counts = {"error": 0, "warning": 0, "info": 0}
    holdings_findings: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    metrics_findings: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    event_info: dict[tuple[str, str], tuple[str, str, date | None]] = {}
    for sha, source_id, fund_ticker, as_of_date, check, severity, detail in event_rows:
        key = _input_key(sha, source_id)
        finding = (str(check), str(severity), str(detail))
        event_info.setdefault(key, (str(fund_ticker), str(source_id), as_of_date))
        if _is_metrics_check(str(check), str(source_id), metrics_sources):
            metrics_findings.setdefault(key, []).append(finding)
            metric_counts[str(severity)] = metric_counts.get(str(severity), 0) + 1
        else:
            holdings_findings.setdefault(key, []).append(finding)
        if severity == "warning":
            warnings += 1

    inputs: dict[tuple[str, str], _Input] = {}
    versions_by_input: dict[tuple[str, str], list[tuple[str, date | None, str]]] = {}
    input_status: dict[tuple[str, str], str] = {}
    for sha, source_id, fund_ticker, as_of_date, status in file_rows:
        key = _input_key(sha, source_id)
        entry = inputs.get(key)
        if entry is None:
            entry = _Input(source_id=str(source_id))
            inputs[key] = entry
        entry.fund_tickers.add(str(fund_ticker))
        entry.versions += 1
        entry.statuses.add(str(status))
        if as_of_date is not None:
            entry.dates.append(as_of_date)
        versions_by_input.setdefault(key, []).append((str(fund_ticker), as_of_date, str(status)))
        if str(status) == "included":
            input_status[key] = "included"
        elif key not in input_status:
            input_status[key] = str(status)

    files_included = sum(
        1
        for key, status in input_status.items()
        if status == "included" and key[1] not in metrics_sources
    )
    files_excluded = sum(
        1
        for key, status in input_status.items()
        if status == "excluded" and key[1] not in metrics_sources
    )

    excluded_files: list[ExcludedFile] = []
    for sha, source_id, fund_ticker, as_of_date, status in file_rows:
        if status != "excluded" or str(source_id) in metrics_sources:
            continue
        excluded_files.append(
            ExcludedFile(
                fund_ticker=str(fund_ticker),
                as_of_date=as_of_date,
                source_id=str(source_id),
                findings=tuple(sorted(holdings_findings.get(_input_key(sha, source_id), []))),
            )
        )
    excluded_files.sort(
        key=lambda entry: (
            entry.fund_ticker,
            entry.as_of_date or date.min,
            entry.source_id,
            entry.findings,
        )
    )

    inconsistent_files: list[InconsistentFile] = []
    error_inputs = sorted(
        key
        for key, findings in holdings_findings.items()
        if any(severity == "error" for _check, severity, _detail in findings)
    )
    for key in error_inputs:
        findings = tuple(sorted(holdings_findings[key]))
        versions = versions_by_input.get(key)
        if not versions:
            fund_ticker, source_id, as_of_date = event_info[key]
            inconsistent_files.append(
                InconsistentFile(
                    fund_ticker=fund_ticker,
                    as_of_date=as_of_date,
                    source_id=source_id,
                    sha256=key[0],
                    reason="errored file has no file_versions row",
                    findings=findings,
                )
            )
            continue
        for fund_ticker, as_of_date, status in versions:
            if status == "excluded":
                continue
            inconsistent_files.append(
                InconsistentFile(
                    fund_ticker=fund_ticker,
                    as_of_date=as_of_date,
                    source_id=key[1],
                    sha256=key[0],
                    reason=f"errored but not excluded (status={status})",
                    findings=findings,
                )
            )
    inconsistent_files.sort(
        key=lambda entry: (
            entry.fund_ticker,
            entry.as_of_date or date.min,
            entry.source_id,
            entry.sha256,
        )
    )

    metrics = _metrics_summary(
        con=con,
        metadata=metadata,
        inputs=inputs,
        versions_by_input=versions_by_input,
        metrics_findings=metrics_findings,
        metrics_sources=metrics_sources,
        event_info=event_info,
        counts=metric_counts,
        asof=asof,
    )

    try:
        if asof is None:
            unmatched_row = con.execute(
                "SELECT count(*) FROM holdings WHERE security_id IS NULL "
                "AND lower(asset_class) NOT IN ('cash', 'fx', 'derivative') "
                f"AND {_balancing_predicate('security_name')} "
                f"AND {_commodity_predicate('asset_class', 'raw_file_sha256')}"
            ).fetchone()
            example_rows = con.execute(
                "SELECT fund_ticker, security_name FROM holdings WHERE security_id IS NULL "
                "AND lower(asset_class) NOT IN ('cash', 'fx', 'derivative') "
                f"AND {_balancing_predicate('security_name')} "
                f"AND {_commodity_predicate('asset_class', 'raw_file_sha256')} "
                "ORDER BY fund_ticker, security_name, weight DESC LIMIT 10"
            ).fetchall()
            measured = _measured_holdings(con)
        else:
            asof_frame = _asof_holdings(con, asof)
            asof_securities = _security_rows(asof_frame, _commodity_shas(con))
            unmatched_only = asof_securities.filter(pl.col("security_id").is_null())
            unmatched_row = (unmatched_only.height,)
            example_rows = [
                (row["fund_ticker"], row["security_name"])
                for row in unmatched_only.sort(
                    ["fund_ticker", "security_name", "weight"],
                    descending=[False, False, True],
                )
                .head(10)
                .iter_rows(named=True)
            ]
            measured = asof_frame
        shares_frame = unmatched_shares(con)
        if asof is not None:
            shares_frame = unmatched_shares(con, asof)
        balancing_lines, physical_commodity_lines = _line_usage(measured, _commodity_shas(con))
    except duckdb.Error:
        return _missing_tables()

    shares = _share_rows(shares_frame)

    by_quality_issuer: dict[str, dict[str, list[float]]] = {kind: {} for kind in _QUALITY_KINDS}
    worst_by_quality_fund: dict[str, dict[str, UnmatchedShare]] = {
        kind: {} for kind in _QUALITY_KINDS
    }
    for row in shares:
        quality = _quality_of(metadata, row.source_id)
        by_quality_issuer[quality].setdefault(_issuer_of(metadata, row.source_id), []).append(
            row.share
        )
        current = worst_by_quality_fund[quality].get(row.fund_ticker)
        if current is None or _worst_rank(row) > _worst_rank(current):
            worst_by_quality_fund[quality][row.fund_ticker] = row

    quality_summaries: list[UnmatchedQuality] = []
    for quality in _QUALITY_KINDS:
        issuer_medians = tuple(
            (issuer, float(median(values)))
            for issuer, values in sorted(by_quality_issuer[quality].items())
        )
        worst_funds = tuple(
            sorted(
                (row for row in worst_by_quality_fund[quality].values() if row.share > 0),
                key=_worst_sort_key,
            )[:_MAX_WORST_FUNDS]
        )
        quality_summaries.append(
            UnmatchedQuality(
                kind=quality,
                issuer_medians=issuer_medians,
                worst_funds=worst_funds,
            )
        )

    over_threshold: tuple[UnmatchedShare, ...] = ()
    if max_unmatched_share is not None:
        over_threshold = tuple(
            row
            for row in shares
            if row.share > max_unmatched_share
            and _quality_of(metadata, row.source_id) == _QUALITY_DAILY
        )

    return QaSummary(
        files_included=files_included,
        files_excluded=files_excluded,
        excluded_files=tuple(excluded_files),
        inconsistent_files=tuple(inconsistent_files),
        warnings=warnings,
        unmatched_rows=int(unmatched_row[0]) if unmatched_row is not None else 0,
        unmatched_examples=tuple(f"{row[0]} {row[1]}" for row in example_rows),
        unmatched_shares=tuple(shares),
        unmatched_quality=tuple(quality_summaries),
        max_unmatched_share=max_unmatched_share,
        unmatched_over_threshold=over_threshold,
        metrics=metrics,
        asof=asof,
        balancing_lines=balancing_lines,
        physical_commodity_lines=physical_commodity_lines,
    )


def _metrics_summary(
    *,
    con: duckdb.DuckDBPyConnection,
    metadata: dict[str, tuple[str, str, str]],
    inputs: dict[tuple[str, str], _Input],
    versions_by_input: dict[tuple[str, str], list[tuple[str, date | None, str]]],
    metrics_findings: dict[tuple[str, str], list[tuple[str, str, str]]],
    metrics_sources: set[str],
    event_info: dict[tuple[str, str], tuple[str, str, date | None]],
    counts: dict[str, int],
    asof: date | None = None,
) -> MetricsSummary:
    """Build the fund-metrics section of the summary (D66, interfaces wave 06 §4).

    A metrics input is one raw file parsed for one metrics source. Its versions
    may span many dates (a NAV history), so inclusion is accounted per input
    while findings, funds and the latest date per issuer come from the rows.
    An errored input passes only when its versions are all excluded; an errored
    input with no version at all (a metrics parser failure recorded against the
    raw file's source) is inconsistent, because nothing was excluded.
    """
    metric_keys = {key for key in inputs if key[1] in metrics_sources} | set(metrics_findings)
    excluded: list[MetricsFile] = []
    inconsistent: list[InconsistentMetricsFile] = []
    files_included = 0
    files_excluded = 0
    for key in sorted(metric_keys):
        # A finding can be recorded against the raw file's own source (a
        # metrics parser failure): the versions under that key are holdings
        # versions, so no metrics input exists for it.
        entry = inputs.get(key) if key[1] in metrics_sources else None
        findings = tuple(sorted(metrics_findings.get(key, [])))
        has_error = any(severity == "error" for _check, severity, _detail in findings)
        has_included = entry is not None and "included" in entry.statuses
        has_excluded = entry is not None and "excluded" in entry.statuses
        if entry is not None:
            if has_included:
                files_included += 1
            elif has_excluded:
                files_excluded += 1
            if has_excluded:
                excluded.append(
                    MetricsFile(
                        source_id=entry.source_id,
                        fund_tickers=tuple(sorted(entry.fund_tickers)),
                        as_of_date=max(entry.dates) if entry.dates else None,
                        versions=entry.versions,
                        findings=findings,
                    )
                )
        if not has_error or (has_excluded and not has_included):
            continue
        if entry is None:
            fund_ticker, source_id, as_of_date = event_info[key]
            inconsistent.append(
                InconsistentMetricsFile(
                    source_id=source_id,
                    fund_tickers=(fund_ticker,),
                    as_of_date=as_of_date,
                    versions=0,
                    reason="errored metrics file has no file_versions row",
                    findings=findings,
                )
            )
            continue
        inconsistent.append(
            InconsistentMetricsFile(
                source_id=entry.source_id,
                fund_tickers=tuple(sorted(entry.fund_tickers)),
                as_of_date=max(entry.dates) if entry.dates else None,
                versions=entry.versions,
                reason="errored but not excluded (status=included)",
                findings=findings,
            )
        )
    excluded.sort(key=_metrics_file_sort_key)
    inconsistent.sort(key=_metrics_file_sort_key)

    funds, latest_by_issuer = _metrics_universe(con, metadata, asof=asof)
    return MetricsSummary(
        files_included=files_included,
        files_excluded=files_excluded,
        excluded_files=tuple(excluded),
        inconsistent_files=tuple(inconsistent),
        funds=funds,
        latest_by_issuer=latest_by_issuer,
        errors=counts.get("error", 0),
        warnings=counts.get("warning", 0),
        infos=counts.get("info", 0),
    )


def _metrics_file_sort_key(
    entry: MetricsFile | InconsistentMetricsFile,
) -> tuple[str, tuple[str, ...], date, int, tuple[tuple[str, str, str], ...]]:
    """Deterministic order for metrics entries, independent of insertion order."""
    return (
        entry.source_id,
        entry.fund_tickers,
        entry.as_of_date or date.min,
        entry.versions,
        entry.findings,
    )


def _render_findings(prefix: str, findings: tuple[tuple[str, str, str], ...]) -> list[str]:
    return [f"{prefix}{severity} {check}: {detail}" for check, severity, detail in findings]


def _render_share(line_prefix: str, row: UnmatchedShare) -> str:
    return (
        f"{line_prefix}{row.fund_ticker} as_of={row.as_of_date.isoformat()} "
        f"source={row.source_id} share={row.share:.2%}"
    )


def _render_metrics_file(
    entry: MetricsFile | InconsistentMetricsFile,
) -> str:
    as_of = entry.as_of_date.isoformat() if entry.as_of_date is not None else "-"
    return (
        f"as_of={as_of} source={entry.source_id} "
        f"funds={','.join(entry.fund_tickers)} versions={entry.versions}"
    )


def _render_metrics(metrics: MetricsSummary) -> list[str]:
    """The fund-metrics health section (D66)."""
    total = metrics.files_included + metrics.files_excluded
    lines = [
        "fund metrics:",
        f"  files: {total} total ({metrics.files_included} included, "
        f"{metrics.files_excluded} excluded)",
        f"  funds with metrics: {len(metrics.funds)}",
    ]
    if metrics.latest_by_issuer:
        lines.append("  latest as-of per issuer:")
        lines.extend(
            f"    {issuer} {latest.isoformat()}" for issuer, latest in metrics.latest_by_issuer
        )
    else:
        lines.append("  latest as-of per issuer: none")
    lines.append(
        f"  findings: {metrics.errors} error(s), {metrics.warnings} warning(s), "
        f"{metrics.infos} info(s)"
    )
    if metrics.excluded_files:
        lines.append("  excluded files:")
        for entry in metrics.excluded_files:
            lines.append(f"    {_render_metrics_file(entry)}")
            lines.extend(_render_findings("      ", entry.findings))
    if metrics.inconsistent_files:
        lines.append("  inconsistent:")
        for inconsistent in metrics.inconsistent_files:
            lines.append(f"    {_render_metrics_file(inconsistent)}: {inconsistent.reason}")
            lines.extend(_render_findings("      ", inconsistent.findings))
    return lines


def render(summary: QaSummary) -> str:
    """Render a deterministic, human-readable QA report."""
    if summary.tables_missing:
        return "QA report\nstatus: FAILED (missing tables; run etfh build first)\n"
    lines = [
        "QA report",
        f"files: {summary.files_included + summary.files_excluded} total "
        f"({summary.files_included} included, {summary.files_excluded} excluded)",
    ]
    if summary.excluded_files:
        lines.append("excluded files:")
        for entry in summary.excluded_files:
            as_of = entry.as_of_date.isoformat() if entry.as_of_date is not None else "-"
            lines.append(f"  {entry.fund_ticker} as_of={as_of} source={entry.source_id}")
            lines.extend(_render_findings("    ", entry.findings))
    if summary.inconsistent_files:
        lines.append("inconsistent:")
        for inconsistent in summary.inconsistent_files:
            as_of = (
                inconsistent.as_of_date.isoformat() if inconsistent.as_of_date is not None else "-"
            )
            lines.append(
                f"  {inconsistent.fund_ticker} as_of={as_of} source={inconsistent.source_id} "
                f"sha={inconsistent.sha256}: {inconsistent.reason}"
            )
            lines.extend(_render_findings("    ", inconsistent.findings))
    lines.append(f"warnings: {summary.warnings}")
    if summary.unmatched_rows:
        lines.append(f"unmatched securities: {summary.unmatched_rows} row(s), showing up to 10:")
        lines.extend(f"  {example}" for example in summary.unmatched_examples)
    else:
        lines.append("unmatched securities: 0")
    if summary.balancing_lines.rows:
        lines.append(
            f"balancing lines: {summary.balancing_lines.rows} rows, "
            f"Σ weight {summary.balancing_lines.weight_sum:.4f}"
        )
    if summary.physical_commodity_lines.rows:
        lines.append(
            f"physical commodity lines: {summary.physical_commodity_lines.rows} rows, "
            f"Σ weight {summary.physical_commodity_lines.weight_sum:.4f}"
        )
    if summary.unmatched_shares:
        lines.append("unmatched weight share (included files):")
        for quality in summary.unmatched_quality:
            label = f"  {quality.kind} files"
            if not quality.issuer_medians and not quality.worst_funds:
                lines.append(f"{label}: none")
                continue
            lines.append(f"{label}:")
            if quality.issuer_medians:
                lines.append("    issuer median:")
                lines.extend(
                    f"      {issuer} {value:.2%}" for issuer, value in quality.issuer_medians
                )
            else:
                lines.append("    issuer median: none")
            if quality.worst_funds:
                lines.append("    worst funds:")
                lines.extend(_render_share("      ", row) for row in quality.worst_funds)
            else:
                lines.append("    worst funds: none")
    else:
        lines.append("unmatched weight share (included files): none")
    if summary.max_unmatched_share is not None:
        threshold = f"{summary.max_unmatched_share:.2%}"
        if summary.unmatched_over_threshold:
            lines.append(
                f"unmatched threshold {threshold} exceeded by "
                f"{len(summary.unmatched_over_threshold)} included daily file(s):"
            )
            lines.extend(_render_share("  ", row) for row in summary.unmatched_over_threshold)
        else:
            lines.append(f"unmatched threshold {threshold}: no included daily file exceeds it")
    lines.extend(_render_metrics(summary.metrics))
    return "\n".join(lines) + "\n"


def _render_check_names(findings: tuple[tuple[str, str, str], ...]) -> str:
    """Check names and severities only: never the detail, which names securities."""
    return ", ".join(f"{check} ({severity})" for check, severity, _detail in findings)


def _markdown_metrics(metrics: MetricsSummary) -> list[str]:
    """Publish-safe fund-metrics section: counts, tickers and check names only."""
    total = metrics.files_included + metrics.files_excluded
    lines = [
        "### Fund metrics",
        f"- files: {total} total ({metrics.files_included} included, "
        f"{metrics.files_excluded} excluded)",
        f"- funds with metrics: {len(metrics.funds)}",
        f"- findings: {metrics.errors} error(s), {metrics.warnings} warning(s), "
        f"{metrics.infos} info(s)",
    ]
    if metrics.latest_by_issuer:
        latest = ", ".join(
            f"{issuer} {day.isoformat()}" for issuer, day in metrics.latest_by_issuer
        )
        lines.append(f"- latest as-of per issuer: {latest}")
    else:
        lines.append("- latest as-of per issuer: none")
    if metrics.excluded_files:
        lines.append("- excluded files:")
        lines.extend(
            f"  - {_render_metrics_file(entry)} — {_render_check_names(entry.findings)}"
            for entry in metrics.excluded_files
        )
    if metrics.inconsistent_files:
        lines.append("- inconsistent:")
        lines.extend(
            f"  - {_render_metrics_file(entry)} — {entry.reason}; "
            f"{_render_check_names(entry.findings)}"
            for entry in metrics.inconsistent_files
        )
    return lines


def render_markdown(summary: QaSummary) -> str:
    """Render a publish-safe markdown summary (interfaces wave 08 §1, D77/D79).

    Safe for a workflow job summary: counts, shares (percentages), issuer and
    fund tickers, dates, source ids and check names. Security names, security
    ids, weight values and file paths never appear. ``render_json`` carries the
    same numbers for machines.
    """
    code = exit_code(summary)
    lines = ["## etfh qa"]
    if summary.tables_missing:
        lines.append("**Status:** FAILED (missing tables; run `etfh build` first)")
        lines.append("")
        lines.append(f"**Exit status:** {code}")
        return "\n".join(lines)
    if summary.asof is not None:
        lines.append(f"**As of:** {summary.asof.isoformat()}")
    lines.append(
        f"**Files:** {summary.files_included + summary.files_excluded} total "
        f"({summary.files_included} included, {summary.files_excluded} excluded)"
    )
    lines.append(f"**Warnings:** {summary.warnings}")
    lines.append(f"**Unmatched securities:** {summary.unmatched_rows} row(s)")
    lines.append(f"**Balancing lines:** {summary.balancing_lines.rows} row(s)")
    lines.append(f"**Physical commodity lines:** {summary.physical_commodity_lines.rows} row(s)")

    if summary.excluded_files:
        lines.append("")
        lines.append("### Excluded files")
        for entry in summary.excluded_files:
            as_of = entry.as_of_date.isoformat() if entry.as_of_date is not None else "-"
            checks = _render_check_names(entry.findings)
            suffix = f" — {checks}" if checks else ""
            lines.append(f"- {entry.fund_ticker} · {as_of} · {entry.source_id}{suffix}")
    if summary.inconsistent_files:
        lines.append("")
        lines.append("### Inconsistent files")
        for inconsistent in summary.inconsistent_files:
            as_of = (
                inconsistent.as_of_date.isoformat() if inconsistent.as_of_date is not None else "-"
            )
            checks = _render_check_names(inconsistent.findings)
            suffix = f" — {checks}" if checks else ""
            lines.append(
                f"- {inconsistent.fund_ticker} · {as_of} · {inconsistent.source_id} "
                f"— {inconsistent.reason}{suffix}"
            )

    lines.append("")
    lines.append("### Unmatched share (included files)")
    if not summary.unmatched_shares:
        lines.append("- none")
    for quality in summary.unmatched_quality:
        lines.append(f"**{quality.kind}**")
        if not quality.issuer_medians and not quality.worst_funds:
            lines.append("- none")
            continue
        if quality.issuer_medians:
            medians = ", ".join(f"{issuer} {value:.2%}" for issuer, value in quality.issuer_medians)
            lines.append(f"- issuer median share: {medians}")
        else:
            lines.append("- issuer median share: none")
        if quality.worst_funds:
            lines.append("- worst funds:")
            lines.extend(
                f"  - {row.fund_ticker} · {row.as_of_date.isoformat()} · {row.source_id} · "
                f"{row.share:.2%}"
                for row in quality.worst_funds
            )
        else:
            lines.append("- worst funds: none")
    if summary.max_unmatched_share is not None:
        threshold = f"{summary.max_unmatched_share:.2%}"
        if summary.unmatched_over_threshold:
            lines.append(
                f"**Unmatched threshold:** {threshold} exceeded by "
                f"{len(summary.unmatched_over_threshold)} included daily file(s)"
            )
            lines.extend(
                f"- {row.fund_ticker} · {row.as_of_date.isoformat()} · {row.source_id} · "
                f"{row.share:.2%}"
                for row in summary.unmatched_over_threshold
            )
        else:
            lines.append(f"**Unmatched threshold:** {threshold}; no included daily file exceeds it")

    lines.append("")
    lines.extend(_markdown_metrics(summary.metrics))
    lines.append("")
    lines.append(f"**Exit status:** {code}")
    return "\n".join(lines)


def _share_payload(row: UnmatchedShare) -> dict[str, Any]:
    return {
        "fund_ticker": row.fund_ticker,
        "as_of_date": row.as_of_date.isoformat(),
        "source_id": row.source_id,
        "unmatched_weight": row.unmatched_weight,
        "total_weight": row.total_weight,
        "share": row.share,
    }


def _metrics_payload(metrics: MetricsSummary) -> dict[str, Any]:
    return {
        "files_included": metrics.files_included,
        "files_excluded": metrics.files_excluded,
        "funds": list(metrics.funds),
        "latest_by_issuer": [
            {"issuer": issuer, "as_of_date": latest.isoformat()}
            for issuer, latest in metrics.latest_by_issuer
        ],
        "errors": metrics.errors,
        "warnings": metrics.warnings,
        "infos": metrics.infos,
        "excluded_files": [
            {
                "source_id": entry.source_id,
                "fund_tickers": list(entry.fund_tickers),
                "as_of_date": entry.as_of_date.isoformat() if entry.as_of_date else None,
                "versions": entry.versions,
                "findings": [
                    {"check": check, "severity": severity}
                    for check, severity, _detail in entry.findings
                ],
            }
            for entry in metrics.excluded_files
        ],
        "inconsistent_files": [
            {
                "source_id": entry.source_id,
                "fund_tickers": list(entry.fund_tickers),
                "as_of_date": entry.as_of_date.isoformat() if entry.as_of_date else None,
                "versions": entry.versions,
                "reason": entry.reason,
                "findings": [
                    {"check": check, "severity": severity}
                    for check, severity, _detail in entry.findings
                ],
            }
            for entry in metrics.inconsistent_files
        ],
    }


def render_json(summary: QaSummary) -> str:
    """Render the QA summary as deterministic JSON for machines.

    Mirrors the numbers in :func:`render` and :func:`render_markdown`; like the
    markdown report it carries check names but no finding details, so it never
    leaks a security name, id, weight or file path into a workflow summary.
    """
    payload: dict[str, Any] = {
        "asof": summary.asof.isoformat() if summary.asof is not None else None,
        "tables_missing": summary.tables_missing,
        "files": {
            "total": summary.files_included + summary.files_excluded,
            "included": summary.files_included,
            "excluded": summary.files_excluded,
        },
        "warnings": summary.warnings,
        "excluded_files": [
            {
                "fund_ticker": entry.fund_ticker,
                "as_of_date": entry.as_of_date.isoformat() if entry.as_of_date else None,
                "source_id": entry.source_id,
                "findings": [
                    {"check": check, "severity": severity}
                    for check, severity, _detail in entry.findings
                ],
            }
            for entry in summary.excluded_files
        ],
        "inconsistent_files": [
            {
                "fund_ticker": entry.fund_ticker,
                "as_of_date": entry.as_of_date.isoformat() if entry.as_of_date else None,
                "source_id": entry.source_id,
                "reason": entry.reason,
                "findings": [
                    {"check": check, "severity": severity}
                    for check, severity, _detail in entry.findings
                ],
            }
            for entry in summary.inconsistent_files
        ],
        "unmatched_rows": summary.unmatched_rows,
        "non_security_lines": {
            "balancing": {
                "rows": summary.balancing_lines.rows,
                "weight_sum": summary.balancing_lines.weight_sum,
            },
            "physical_commodity": {
                "rows": summary.physical_commodity_lines.rows,
                "weight_sum": summary.physical_commodity_lines.weight_sum,
            },
        },
        "unmatched_shares": {
            quality.kind: {
                "issuer_medians": [
                    {"issuer": issuer, "share": value} for issuer, value in quality.issuer_medians
                ],
                "worst_funds": [_share_payload(row) for row in quality.worst_funds],
            }
            for quality in summary.unmatched_quality
        },
        "max_unmatched_share": summary.max_unmatched_share,
        "unmatched_over_threshold": [
            _share_payload(row) for row in summary.unmatched_over_threshold
        ],
        "fund_metrics": _metrics_payload(summary.metrics),
        "exit_status": exit_code(summary),
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def exit_code(summary: QaSummary) -> int:
    """1 when tables are missing or the database is inconsistent, 3 when the
    unmatched threshold is exceeded, 0 otherwise."""
    if summary.tables_missing or summary.inconsistent_files or summary.metrics.inconsistent_files:
        return 1
    if summary.unmatched_over_threshold:
        return 3
    return 0
