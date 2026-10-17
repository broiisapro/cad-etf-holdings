"""Tests for the QA summary, report and exit code."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

from etfh.qa import exit_code, render, summarize
from etfh.schema import DDL_FILE_VERSIONS, DDL_HOLDINGS, DDL_HOLDINGS_CHANGES, DDL_QA_EVENTS

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
NOW = datetime(2026, 11, 3, 22, 0, tzinfo=UTC)
AS_OF = date(2026, 11, 2)

_DDL = (DDL_FILE_VERSIONS, DDL_HOLDINGS, DDL_QA_EVENTS, DDL_HOLDINGS_CHANGES)


def _quoted(ddl: str) -> str:
    # `check` is a reserved word in DuckDB, so `DDL_QA_EVENTS` needs it quoted
    # (a defect in etfh/schema.py reported for its owner to fix); quoting is
    # harmless once that lands because the column name is unchanged.
    return re.sub(r"\bcheck\b", '"check"', ddl)


def _connect(tmp_path: Path, *, tables: bool = True) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(tmp_path / "etfh.duckdb"))
    if tables:
        for ddl in _DDL:
            con.execute(_quoted(ddl))
    return con


def _file_version(
    con: duckdb.DuckDBPyConnection,
    sha: str,
    *,
    fund: str = "SYNA",
    source: str = "syn_csv",
    as_of: date = AS_OF,
    status: str = "included",
) -> None:
    con.execute(
        "INSERT INTO file_versions VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
        [source, fund, as_of, NOW, sha, f"raw/{sha}.csv", status],
    )


def _qa_event(
    con: duckdb.DuckDBPyConnection,
    sha: str,
    *,
    fund: str = "SYNA",
    source: str = "syn_csv",
    as_of: date = AS_OF,
    check: str = "weight_sum",
    severity: str = "error",
    detail: str = "sum=0.8500 expected=1±0.005",
    excluded: bool = True,
) -> None:
    con.execute(
        "INSERT INTO qa_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [sha, source, fund, as_of, check, severity, detail, excluded],
    )


def _holding(con: duckdb.DuckDBPyConnection, index: int, *, security_id: str | None) -> None:
    con.execute(
        "INSERT INTO holdings VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "SYNF",
            date(2026, 9, 30),
            NOW,
            security_id,
            f"Synthetic Name {index:02d}",
            None,
            None,
            None,
            None,
            "equity",
            "Unknown",
            "Unknown",
            None,
            0.01,
            0.0,
            None,
            "syn_pdf",
            SHA_C,
        ],
    )


def test_clean_database_exits_zero(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    _file_version(con, SHA_A)
    summary = summarize(con)
    assert exit_code(summary) == 0
    assert summary.files_included == 1
    assert summary.files_excluded == 0
    text = render(summary)
    assert "files: 1 total (1 included, 0 excluded)" in text
    assert "warnings: 0" in text
    assert "unmatched securities: 0" in text


def test_two_excluded_errored_files_exit_zero_and_are_listed(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    _file_version(con, SHA_B, fund="SYND", source="syn_xlsx", status="excluded")
    _file_version(con, SHA_A, fund="SYNC", source="syn_csv", status="excluded")
    _qa_event(con, SHA_B, fund="SYND", source="syn_xlsx", check="duplicate_security")
    _qa_event(
        con,
        SHA_A,
        fund="SYNC",
        check="weight_sum",
        detail="sum=0.8500 expected=1±0.005 (asset_class=equity, delta=0.1500)",
    )
    summary = summarize(con)
    assert exit_code(summary) == 0
    assert summary.files_excluded == 2
    assert summary.unexcluded_error_shas == ()
    text = render(summary)
    assert "files: 2 total (0 included, 2 excluded)" in text
    assert "excluded files:" in text
    assert "SYNC as_of=2026-11-02 source=syn_csv" in text
    assert "SYND as_of=2026-11-02 source=syn_xlsx" in text
    assert "error weight_sum: sum=0.8500 expected=1±0.005" in text
    assert "error duplicate_security:" in text
    assert text.index("SYNC as_of") < text.index("SYND as_of")


def test_errored_file_marked_included_exits_one(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    _file_version(con, SHA_A, status="included")
    _qa_event(con, SHA_A)
    summary = summarize(con)
    assert summary.unexcluded_error_shas == (SHA_A,)
    assert exit_code(summary) == 1


def test_errored_file_without_a_version_row_exits_one(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    _file_version(con, SHA_A)
    _qa_event(con, SHA_B)
    summary = summarize(con)
    assert exit_code(summary) == 1
    assert SHA_B in summary.unexcluded_error_shas


def test_missing_tables_exit_one(tmp_path: Path) -> None:
    con = _connect(tmp_path, tables=False)
    summary = summarize(con)
    assert summary.tables_missing
    assert exit_code(summary) == 1
    assert "missing tables" in render(summary)


def test_warnings_counted_without_excluding(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    _file_version(con, SHA_A)
    _qa_event(con, SHA_A, check="negative_weight", severity="warning", excluded=False)
    _qa_event(con, SHA_A, check="declared_unit", severity="info", excluded=False)
    summary = summarize(con)
    assert summary.warnings == 1
    assert exit_code(summary) == 0
    assert "warnings: 1" in render(summary)


def test_unmatched_securities_counted_and_capped_at_ten(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    _file_version(con, SHA_A)
    for index in range(12):
        _holding(con, index, security_id=None)
    _holding(con, 99, security_id="ISIN:CA600000AA15")
    summary = summarize(con)
    assert summary.unmatched_rows == 12
    assert len(summary.unmatched_examples) == 10
    assert summary.unmatched_examples[0] == "SYNF Synthetic Name 00"
    text = render(summary)
    assert "unmatched securities: 12 row(s), showing up to 10:" in text
    assert text.count("\n  SYNF ") == 10
    assert exit_code(summary) == 0


def test_render_is_deterministic(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    _file_version(con, SHA_A, fund="SYNC", status="excluded")
    _qa_event(con, SHA_A, fund="SYNC", check="weight_sum")
    first = render(summarize(con))
    second = render(summarize(con))
    assert first == second
