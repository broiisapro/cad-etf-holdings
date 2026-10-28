"""CLI tests for the QA report formats and the point-in-time summary.

``tests/test_cli.py`` keeps the historical text-output tests; this file covers
the wave-08 contract: ``--format text|markdown|json`` and a fully
point-in-time ``--asof`` report. The markdown output is publish-safe (D77/D79):
counts, shares, fund/issuer tickers and check names only, never a security
name, id, weight value or file path.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from etfh.build import BuildResult, build
from etfh.cli import app
from etfh.qa import QaSummary, exit_code, render, summarize
from etfh.schema import (
    DDL_FILE_VERSIONS,
    DDL_HOLDINGS,
    DDL_HOLDINGS_CHANGES,
    DDL_QA_EVENTS,
)

RAW_ROOT = Path(__file__).resolve().parent / "fixtures" / "raw_synthetic"

runner = CliRunner()
_INSERT_HOLDING = (
    "INSERT INTO holdings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


@pytest.fixture(scope="module")
def synthetic_build(tmp_path_factory: pytest.TempPathFactory) -> BuildResult:
    return build(RAW_ROOT, tmp_path_factory.mktemp("cli-qa-formats"))


def _run(build_result: BuildResult, *args: str) -> tuple[object, str]:
    result = runner.invoke(app, ["qa", "--db", str(build_result.db_path), *args])
    return result, result.output


def test_default_text_output_is_unchanged(synthetic_build: BuildResult) -> None:
    con = duckdb.connect(str(synthetic_build.db_path), read_only=True)
    try:
        expected = render(summarize(con))
    finally:
        con.close()
    result, output = _run(synthetic_build)
    assert result.exit_code == 0, output  # type: ignore[attr-defined]
    assert output == expected + "\n"
    assert output.startswith("QA report\n")


def test_markdown_starts_and_ends_with_the_contract_lines(
    synthetic_build: BuildResult,
) -> None:
    result, output = _run(synthetic_build, "--format", "markdown")
    assert result.exit_code == 0, output  # type: ignore[attr-defined]
    assert output.splitlines()[0] == "## etfh qa"
    assert output.splitlines()[-1] == "**Exit status:** 0"
    assert "**Files:** 15 total (13 included, 2 excluded)" in output
    assert "**Unmatched securities:** 24 row(s)" in output
    assert "weight_sum (error)" in output
    assert "duplicate_security (error)" in output


def test_markdown_contains_no_security_names_ids_weights_or_paths(
    synthetic_build: BuildResult,
) -> None:
    con = duckdb.connect(str(synthetic_build.db_path), read_only=True)
    try:
        names = [
            str(row[0])
            for row in con.execute("SELECT DISTINCT security_name FROM holdings").fetchall()
        ]
    finally:
        con.close()
    assert names
    _, output = _run(synthetic_build, "--format", "markdown")
    for name in names:
        assert name not in output, name
    for forbidden in ("ISIN:", "CUSIP:", "TKR:", "sum=", "weight=", "raw/", "tests/fixtures"):
        assert forbidden not in output, forbidden
    for extension in (".csv", ".xlsx", ".json", ".pdf"):
        assert extension not in output, extension


def test_markdown_asof_has_no_text_preamble(synthetic_build: BuildResult) -> None:
    result, output = _run(synthetic_build, "--format", "markdown", "--asof", "2026-10-31")
    assert result.exit_code == 0, output  # type: ignore[attr-defined]
    assert output.splitlines()[0] == "## etfh qa"
    assert "**As of:** 2026-10-31" in output
    assert "as of 2026-10-31:" not in output
    assert output.splitlines()[-1] == "**Exit status:** 0"


def test_json_mirrors_the_numbers_for_machines(synthetic_build: BuildResult) -> None:
    result, output = _run(synthetic_build, "--format", "json")
    assert result.exit_code == 0, output  # type: ignore[attr-defined]
    payload = json.loads(output)
    assert payload["asof"] is None
    assert payload["files"] == {"total": 15, "included": 13, "excluded": 2}
    assert payload["warnings"] == 0
    assert payload["unmatched_rows"] == 24
    assert payload["non_security_lines"] == {
        "balancing": {"rows": 0, "weight_sum": 0.0},
        "physical_commodity": {"rows": 0, "weight_sum": 0.0},
    }
    assert payload["unmatched_shares"]["daily"]["worst_funds"] == []
    quarterly = payload["unmatched_shares"]["quarterly"]["worst_funds"]
    assert [row["fund_ticker"] for row in quarterly] == ["SYNF"]
    assert payload["fund_metrics"]["files_included"] == 0
    assert payload["exit_status"] == 0


def test_json_asof_carries_the_date_and_point_in_time_numbers(
    synthetic_build: BuildResult,
) -> None:
    _, output = _run(synthetic_build, "--format", "json", "--asof", "2026-10-31")
    payload = json.loads(output)
    assert payload["asof"] == "2026-10-31"
    assert payload["files"]["total"] < 15
    assert payload["exit_status"] == 0


def test_unknown_format_is_a_usage_error(synthetic_build: BuildResult) -> None:
    result = runner.invoke(
        app,
        ["qa", "--db", str(synthetic_build.db_path), "--format", "xml"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2, result.output
    assert "--format must be one of text, markdown, json" in result.output


def _threshold_database(tmp_path: Path) -> Path:
    """One included daily file whose unmatched share is 90 %."""
    db = tmp_path / "threshold.duckdb"
    con = duckdb.connect(str(db))
    try:
        for ddl in (DDL_FILE_VERSIONS, DDL_HOLDINGS, DDL_QA_EVENTS, DDL_HOLDINGS_CHANGES):
            con.execute(ddl)
        con.execute(
            "CREATE TABLE sources (source_id VARCHAR, issuer VARCHAR, kind VARCHAR, format VARCHAR)"
        )
        con.execute("INSERT INTO sources VALUES ('syn_csv', 'synthetic', 'daily_holdings', 'csv')")
        con.execute(
            "INSERT INTO file_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "syn_csv",
                "SYNA",
                date(2026, 11, 2),
                1,
                datetime(2026, 11, 3, 22, tzinfo=UTC),
                "a" * 64,
                "raw/a.csv",
                "included",
            ],
        )
        for index, (security_id, weight) in enumerate(((None, 0.9), ("ISIN:CA600000AA15", 0.1))):
            con.execute(
                _INSERT_HOLDING,
                [
                    "SYNA",
                    date(2026, 11, 2),
                    datetime(2026, 11, 3, 22, tzinfo=UTC),
                    1,
                    security_id,
                    f"Synthetic Name {index}",
                    None,
                    None,
                    None,
                    None,
                    "equity",
                    "Unknown",
                    "Unknown",
                    None,
                    weight,
                    0.0,
                    None,
                    "syn_csv",
                    "a" * 64,
                ],
            )
    finally:
        con.close()
    return db


def test_markdown_reports_the_same_exit_status(tmp_path: Path) -> None:
    db = _threshold_database(tmp_path)
    result = runner.invoke(
        app,
        [
            "qa",
            "--db",
            str(db),
            "--max-unmatched-share",
            "0.5",
            "--format",
            "markdown",
        ],
    )
    assert result.exit_code == 3, result.output
    assert result.output.splitlines()[-1] == "**Exit status:** 3"
    assert "SYNA" in result.output
    assert "90.00%" in result.output
    assert "Synthetic Name" not in result.output


def test_json_reports_the_same_exit_status(tmp_path: Path) -> None:
    db = _threshold_database(tmp_path)
    result = runner.invoke(
        app, ["qa", "--db", str(db), "--max-unmatched-share", "0.5", "--format", "json"]
    )
    assert result.exit_code == 3, result.output
    payload = json.loads(result.output)
    assert payload["exit_status"] == 3
    assert [row["fund_ticker"] for row in payload["unmatched_over_threshold"]] == ["SYNA"]


def test_asof_text_summary_is_point_in_time(tmp_path: Path) -> None:
    """A republished version known after D cannot change the report for D."""
    db = tmp_path / "asof.duckdb"
    con = duckdb.connect(str(db))
    try:
        for ddl in (DDL_FILE_VERSIONS, DDL_HOLDINGS, DDL_QA_EVENTS, DDL_HOLDINGS_CHANGES):
            con.execute(ddl)
        con.execute(
            "CREATE TABLE sources (source_id VARCHAR, issuer VARCHAR, kind VARCHAR, format VARCHAR)"
        )
        con.execute("INSERT INTO sources VALUES ('syn_csv', 'synthetic', 'daily_holdings', 'csv')")
        now = datetime(2026, 11, 3, 22, tzinfo=UTC)
        con.execute(
            "INSERT INTO file_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ["syn_csv", "SYNA", date(2026, 11, 2), 1, now, "a" * 64, "raw/a.csv", "included"],
        )
        con.execute(
            "INSERT INTO file_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "syn_csv",
                "SYNA",
                date(2026, 11, 2),
                2,
                datetime(2026, 11, 6, 22, tzinfo=UTC),
                "b" * 64,
                "raw/b.csv",
                "excluded",
            ],
        )
        con.execute(
            "INSERT INTO qa_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "b" * 64,
                "syn_csv",
                "SYNA",
                date(2026, 11, 2),
                "weight_sum",
                "error",
                "sum=0.8500 expected=1±0.005",
                True,
            ],
        )
        for index, (security_id, weight, sha) in enumerate(
            ((None, 0.4, "a" * 64), ("ISIN:CA600000AA15", 0.5, "a" * 64))
        ):
            con.execute(
                _INSERT_HOLDING,
                [
                    "SYNA",
                    date(2026, 11, 2),
                    now,
                    1,
                    security_id,
                    f"Synthetic Name {index}",
                    None,
                    None,
                    None,
                    None,
                    "equity",
                    "Unknown",
                    "Unknown",
                    None,
                    weight,
                    0.0,
                    None,
                    "syn_csv",
                    sha,
                ],
            )
        summary = summarize(con)
        assert summary.files_excluded == 1
        at = summarize(con, asof=date(2026, 11, 4))
        assert at.files_excluded == 0
        assert at.excluded_files == ()
        assert exit_code(at) == 0
    finally:
        con.close()

    result = runner.invoke(app, ["qa", "--db", str(db), "--asof", "2026-11-04"])
    assert result.exit_code == 0, result.output
    assert "as of 2026-11-04:" in result.output
    assert "excluded files:" not in result.output
    assert "share=44.44%" in result.output


def test_qa_summary_type_is_stable_for_the_formats(synthetic_build: BuildResult) -> None:
    """Both renderers take the same immutable summary and add no hidden state."""
    con = duckdb.connect(str(synthetic_build.db_path), read_only=True)
    try:
        summary = summarize(con)
    finally:
        con.close()
    assert isinstance(summary, QaSummary)
    assert summary.asof is None
