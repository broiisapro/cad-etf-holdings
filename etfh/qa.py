"""QA summary, text report and exit code for a built database.

Reads ``file_versions``, ``qa_events`` and (when present) ``holdings`` and
``fund_metrics`` from a DuckDB built by ``etfh build``; the additive ``sources``
table (D53) supplies the issuer, source kind and format when it exists.

The report keeps daily holdings and quarterly portfolio disclosures in
separate quality sections: each gets its own per-issuer median unmatched weight
share and its own worst-funds list (only funds with a share above zero, so a
clean portfolio does not pad the list), and the shares are derived from the
source kind, never from weight-sum heuristics.

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

from dataclasses import dataclass, field
from datetime import date
from statistics import median

import duckdb
import polars as pl
from polars.datatypes import DataType, DataTypeClass

from etfh import analytics

__all__ = [
    "ExcludedFile",
    "InconsistentFile",
    "InconsistentMetricsFile",
    "MetricsFile",
    "MetricsSummary",
    "QaSummary",
    "UnmatchedQuality",
    "UnmatchedShare",
    "exit_code",
    "render",
    "summarize",
    "unmatched_shares",
]

_DAILY_KIND = "daily_holdings"
_QUARTERLY_KIND = "quarterly_disclosure"
_METRICS_KIND = "fund_metrics"
_PDF_FORMAT = "pdf"
_QUALITY_DAILY = "daily"
_QUALITY_QUARTERLY = "quarterly"
# Fixed order: the report must not depend on dict or database row order.
_QUALITY_KINDS = (_QUALITY_DAILY, _QUALITY_QUARTERLY)
_MAX_WORST_FUNDS = 10
_NON_SECURITY_CLASSES = ("cash", "fx", "derivative")
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
    tables_missing: bool = False


def _missing_tables() -> QaSummary:
    return QaSummary(tables_missing=True)


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
    null ``security_id`` whose ``asset_class`` is not one of
    cash/fx/derivative, and ``total`` is the same sum over every
    non-cash/fx/derivative row. The share is 0 when the total is 0. Returns the
    columns ``fund_ticker, as_of_date, source_id, unmatched_weight,
    total_weight, share``.
    """
    if asof is not None:
        return _unmatched_shares_asof(con, asof)
    rows = con.execute(
        """
        WITH latest AS (
            SELECT source_id, fund_ticker, as_of_date, max(file_version) AS file_version
            FROM file_versions
            WHERE status = 'included'
            GROUP BY source_id, fund_ticker, as_of_date
        )
        SELECT h.fund_ticker,
               h.as_of_date,
               h.source_id,
               COALESCE(SUM(ABS(h.weight)) FILTER (
                   WHERE h.security_id IS NULL
                     AND lower(h.asset_class) NOT IN ('cash', 'fx', 'derivative')
               ), 0.0) AS unmatched_weight,
               COALESCE(SUM(ABS(h.weight)) FILTER (
                   WHERE lower(h.asset_class) NOT IN ('cash', 'fx', 'derivative')
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
    for fund in analytics.funds_available(con):
        selected = analytics.holdings_asof(con, fund, asof)
        if selected.is_empty():
            continue
        rows.append(_asof_share_row(selected))
    return pl.DataFrame(rows, schema=_SHARE_SCHEMA)


def _asof_share_row(selected: pl.DataFrame) -> dict[str, object]:
    """The unmatched share of one analytics-selected holdings frame."""
    securities = selected.filter(
        ~pl.col("asset_class").str.to_lowercase().is_in(_NON_SECURITY_CLASSES)
    )
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
    con: duckdb.DuckDBPyConnection, metadata: dict[str, tuple[str, str, str]]
) -> tuple[tuple[str, ...], tuple[tuple[str, date], ...]]:
    """Funds with metrics rows and the latest metrics date per issuer.

    Returns empty tuples when the database has no ``fund_metrics`` table, so
    the report degrades to "no metrics" instead of failing.
    """
    try:
        fund_rows = con.execute(
            "SELECT DISTINCT fund_ticker FROM fund_metrics ORDER BY fund_ticker"
        ).fetchall()
        date_rows = con.execute(
            "SELECT source_id, max(as_of_date) FROM fund_metrics GROUP BY source_id"
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
) -> QaSummary:
    """Read the built tables and collect everything the report needs.

    ``max_unmatched_share``, when set, flags included ``daily_holdings`` inputs
    whose unmatched weight share exceeds it; :func:`exit_code` then returns 3.
    A *file* is one stored raw input per source (SHA-256 + source id): a
    republished file is a new input, while a metrics history that yields many
    dated versions is still one. The top-line counts cover holdings inputs only;
    fund-metrics inputs have their own section (D66).
    """
    if max_unmatched_share is not None and not 0.0 <= max_unmatched_share <= 1.0:
        raise ValueError("max_unmatched_share must be between 0 and 1")
    try:
        file_rows = con.execute(
            "SELECT raw_file_sha256, source_id, fund_ticker, as_of_date, status FROM file_versions"
        ).fetchall()
        event_rows = con.execute(
            'SELECT raw_file_sha256, source_id, fund_ticker, as_of_date, "check", '
            "severity, detail FROM qa_events"
        ).fetchall()
    except duckdb.Error:
        return _missing_tables()

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
    )

    try:
        unmatched_row = con.execute(
            "SELECT count(*) FROM holdings WHERE security_id IS NULL "
            "AND lower(asset_class) NOT IN ('cash', 'fx', 'derivative')"
        ).fetchone()
        examples = con.execute(
            "SELECT fund_ticker, security_name FROM holdings WHERE security_id IS NULL "
            "AND lower(asset_class) NOT IN ('cash', 'fx', 'derivative') "
            "ORDER BY fund_ticker, security_name, weight DESC LIMIT 10"
        ).fetchall()
        shares_frame = unmatched_shares(con)
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
        unmatched_examples=tuple(f"{row[0]} {row[1]}" for row in examples),
        unmatched_shares=tuple(shares),
        unmatched_quality=tuple(quality_summaries),
        max_unmatched_share=max_unmatched_share,
        unmatched_over_threshold=over_threshold,
        metrics=metrics,
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

    funds, latest_by_issuer = _metrics_universe(con, metadata)
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
    return "\n".join(lines) + "\n"


def exit_code(summary: QaSummary) -> int:
    """1 when tables are missing or the database is inconsistent, 3 when the
    unmatched threshold is exceeded, 0 otherwise."""
    if summary.tables_missing or summary.inconsistent_files or summary.metrics.inconsistent_files:
        return 1
    if summary.unmatched_over_threshold:
        return 3
    return 0
