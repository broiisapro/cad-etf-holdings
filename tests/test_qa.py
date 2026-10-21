"""Tests for the QA summary, report and exit code.

Every database is hand-made here (the frozen DDLs plus explicit rows), so the
QA rules are tested without depending on the build.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest

from etfh.qa import (
    QaSummary,
    UnmatchedQuality,
    exit_code,
    render,
    summarize,
    unmatched_shares,
)
from etfh.schema import DDL_FILE_VERSIONS, DDL_HOLDINGS, DDL_HOLDINGS_CHANGES, DDL_QA_EVENTS

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
NOW = datetime(2026, 11, 3, 22, 0, tzinfo=UTC)
AS_OF = date(2026, 11, 2)

_DDL = (DDL_FILE_VERSIONS, DDL_HOLDINGS, DDL_QA_EVENTS, DDL_HOLDINGS_CHANGES)
_SHARE_COLUMNS = [
    "fund_ticker",
    "as_of_date",
    "source_id",
    "unmatched_weight",
    "total_weight",
    "share",
]


def _connect(
    tmp_path: Path, *, tables: bool = True, sources: bool = False
) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(tmp_path / "etfh.duckdb"))
    if tables:
        for ddl in _DDL:
            con.execute(ddl)
        if sources:
            con.execute(
                "CREATE TABLE sources ("
                "source_id VARCHAR, issuer VARCHAR, kind VARCHAR, format VARCHAR)"
            )
    return con


def _file_version(
    con: duckdb.DuckDBPyConnection,
    sha: str,
    *,
    fund: str = "SYNA",
    source: str = "syn_csv",
    as_of: date = AS_OF,
    status: str = "included",
    version: int = 1,
) -> None:
    con.execute(
        "INSERT INTO file_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [source, fund, as_of, version, NOW, sha, f"raw/{sha}.csv", status],
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


def _holding(
    con: duckdb.DuckDBPyConnection,
    index: int,
    *,
    security_id: str | None,
    fund: str = "SYNA",
    source: str = "syn_csv",
    as_of: date = AS_OF,
    sha: str = SHA_A,
    version: int = 1,
    asset_class: str = "equity",
    weight: float = 0.01,
    name: str | None = None,
) -> None:
    con.execute(
        "INSERT INTO holdings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            fund,
            as_of,
            NOW,
            version,
            security_id,
            name or f"Synthetic Name {index:02d}",
            None,
            None,
            None,
            None,
            asset_class,
            "Unknown",
            "Unknown",
            None,
            weight,
            0.0,
            None,
            source,
            sha,
        ],
    )


def _source(
    con: duckdb.DuckDBPyConnection,
    source_id: str,
    *,
    issuer: str,
    kind: str,
    format: str = "csv",
) -> None:
    con.execute("INSERT INTO sources VALUES (?, ?, ?, ?)", [source_id, issuer, kind, format])


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
    assert "unmatched weight share (included files): none" in text
    assert "inconsistent:" not in text


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
    assert summary.inconsistent_files == ()
    text = render(summary)
    assert "files: 2 total (0 included, 2 excluded)" in text
    assert "excluded files:" in text
    assert "SYNC as_of=2026-11-02 source=syn_csv" in text
    assert "SYND as_of=2026-11-02 source=syn_xlsx" in text
    assert "error weight_sum: sum=0.8500 expected=1±0.005" in text
    assert "error duplicate_security:" in text
    assert text.index("SYNC as_of") < text.index("SYND as_of")


def test_errored_file_marked_included_is_inconsistent_and_exits_one(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    _file_version(con, SHA_A, status="included")
    _qa_event(con, SHA_A)
    summary = summarize(con)
    assert exit_code(summary) == 1
    assert len(summary.inconsistent_files) == 1
    entry = summary.inconsistent_files[0]
    assert entry.fund_ticker == "SYNA"
    assert entry.source_id == "syn_csv"
    assert entry.sha256 == SHA_A
    assert entry.reason == "errored but not excluded (status=included)"
    text = render(summary)
    assert "inconsistent:" in text
    assert f"sha={SHA_A}: errored but not excluded (status=included)" in text
    assert "    error weight_sum: sum=0.8500" in text


def test_errored_file_without_a_version_row_is_inconsistent_and_exits_one(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    _file_version(con, SHA_A)
    _qa_event(con, SHA_B, check="parse_error", detail="ValueError: bad table", excluded=True)
    summary = summarize(con)
    assert exit_code(summary) == 1
    assert summary.excluded_files == ()
    assert len(summary.inconsistent_files) == 1
    entry = summary.inconsistent_files[0]
    assert entry.sha256 == SHA_B
    assert entry.reason == "errored file has no file_versions row"
    text = render(summary)
    assert "inconsistent:" in text
    assert f"sha={SHA_B}: errored file has no file_versions row" in text
    assert "    error parse_error: ValueError: bad table" in text


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


def test_unmatched_securities_exclude_cash_fx_and_derivative_rows(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    _file_version(con, SHA_A)
    _holding(con, 0, security_id=None, name="Alpha Corp.")
    _holding(con, 1, security_id=None, name="Beta Corp.")
    _holding(con, 2, security_id=None, asset_class="cash", name="Cash")
    _holding(con, 3, security_id=None, asset_class="derivative", name="Call option")
    _holding(con, 4, security_id=None, asset_class="fx", name="FX forward")
    _holding(con, 5, security_id="ISIN:CA600000AA15", name="Gamma Corp.")
    summary = summarize(con)
    assert summary.unmatched_rows == 2
    assert summary.unmatched_examples == ("SYNA Alpha Corp.", "SYNA Beta Corp.")
    text = render(summary)
    assert "unmatched securities: 2 row(s), showing up to 10:" in text
    assert "  SYNA Cash" not in text
    assert "  SYNA Call option" not in text


def test_unmatched_examples_capped_at_ten(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    _file_version(con, SHA_A)
    for index in range(12):
        _holding(con, index, security_id=None)
    _holding(con, 99, security_id="ISIN:CA600000AA15")
    _holding(con, 100, security_id=None, asset_class="cash")
    summary = summarize(con)
    assert summary.unmatched_rows == 12
    assert len(summary.unmatched_examples) == 10
    assert summary.unmatched_examples[0] == "SYNA Synthetic Name 00"
    text = render(summary)
    assert "unmatched securities: 12 row(s), showing up to 10:" in text
    assert text.count("\n  SYNA ") == 10


def test_unmatched_shares_per_included_file_version(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    _file_version(con, SHA_A, fund="SYNA", source="syn_csv")
    _file_version(con, SHA_B, fund="SYNB", source="syn_xlsx")
    _file_version(con, SHA_C, fund="SYNC", source="syn_csv", status="excluded")
    _holding(con, 0, security_id=None, weight=0.4)
    _holding(con, 1, security_id="ISIN:CA600000AA15", weight=0.5)
    _holding(con, 2, security_id=None, asset_class="cash", weight=0.1)
    _holding(con, 3, security_id="ISIN:CA600000AA15", fund="SYNB", source="syn_xlsx", sha=SHA_B)
    _holding(
        con,
        4,
        security_id=None,
        fund="SYNC",
        source="syn_csv",
        sha=SHA_C,
        weight=1.0,
    )
    frame = unmatched_shares(con)
    assert frame.columns == _SHARE_COLUMNS
    rows = list(frame.iter_rows(named=True))
    assert len(rows) == 2
    assert rows[0]["fund_ticker"] == "SYNA"
    assert rows[0]["source_id"] == "syn_csv"
    assert rows[0]["unmatched_weight"] == pytest.approx(0.4)
    assert rows[0]["total_weight"] == pytest.approx(0.9)
    assert rows[0]["share"] == pytest.approx(0.4 / 0.9)
    assert rows[1]["fund_ticker"] == "SYNB"
    assert rows[1]["unmatched_weight"] == 0.0
    assert rows[1]["total_weight"] == pytest.approx(0.01)
    assert rows[1]["share"] == 0.0


def test_unmatched_shares_empty_tables(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    frame = unmatched_shares(con)
    assert frame.columns == _SHARE_COLUMNS
    assert frame.is_empty()


def test_share_is_zero_when_only_non_security_rows_exist(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    _file_version(con, SHA_A)
    _holding(con, 0, security_id=None, asset_class="cash", weight=0.6)
    _holding(con, 1, security_id=None, asset_class="fx", weight=-0.4)
    _holding(con, 2, security_id=None, asset_class="derivative", weight=0.5)
    frame = unmatched_shares(con)
    row = frame.row(0, named=True)
    assert row["unmatched_weight"] == 0.0
    assert row["total_weight"] == 0.0
    assert row["share"] == 0.0


def _unmatched_version(
    con: duckdb.DuckDBPyConnection,
    index: int,
    sha: str,
    *,
    fund: str,
    source: str,
    unmatched: float,
    matched: float,
) -> None:
    _file_version(con, sha, fund=fund, source=source)
    _holding(con, index, security_id=None, fund=fund, source=source, sha=sha, weight=unmatched)
    _holding(
        con,
        index + 1,
        security_id="ISIN:CA600000AA15",
        fund=fund,
        source=source,
        sha=sha,
        weight=matched,
    )


def _quality(summary: QaSummary, kind: str) -> UnmatchedQuality:
    return next(quality for quality in summary.unmatched_quality if quality.kind == kind)


def test_daily_and_quarterly_quality_are_separate(tmp_path: Path) -> None:
    con = _connect(tmp_path, sources=True)
    _source(con, "evolve_daily", issuer="evolve", kind="daily_holdings")
    _source(con, "bmo_daily", issuer="bmo", kind="daily_holdings")
    _source(con, "vanguard_qpd", issuer="vanguard", kind="quarterly_disclosure", format="pdf")
    _unmatched_version(
        con, 0, SHA_A, fund="ESPX", source="evolve_daily", unmatched=0.5, matched=0.5
    )
    _unmatched_version(con, 2, SHA_B, fund="ZEM", source="bmo_daily", unmatched=0.2, matched=0.8)
    _unmatched_version(con, 4, SHA_C, fund="XEQT", source="bmo_daily", unmatched=0.1, matched=0.9)
    _unmatched_version(con, 6, SHA_D, fund="VFV", source="vanguard_qpd", unmatched=0.9, matched=0.1)
    _unmatched_version(
        con, 8, "e" * 64, fund="VIU", source="vanguard_qpd", unmatched=0.3, matched=0.7
    )
    summary = summarize(con)
    daily = _quality(summary, "daily")
    quarterly = _quality(summary, "quarterly")
    assert daily.issuer_medians == (
        ("bmo", pytest.approx(0.15)),
        ("evolve", pytest.approx(0.5)),
    )
    assert [row.fund_ticker for row in daily.worst_funds] == ["ESPX", "ZEM", "XEQT"]
    assert quarterly.issuer_medians == (("vanguard", pytest.approx(0.6)),)
    assert [row.fund_ticker for row in quarterly.worst_funds] == ["VFV", "VIU"]
    text = render(summary)
    assert "unmatched weight share (included files):" in text
    assert "  daily files:" in text
    assert "  quarterly files:" in text
    assert "      bmo 15.00%" in text
    assert "      evolve 50.00%" in text
    assert "      vanguard 60.00%" in text
    assert "    worst funds:" in text
    assert "      ESPX as_of=2026-11-02 source=evolve_daily share=50.00%" in text
    assert "      XEQT as_of=2026-11-02 source=bmo_daily share=10.00%" in text
    assert "      VFV as_of=2026-11-02 source=vanguard_qpd share=90.00%" in text
    # The daily section must not absorb the quarterly median or vice versa.
    daily_section = text.split("  daily files:", 1)[1].split("  quarterly files:", 1)[0]
    assert "vanguard" not in daily_section
    assert "bmo" not in text.split("  quarterly files:", 1)[1]


def test_worst_funds_only_lists_positive_shares(tmp_path: Path) -> None:
    con = _connect(tmp_path, sources=True)
    _source(con, "evolve_daily", issuer="evolve", kind="daily_holdings")
    _source(con, "vanguard_qpd", issuer="vanguard", kind="quarterly_disclosure", format="pdf")
    _unmatched_version(
        con, 0, SHA_A, fund="ZERO1", source="evolve_daily", unmatched=0.0, matched=0.5
    )
    _unmatched_version(
        con, 2, SHA_B, fund="ZERO2", source="evolve_daily", unmatched=0.0, matched=0.5
    )
    _unmatched_version(
        con, 4, SHA_C, fund="HAS1", source="evolve_daily", unmatched=0.4, matched=0.6
    )
    _unmatched_version(con, 6, SHA_D, fund="VFV", source="vanguard_qpd", unmatched=0.9, matched=0.1)
    summary = summarize(con)
    assert [row.fund_ticker for row in _quality(summary, "daily").worst_funds] == ["HAS1"]
    assert [row.fund_ticker for row in _quality(summary, "quarterly").worst_funds] == ["VFV"]
    text = render(summary)
    assert "ZERO1 as_of=" not in text
    assert "ZERO2 as_of=" not in text
    assert "    worst funds: none" not in text


def test_synthetic_quarterly_source_is_classified_quarterly(tmp_path: Path) -> None:
    con = _connect(tmp_path, sources=True)
    _source(con, "syn_pdf", issuer="synthetic", kind="synthetic", format="pdf")
    _source(con, "syn_csv", issuer="synthetic", kind="synthetic", format="csv")
    _unmatched_version(con, 0, SHA_A, fund="SYNF", source="syn_pdf", unmatched=0.8, matched=0.2)
    _unmatched_version(con, 2, SHA_B, fund="SYNA", source="syn_csv", unmatched=0.1, matched=0.9)
    summary = summarize(con)
    assert [row.fund_ticker for row in _quality(summary, "daily").worst_funds] == ["SYNA"]
    assert [row.fund_ticker for row in _quality(summary, "quarterly").worst_funds] == ["SYNF"]


def test_worst_funds_take_the_worst_file_per_fund_and_cap_at_ten(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    for index in range(12):
        fund = f"F{index:02d}"
        sha = f"{index:064x}"
        _unmatched_version(
            con,
            index * 2,
            sha,
            fund=fund,
            source="syn_csv",
            unmatched=0.01 * (index + 1),
            matched=0.5,
        )
    _file_version(con, SHA_D, fund="F00", source="syn_csv", version=2)
    _holding(
        con, 30, security_id=None, fund="F00", source="syn_csv", sha=SHA_D, version=2, weight=0.9
    )
    _holding(
        con,
        31,
        security_id="ISIN:CA600000AA15",
        fund="F00",
        source="syn_csv",
        sha=SHA_D,
        version=2,
        weight=0.1,
    )
    summary = summarize(con)
    worst_funds = _quality(summary, "daily").worst_funds
    assert len(worst_funds) == 10
    assert worst_funds[0].fund_ticker == "F00"
    assert worst_funds[0].share == pytest.approx(0.9)
    assert [row.fund_ticker for row in worst_funds[1:]] == [
        f"F{index:02d}" for index in range(11, 2, -1)
    ]
    assert _quality(summary, "quarterly").worst_funds == ()


def test_max_unmatched_share_checks_only_daily_files_and_exits_three(tmp_path: Path) -> None:
    con = _connect(tmp_path, sources=True)
    _source(con, "evolve_daily", issuer="evolve", kind="daily_holdings")
    _source(con, "vanguard_qpd", issuer="vanguard", kind="quarterly_disclosure", format="pdf")
    _unmatched_version(
        con, 0, SHA_A, fund="ESPX", source="evolve_daily", unmatched=0.5, matched=0.5
    )
    _unmatched_version(con, 2, SHA_B, fund="VFV", source="vanguard_qpd", unmatched=0.9, matched=0.1)
    summary = summarize(con, max_unmatched_share=0.1)
    assert exit_code(summary) == 3
    assert [row.fund_ticker for row in summary.unmatched_over_threshold] == ["ESPX"]
    assert summary.max_unmatched_share == 0.1
    text = render(summary)
    assert "unmatched threshold 10.00% exceeded by 1 included daily file(s):" in text
    assert "  ESPX as_of=2026-11-02 source=evolve_daily share=50.00%" in text
    # The quarterly file is reported in its own quality section, never as a
    # threshold breach.
    threshold_section = text.split("unmatched threshold", 1)[1]
    assert "VFV" not in threshold_section
    assert "      VFV as_of=2026-11-02 source=vanguard_qpd share=90.00%" in text


def test_max_unmatched_share_not_checked_by_default(tmp_path: Path) -> None:
    con = _connect(tmp_path, sources=True)
    _source(con, "evolve_daily", issuer="evolve", kind="daily_holdings")
    _unmatched_version(
        con, 0, SHA_A, fund="ESPX", source="evolve_daily", unmatched=0.9, matched=0.1
    )
    summary = summarize(con)
    assert summary.unmatched_over_threshold == ()
    assert exit_code(summary) == 0
    assert "unmatched threshold" not in render(summary)


def test_max_unmatched_share_below_the_threshold_passes(tmp_path: Path) -> None:
    con = _connect(tmp_path, sources=True)
    _source(con, "evolve_daily", issuer="evolve", kind="daily_holdings")
    _unmatched_version(
        con, 0, SHA_A, fund="ESPX", source="evolve_daily", unmatched=0.1, matched=0.9
    )
    summary = summarize(con, max_unmatched_share=0.2)
    assert summary.unmatched_over_threshold == ()
    assert exit_code(summary) == 0
    assert "unmatched threshold 20.00%: no included daily file exceeds it" in render(summary)


def test_a_quarterly_file_never_trips_the_threshold(tmp_path: Path) -> None:
    con = _connect(tmp_path, sources=True)
    _source(con, "vanguard_qpd", issuer="vanguard", kind="quarterly_disclosure", format="pdf")
    _unmatched_version(con, 0, SHA_A, fund="VFV", source="vanguard_qpd", unmatched=0.9, matched=0.1)
    summary = summarize(con, max_unmatched_share=0.1)
    assert summary.unmatched_over_threshold == ()
    assert exit_code(summary) == 0
    text = render(summary)
    assert "unmatched threshold 10.00%: no included daily file exceeds it" in text
    assert "unmatched threshold 10.00% exceeded" not in text


def test_max_unmatched_share_without_a_sources_table_checks_every_file(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    _unmatched_version(con, 0, SHA_A, fund="ESPX", source="syn_csv", unmatched=0.9, matched=0.1)
    summary = summarize(con, max_unmatched_share=0.5)
    assert exit_code(summary) == 3
    assert [row.fund_ticker for row in summary.unmatched_over_threshold] == ["ESPX"]


def test_inconsistent_database_takes_precedence_over_the_threshold(tmp_path: Path) -> None:
    con = _connect(tmp_path, sources=True)
    _source(con, "evolve_daily", issuer="evolve", kind="daily_holdings")
    _unmatched_version(
        con, 0, SHA_A, fund="ESPX", source="evolve_daily", unmatched=0.9, matched=0.1
    )
    _qa_event(con, SHA_A, fund="ESPX", source="evolve_daily", excluded=False)
    summary = summarize(con, max_unmatched_share=0.1)
    assert summary.unmatched_over_threshold
    assert exit_code(summary) == 1


def test_invalid_threshold_rejected(tmp_path: Path) -> None:
    con = _connect(tmp_path)
    with pytest.raises(ValueError, match="between 0 and 1"):
        summarize(con, max_unmatched_share=1.5)
    with pytest.raises(ValueError, match="between 0 and 1"):
        summarize(con, max_unmatched_share=-0.1)


def _report_with_insertion_order(root: Path, *, reverse: bool) -> str:
    """Render the same database content with rows inserted in either order."""
    root.mkdir(parents=True, exist_ok=True)
    con = _connect(root, sources=True)
    sources = [
        ("evolve_daily", "evolve", "daily_holdings", "xlsx"),
        ("bmo_daily", "bmo", "daily_holdings", "xlsx"),
        ("vanguard_qpd", "vanguard", "quarterly_disclosure", "pdf"),
    ]
    for source_id, issuer, kind, source_format in reversed(sources) if reverse else sources:
        _source(con, source_id, issuer=issuer, kind=kind, format=source_format)
    versions = [
        (SHA_A, "ESPX", "evolve_daily", 0.5, 0.5),
        (SHA_B, "ZEM", "bmo_daily", 0.2, 0.8),
        (SHA_C, "XEQT", "bmo_daily", 0.1, 0.9),
        (SHA_D, "ZERO", "bmo_daily", 0.0, 0.5),
        ("e" * 64, "VFV", "vanguard_qpd", 0.9, 0.1),
        ("f" * 64, "VIU", "vanguard_qpd", 0.3, 0.7),
    ]
    base_index = {fund: index * 2 for index, (_sha, fund, *_rest) in enumerate(versions)}
    for sha, fund, source_id, unmatched, matched in reversed(versions) if reverse else versions:
        _unmatched_version(
            con,
            base_index[fund],
            sha,
            fund=fund,
            source=source_id,
            unmatched=unmatched,
            matched=matched,
        )
    _file_version(con, "9" * 64, fund="SYNC", source="evolve_daily", status="excluded")
    _qa_event(con, "9" * 64, fund="SYNC", source="evolve_daily", check="weight_sum")
    text = render(summarize(con, max_unmatched_share=0.1))
    con.close()
    return text


def test_render_does_not_depend_on_insertion_order(tmp_path: Path) -> None:
    forward = _report_with_insertion_order(tmp_path / "forward", reverse=False)
    backward = _report_with_insertion_order(tmp_path / "backward", reverse=True)
    assert forward == backward
    assert "  daily files:" in forward
    assert "  quarterly files:" in forward
    assert forward.count("    worst funds:") == 2
