"""QA summary, text report and exit code for a built database.

Reads ``file_versions``, ``qa_events`` and (when present) ``holdings`` from a
DuckDB built by ``etfh build``; the additive ``sources`` table (D53) supplies
the issuer and source kind when it exists.

Rules the report and exit code encode:

- every raw file with an ``error`` finding is listed; a file that is errored
  but not excluded (or has no ``file_versions`` row at all) is additionally
  reported under ``inconsistent:`` and keeps exit code 1;
- exit code 1 means the tables are missing or the database is inconsistent;
- exit code 3 means a ``--max-unmatched-share`` threshold was exceeded;
- otherwise 0: every errored file is excluded and reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import median

import duckdb
import polars as pl

__all__ = [
    "ExcludedFile",
    "InconsistentFile",
    "QaSummary",
    "UnmatchedShare",
    "exit_code",
    "render",
    "summarize",
    "unmatched_shares",
]

_NON_SECURITY_ASSET_CLASSES = ("cash", "fx", "derivative")
_DAILY_KIND = "daily_holdings"
_MAX_WORST_FUNDS = 10


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
    """One included file version's share of unmatched non-cash weight."""

    fund_ticker: str
    as_of_date: date
    source_id: str
    unmatched_weight: float
    total_weight: float
    share: float


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
    unmatched_issuer_medians: tuple[tuple[str, float], ...] = ()
    unmatched_worst_funds: tuple[UnmatchedShare, ...] = ()
    max_unmatched_share: float | None = None
    unmatched_over_threshold: tuple[UnmatchedShare, ...] = ()
    tables_missing: bool = False


def _missing_tables() -> QaSummary:
    return QaSummary(tables_missing=True)


def unmatched_shares(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Unmatched non-cash weight share per included fund/as-of/source.

    For every ``(fund_ticker, as_of_date, source_id)`` the latest included
    ``file_version`` is used (the version analytics select for that date, so a
    republished file is measured once, not accumulated). ``unmatched`` is the
    sum of ``abs(weight)`` over rows with a null ``security_id`` whose
    ``asset_class`` is not one of cash/fx/derivative; ``total`` is the same sum
    over every non-cash/fx/derivative row. The share is 0 when the total is 0.
    Returns the columns ``fund_ticker, as_of_date, source_id,
    unmatched_weight, total_weight, share``.
    """
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
    ).select(
        "fund_ticker",
        "as_of_date",
        "source_id",
        "unmatched_weight",
        "total_weight",
        "share",
    )


def _source_metadata(con: duckdb.DuckDBPyConnection) -> dict[str, tuple[str, str]]:
    """``source_id -> (issuer, kind)`` from the additive ``sources`` table.

    When the table does not exist (older database), every source is treated as
    an issuer named after itself with kind ``daily_holdings``, so the report
    and the threshold stay deterministic without guessing.
    """
    try:
        rows = con.execute("SELECT source_id, issuer, kind FROM sources").fetchall()
    except duckdb.Error:
        return {}
    metadata: dict[str, tuple[str, str]] = {}
    for source_id, issuer, kind in rows:
        metadata[str(source_id)] = (
            str(issuer) if issuer is not None else str(source_id),
            str(kind) if kind is not None else _DAILY_KIND,
        )
    return metadata


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


def summarize(
    con: duckdb.DuckDBPyConnection,
    max_unmatched_share: float | None = None,
) -> QaSummary:
    """Read the built tables and collect everything the report needs.

    ``max_unmatched_share``, when set, flags included ``daily_holdings`` file
    versions whose unmatched weight share exceeds it; :func:`exit_code` then
    returns 3.
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

    findings_by_sha: dict[str, list[tuple[str, str, str]]] = {}
    warnings = 0
    event_info_by_sha: dict[str, tuple[str, str, date | None]] = {}
    for sha, source_id, fund_ticker, as_of_date, check, severity, detail in event_rows:
        key = str(sha)
        findings_by_sha.setdefault(key, []).append((str(check), str(severity), str(detail)))
        event_info_by_sha.setdefault(key, (str(source_id), str(fund_ticker), as_of_date))
        if severity == "warning":
            warnings += 1
    error_shas = {
        sha
        for sha, findings in findings_by_sha.items()
        if any(severity == "error" for _check, severity, _detail in findings)
    }

    versions_by_sha: dict[str, list[tuple[str, str, date | None, str]]] = {}
    files_included = 0
    files_excluded = 0
    for sha, source_id, fund_ticker, as_of_date, status in file_rows:
        keyword = str(sha)
        versions_by_sha.setdefault(keyword, []).append(
            (str(source_id), str(fund_ticker), as_of_date, str(status))
        )
        if status == "included":
            files_included += 1
        elif status == "excluded":
            files_excluded += 1

    excluded_files: list[ExcludedFile] = []
    for sha, source_id, fund_ticker, as_of_date, status in file_rows:
        if status != "excluded":
            continue
        excluded_files.append(
            ExcludedFile(
                fund_ticker=str(fund_ticker),
                as_of_date=as_of_date,
                source_id=str(source_id),
                findings=tuple(sorted(findings_by_sha.get(str(sha), []))),
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
    for sha in sorted(error_shas):
        findings = tuple(sorted(findings_by_sha[sha]))
        versions = versions_by_sha.get(sha)
        if not versions:
            source_id, fund_ticker, as_of_date = event_info_by_sha[sha]
            inconsistent_files.append(
                InconsistentFile(
                    fund_ticker=fund_ticker,
                    as_of_date=as_of_date,
                    source_id=source_id,
                    sha256=sha,
                    reason="errored file has no file_versions row",
                    findings=findings,
                )
            )
            continue
        for source_id, fund_ticker, as_of_date, status in versions:
            if status == "excluded":
                continue
            inconsistent_files.append(
                InconsistentFile(
                    fund_ticker=fund_ticker,
                    as_of_date=as_of_date,
                    source_id=source_id,
                    sha256=sha,
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
    metadata = _source_metadata(con)
    by_issuer: dict[str, list[float]] = {}
    for row in shares:
        issuer = metadata.get(row.source_id, (row.source_id, _DAILY_KIND))[0]
        by_issuer.setdefault(issuer, []).append(row.share)
    issuer_medians = tuple(
        (issuer, float(median(values))) for issuer, values in sorted(by_issuer.items())
    )

    worst_by_fund: dict[str, UnmatchedShare] = {}
    for row in shares:
        current = worst_by_fund.get(row.fund_ticker)
        if current is None or (
            row.share,
            row.as_of_date.isoformat(),
            row.source_id,
        ) > (
            current.share,
            current.as_of_date.isoformat(),
            current.source_id,
        ):
            worst_by_fund[row.fund_ticker] = row
    worst_funds = tuple(
        sorted(worst_by_fund.values(), key=lambda row: (-row.share, row.fund_ticker))[
            :_MAX_WORST_FUNDS
        ]
    )

    over_threshold: tuple[UnmatchedShare, ...] = ()
    if max_unmatched_share is not None:
        over_threshold = tuple(
            row
            for row in shares
            if row.share > max_unmatched_share
            and metadata.get(row.source_id, ("", _DAILY_KIND))[1] == _DAILY_KIND
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
        unmatched_issuer_medians=issuer_medians,
        unmatched_worst_funds=worst_funds,
        max_unmatched_share=max_unmatched_share,
        unmatched_over_threshold=over_threshold,
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
        lines.append("  issuer median:")
        for issuer, value in summary.unmatched_issuer_medians:
            lines.append(f"    {issuer} {value:.2%}")
        lines.append("  worst funds:")
        lines.extend(_render_share("    ", row) for row in summary.unmatched_worst_funds)
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
    if summary.tables_missing or summary.inconsistent_files:
        return 1
    if summary.unmatched_over_threshold:
        return 3
    return 0
