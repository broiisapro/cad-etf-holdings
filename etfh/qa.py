"""QA summary, text report and exit code for a built database.

Reads ``file_versions``, ``qa_events`` and (when present) ``holdings`` from a
DuckDB built by ``etfh build``. The exit code is 0 when every file with an
error finding is excluded and reported, and 1 when tables are missing or an
errored file is included: that is the rule the definition of done relies on,
so two excluded bad files still exit 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import duckdb

__all__ = ["ExcludedFile", "QaSummary", "exit_code", "render", "summarize"]


@dataclass(frozen=True, slots=True)
class ExcludedFile:
    """One excluded file and all of its recorded findings."""

    fund_ticker: str
    as_of_date: date | None
    source_id: str
    findings: tuple[tuple[str, str, str], ...]  # (check, severity, detail)


@dataclass(frozen=True, slots=True)
class QaSummary:
    """Totals, excluded files and unmatched counts read from the database."""

    files_included: int = 0
    files_excluded: int = 0
    excluded_files: tuple[ExcludedFile, ...] = ()
    warnings: int = 0
    unmatched_rows: int = 0
    unmatched_examples: tuple[str, ...] = ()
    unexcluded_error_shas: tuple[str, ...] = ()
    tables_missing: bool = False


def _missing_tables() -> QaSummary:
    return QaSummary(tables_missing=True)


def summarize(con: duckdb.DuckDBPyConnection) -> QaSummary:
    """Read the built tables and collect everything the report needs."""
    try:
        file_rows = con.execute(
            "SELECT raw_file_sha256, source_id, fund_ticker, as_of_date, status FROM file_versions"
        ).fetchall()
        event_rows = con.execute(
            'SELECT raw_file_sha256, "check", severity, detail FROM qa_events'
        ).fetchall()
    except duckdb.Error:
        return _missing_tables()

    status_by_sha: dict[str, set[str]] = {}
    files_included = 0
    files_excluded = 0
    for sha, _source_id, _fund_ticker, _as_of_date, status in file_rows:
        status_by_sha.setdefault(str(sha), set()).add(str(status))
        if status == "included":
            files_included += 1
        elif status == "excluded":
            files_excluded += 1

    findings_by_sha: dict[str, list[tuple[str, str, str]]] = {}
    warnings = 0
    for sha, check, severity, detail in event_rows:
        findings_by_sha.setdefault(str(sha), []).append((str(check), str(severity), str(detail)))
        if severity == "warning":
            warnings += 1

    error_shas = {
        sha
        for sha, findings in findings_by_sha.items()
        if any(severity == "error" for _check, severity, _detail in findings)
    }
    unexcluded_error_shas = tuple(
        sorted(sha for sha in error_shas if "excluded" not in status_by_sha.get(sha, set()))
    )

    excluded_files: list[ExcludedFile] = []
    for sha, _source_id, fund_ticker, as_of_date, status in file_rows:
        if status != "excluded":
            continue
        findings = tuple(sorted(findings_by_sha.get(str(sha), [])))
        excluded_files.append(
            ExcludedFile(
                fund_ticker=str(fund_ticker),
                as_of_date=as_of_date,
                source_id=str(_source_id),
                findings=findings,
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

    try:
        unmatched_row = con.execute(
            "SELECT count(*) FROM holdings WHERE security_id IS NULL"
        ).fetchone()
        examples = con.execute(
            "SELECT fund_ticker, security_name FROM holdings WHERE security_id IS NULL "
            "ORDER BY fund_ticker, security_name, weight DESC LIMIT 10"
        ).fetchall()
    except duckdb.Error:
        return _missing_tables()

    return QaSummary(
        files_included=files_included,
        files_excluded=files_excluded,
        excluded_files=tuple(excluded_files),
        warnings=warnings,
        unmatched_rows=int(unmatched_row[0]) if unmatched_row is not None else 0,
        unmatched_examples=tuple(f"{row[0]} {row[1]}" for row in examples),
        unexcluded_error_shas=unexcluded_error_shas,
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
            for check, severity, detail in entry.findings:
                lines.append(f"    {severity} {check}: {detail}")
    lines.append(f"warnings: {summary.warnings}")
    if summary.unmatched_rows:
        lines.append(f"unmatched securities: {summary.unmatched_rows} row(s), showing up to 10:")
        lines.extend(f"  {example}" for example in summary.unmatched_examples)
    else:
        lines.append("unmatched securities: 0")
    return "\n".join(lines) + "\n"


def exit_code(summary: QaSummary) -> int:
    """0 when every errored file is excluded and reported; 1 otherwise."""
    if summary.tables_missing or summary.unexcluded_error_shas:
        return 1
    return 0
