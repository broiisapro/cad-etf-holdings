"""QA on real-shaped look-alike data.

Builds a raw store in ``tmp_path`` from the committed look-alike issuer files
(Purpose, Evolve and BMO dailies plus the QPD look-alikes) with
:class:`~etfh.storage.RawStore`, runs :func:`etfh.build.build` and checks the
QA summary: no clean file is excluded, every deliberately broken copy is, the
unmatched weight shares are computed, and the exit codes are 0/1/3 as
specified. Nothing here touches the network or a real issuer file.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import polars as pl
import pytest

from etfh.build import BuildResult, build
from etfh.qa import exit_code, summarize, unmatched_shares
from etfh.storage import RawStore
from tests._qa_live_helpers import (
    LookAlike,
    broken_duplicate_security,
    broken_future_as_of,
    broken_qpd_stated_total,
    broken_unit_contradiction,
    broken_weight_sum,
    clean_look_alikes,
    duplicate_lines_copy,
    write_look_alikes,
)

_SHARE_COLUMNS = [
    "fund_ticker",
    "as_of_date",
    "source_id",
    "unmatched_weight",
    "total_weight",
    "share",
]


def _build(tmp_path: Path, items: list[LookAlike]) -> BuildResult:
    store = RawStore(tmp_path / "raw")
    write_look_alikes(store, items)
    return build(tmp_path / "raw", tmp_path / "build")


def _connect(db_path: Path, *, read_only: bool = True) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path), read_only=read_only)


def _error_checks_by_fund(db_path: Path) -> dict[str, set[str]]:
    con = _connect(db_path)
    try:
        rows = con.execute(
            'SELECT q.fund_ticker, q."check" FROM qa_events q '
            "JOIN file_versions v USING (raw_file_sha256) "
            "WHERE q.severity = 'error' AND v.status = 'excluded'"
        ).fetchall()
    finally:
        con.close()
    checks: dict[str, set[str]] = {}
    for fund, check in rows:
        checks.setdefault(str(fund), set()).add(str(check))
    return checks


def _included_funds(db_path: Path) -> set[str]:
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT DISTINCT fund_ticker FROM file_versions WHERE status = 'included'"
        ).fetchall()
    finally:
        con.close()
    return {str(row[0]) for row in rows}


@pytest.fixture(scope="module")
def clean_result(tmp_path_factory: pytest.TempPathFactory) -> BuildResult:
    tmp_path = tmp_path_factory.mktemp("qa-live-clean")
    return _build(tmp_path, clean_look_alikes(tmp_path))


def test_clean_look_alikes_build_without_exclusions(clean_result: BuildResult) -> None:
    assert clean_result.n_excluded_files == 0
    assert clean_result.n_included_files == 12
    con = _connect(clean_result.db_path)
    try:
        summary = summarize(con)
    finally:
        con.close()
    assert exit_code(summary) == 0
    assert summary.files_excluded == 0
    assert summary.inconsistent_files == ()
    sources = _included_funds(clean_result.db_path)
    assert {"PDIV", "PDF", "PID", "RDE", "ESPX", "ZSP", "ZAG", "ZEM", "CYBR", "VFV"} <= sources


def test_unmatched_shares_are_computed(clean_result: BuildResult) -> None:
    con = _connect(clean_result.db_path)
    try:
        frame = unmatched_shares(con)
    finally:
        con.close()
    assert frame.columns == _SHARE_COLUMNS
    assert not frame.is_empty()
    assert frame["fund_ticker"].n_unique() >= 4
    assert all(0.0 <= share <= 1.0 for share in frame["share"].to_list())
    assert all(
        unmatched <= total
        for unmatched, total in zip(
            frame["unmatched_weight"].to_list(), frame["total_weight"].to_list(), strict=True
        )
    )
    bmo = frame.filter((pl.col("fund_ticker") == "ZSP") & (pl.col("source_id") == "bmo_daily"))
    assert bmo.height == 1
    assert bmo["share"][0] == 0.0
    fixed_income = frame.filter(
        (pl.col("fund_ticker") == "ZAG") & (pl.col("source_id") == "bmo_daily")
    )
    assert fixed_income.height == 1
    assert fixed_income["share"][0] == 0.0
    purpose = frame.filter(
        (pl.col("fund_ticker") == "PDIV") & (pl.col("source_id") == "purpose_daily")
    )
    assert purpose.height == 1
    assert purpose["share"][0] == 1.0


def test_broken_copies_are_excluded_one_per_check(tmp_path: Path) -> None:
    items = clean_look_alikes(tmp_path)
    items.extend(
        [
            broken_weight_sum(tmp_path),
            broken_unit_contradiction(tmp_path),
            broken_future_as_of(tmp_path),
            broken_duplicate_security(tmp_path),
            broken_qpd_stated_total(tmp_path),
        ]
    )
    result = _build(tmp_path, items)
    assert result.n_excluded_files == 5
    checks = _error_checks_by_fund(result.db_path)
    assert checks["ZQQ"] == {"weight_sum"}
    assert "unit_contradiction" in checks["BNC"]
    assert "weight_sum" in checks["BNC"]
    assert checks["PHR"] == {"future_as_of"}
    assert checks["ZCN"] == {"duplicate_security"}
    assert checks["ZLB"] == {"qpd_stated_total"}
    assert {"PDIV", "PDF", "PID", "RDE", "ESPX", "ZSP", "ZAG", "ZEM", "CYBR", "VFV"} <= (
        _included_funds(result.db_path)
    )
    con = _connect(result.db_path)
    try:
        summary = summarize(con)
    finally:
        con.close()
    assert exit_code(summary) == 0
    assert {entry.fund_ticker for entry in summary.excluded_files} == {
        "ZQQ",
        "BNC",
        "PHR",
        "ZCN",
        "ZLB",
    }


def test_duplicate_lines_info_keeps_the_file(tmp_path: Path) -> None:
    items = clean_look_alikes(tmp_path)
    items.append(duplicate_lines_copy(tmp_path))
    result = _build(tmp_path, items)
    assert result.n_excluded_files == 0
    assert "ZDV" in _included_funds(result.db_path)
    con = _connect(result.db_path)
    try:
        rows = con.execute(
            "SELECT severity, \"check\", detail FROM qa_events WHERE fund_ticker = 'ZDV'"
        ).fetchall()
        summary = summarize(con)
    finally:
        con.close()
    assert exit_code(summary) == 0
    duplicate_lines = [row for row in rows if row[1] == "duplicate_lines"]
    assert len(duplicate_lines) == 1
    assert duplicate_lines[0][0] == "info"
    assert "same identifier form (CASH), rows kept separate" in duplicate_lines[0][2]


def test_exit_code_three_when_a_daily_file_exceeds_the_threshold(
    clean_result: BuildResult,
) -> None:
    con = _connect(clean_result.db_path)
    try:
        strict = summarize(con, max_unmatched_share=0.5)
        loose = summarize(con, max_unmatched_share=1.0)
    finally:
        con.close()
    assert exit_code(strict) == 3
    assert strict.unmatched_over_threshold
    assert all(row.share > 0.5 for row in strict.unmatched_over_threshold)
    assert exit_code(loose) == 0
    assert loose.unmatched_over_threshold == ()


def test_inconsistent_database_exits_one(tmp_path: Path) -> None:
    items = clean_look_alikes(tmp_path)
    items.append(broken_future_as_of(tmp_path))
    result = _build(tmp_path, items)
    con = _connect(result.db_path, read_only=False)
    try:
        con.execute("DELETE FROM file_versions WHERE fund_ticker = 'PHR'")
        summary = summarize(con)
    finally:
        con.close()
    assert exit_code(summary) == 1
    assert len(summary.inconsistent_files) == 1
    entry = summary.inconsistent_files[0]
    assert entry.fund_ticker == "PHR"
    assert entry.reason == "errored file has no file_versions row"
    assert entry.findings
