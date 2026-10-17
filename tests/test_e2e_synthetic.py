"""End-to-end synthetic pipeline test with the real defaults (no fakes).

Runs ``etfh.build.build`` on the committed synthetic raw store, then drives the
CLI for QA, overlap and changes. This is the offline half of the Epic 1
definition of done; the live ``collect`` and Streamlit health check stay in the
integration run documented in ``docs/epic-01-pipeline-core.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import polars as pl
import pytest
from typer.testing import CliRunner

from etfh.build import BuildResult, build
from etfh.cli import app

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RAW_ROOT = FIXTURES / "raw_synthetic"
EXPECTED_OVERLAP = FIXTURES / "expected_overlap.json"

runner = CliRunner()

_TABLES = ("file_versions", "holdings", "qa_events", "holdings_changes")


@pytest.fixture(scope="module")
def e2e_build(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, BuildResult]:
    out_dir = tmp_path_factory.mktemp("e2e")
    return out_dir, build(RAW_ROOT, out_dir)


def test_build_produces_the_expected_tables(e2e_build: tuple[Path, BuildResult]) -> None:
    out_dir, result = e2e_build
    assert result.n_file_versions == 15
    assert result.n_included_files == 13
    assert result.n_excluded_files == 2
    assert result.db_path == out_dir / "etfh.duckdb"
    assert result.db_path.is_file()
    for table in _TABLES:
        assert (out_dir / "parquet" / f"{table}.parquet").is_file()
    assert (out_dir / "reports" / "unmatched.csv").is_file()
    assert (out_dir / "reports" / "fuzzy_proposals.csv").is_file()
    connection = duckdb.connect(str(result.db_path), read_only=True)
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in _TABLES
        }
    finally:
        connection.close()
    assert counts["file_versions"] == result.n_file_versions
    assert counts["holdings"] == result.n_holdings
    assert counts["qa_events"] == result.n_qa_events
    assert counts["holdings_changes"] == result.n_changes


def test_bad_files_stay_out_of_holdings(e2e_build: tuple[Path, BuildResult]) -> None:
    _, result = e2e_build
    connection = duckdb.connect(str(result.db_path), read_only=True)
    try:
        bad = connection.execute(
            "SELECT COUNT(*) FROM holdings WHERE fund_ticker IN ('SYNC', 'SYND')"
        ).fetchone()[0]
        cash = connection.execute(
            "SELECT COUNT(*) FROM holdings WHERE security_id = 'CASH:CAD'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert bad == 0
    assert cash > 0


def test_synf_names_are_unmatched_and_proposed(e2e_build: tuple[Path, BuildResult]) -> None:
    out_dir, result = e2e_build
    assert result.n_unmatched > 0
    proposals = pl.read_csv(out_dir / "reports" / "fuzzy_proposals.csv")
    assert proposals.height > 0
    assert proposals.columns == [
        "security_name",
        "candidate_security_id",
        "candidate_name",
        "score",
    ]
    assert proposals["score"].min() >= 92
    unmatched = pl.read_csv(out_dir / "reports" / "unmatched.csv")
    assert set(unmatched["fund_ticker"].unique().to_list()) == {"SYNF"}


def test_qa_cli_exits_zero_with_the_two_bad_files(e2e_build: tuple[Path, BuildResult]) -> None:
    _, result = e2e_build
    cli = runner.invoke(app, ["qa", "--db", str(result.db_path)])
    assert cli.exit_code == 0, cli.output
    assert "files: 15 total (13 included, 2 excluded)" in cli.output
    assert "SYNC" in cli.output
    assert "SYND" in cli.output


def test_overlap_json_equals_the_committed_fixture(e2e_build: tuple[Path, BuildResult]) -> None:
    _, result = e2e_build
    cli = runner.invoke(
        app,
        [
            "overlap",
            "SYNA",
            "SYNB",
            "--asof",
            "2026-11-02",
            "--db",
            str(result.db_path),
            "--json",
        ],
    )
    assert cli.exit_code == 0, cli.output
    expected = json.loads(EXPECTED_OVERLAP.read_text(encoding="utf-8"))
    assert json.loads(cli.output) == expected


def test_overlap_one_day_later_breaks_the_lookahead_trap(
    e2e_build: tuple[Path, BuildResult],
) -> None:
    _, result = e2e_build
    cli = runner.invoke(
        app,
        [
            "overlap",
            "SYNA",
            "SYNB",
            "--asof",
            "2026-11-03",
            "--db",
            str(result.db_path),
        ],
    )
    assert cli.exit_code == 0, cli.output
    assert "0.0010" in cli.output


def test_changes_cli_lists_the_consecutive_pairs(e2e_build: tuple[Path, BuildResult]) -> None:
    _, result = e2e_build
    cli = runner.invoke(
        app,
        [
            "changes",
            "--fund",
            "SYNA",
            "--from",
            "2026-10-29",
            "--to",
            "2026-11-02",
            "--db",
            str(result.db_path),
        ],
    )
    assert cli.exit_code == 0, cli.output
    assert "2026-10-29 -> 2026-10-30" in cli.output
    assert "2026-10-30 -> 2026-11-02" in cli.output


def test_rebuild_is_byte_identical(e2e_build: tuple[Path, BuildResult], tmp_path: Path) -> None:
    first_dir, _ = e2e_build
    second = build(RAW_ROOT, tmp_path / "second")
    for table in _TABLES:
        left = (first_dir / "parquet" / f"{table}.parquet").read_bytes()
        right = (tmp_path / "second" / "parquet" / f"{table}.parquet").read_bytes()
        assert left == right, table
    assert second.n_holdings > 0
