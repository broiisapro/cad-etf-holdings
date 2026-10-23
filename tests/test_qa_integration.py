"""Validation and QA against the real build output (no fakes).

Runs :func:`etfh.build.build` on the committed synthetic raw store, then
checks the QA summary, the report text and the exit code. The additive
``sources`` table (D53, written by the build in wave 03) is supplied here from
``config/sources.yaml`` so the report is identical whether or not the build
already writes it. The snapshot below is the report for the committed
fixtures; it only changes when a parser, the security master, the build or a
validation rule changes what it produces for them.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import duckdb
import polars as pl
import pytest

from etfh import analytics
from etfh.build import BuildResult, build
from etfh.qa import QaSummary, exit_code, render, summarize, unmatched_shares
from etfh.registry import load_sources

FIXTURE_RAW = Path(__file__).resolve().parent / "fixtures" / "raw_synthetic"
FIXTURE_MANIFEST = Path(__file__).resolve().parent / "fixtures" / "manifest.json"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

EXPECTED_REPORT = (
    "QA report\n"
    "files: 15 total (13 included, 2 excluded)\n"
    "excluded files:\n"
    "  SYNC as_of=2026-10-29 source=syn_csv\n"
    "    error weight_sum: sum=0.8500 expected=1±0.006 "
    "(asset_class=equity, rows=20, weight_decimals=4, tolerance=0.006, delta=0.1500)\n"
    "  SYND as_of=2026-10-29 source=syn_xlsx\n"
    "    error duplicate_security: security_id=ISIN:CA600000AA15 appears 2 times "
    "(rows 2, 3); different identifier forms: CUSIP, ISIN\n"
    "warnings: 0\n"
    "unmatched securities: 24 row(s), showing up to 10:\n"
    "  SYNF Birchwood Timber Corp.\n"
    "  SYNF Cedarline Energy Corp.\n"
    "  SYNF Dunmore Mining Inc.\n"
    "  SYNF Elmsworth Capital Corp.\n"
    "  SYNF Everfield Agriculture Corp.\n"
    "  SYNF Fairhaven Financial Group\n"
    "  SYNF Foxglove Networks Inc.\n"
    "  SYNF Glenora Utilities Inc.\n"
    "  SYNF Harbourside Logistics Corp.\n"
    "  SYNF Ironbridge Steel Inc.\n"
    "unmatched weight share (included files):\n"
    "  daily files:\n"
    "    issuer median:\n"
    "      synthetic 0.00%\n"
    "    worst funds: none\n"
    "  quarterly files:\n"
    "    issuer median:\n"
    "      synthetic 100.00%\n"
    "    worst funds:\n"
    "      SYNF as_of=2026-09-30 source=syn_pdf share=100.00%\n"
)


def _write_sources_table(con: duckdb.DuckDBPyConnection) -> None:
    sources = load_sources(CONFIG_DIR / "sources.yaml")
    con.execute(
        "CREATE OR REPLACE TABLE sources ("
        "source_id VARCHAR, issuer VARCHAR, kind VARCHAR, format VARCHAR)"
    )
    con.executemany(
        "INSERT INTO sources VALUES (?, ?, ?, ?)",
        [
            (source.id, source.issuer, source.kind.value, source.format.value)
            for source in sources.values()
        ],
    )


@pytest.fixture(scope="module")
def build_result(tmp_path_factory: pytest.TempPathFactory) -> BuildResult:
    out = tmp_path_factory.mktemp("qa-integration")
    return build(FIXTURE_RAW, out)


@pytest.fixture(scope="module")
def summary(build_result: BuildResult) -> QaSummary:
    con = duckdb.connect(str(build_result.db_path))
    try:
        _write_sources_table(con)
        return summarize(con)
    finally:
        con.close()


def test_exactly_the_two_manifest_bad_files_are_excluded(
    summary: QaSummary, build_result: BuildResult
) -> None:
    manifest = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    expected = {entry["fund_ticker"]: entry["failing_check"] for entry in manifest["bad_files"]}
    assert build_result.n_excluded_files == len(expected)
    excluded = {entry.fund_ticker: entry for entry in summary.excluded_files}
    assert set(excluded) == set(expected)
    for ticker, failing_check in expected.items():
        error_checks = {
            check for check, severity, _detail in excluded[ticker].findings if severity == "error"
        }
        assert failing_check in error_checks


def test_exit_code_is_zero_with_the_two_bad_files_excluded(summary: QaSummary) -> None:
    assert exit_code(summary) == 0


def test_render_matches_the_snapshot(summary: QaSummary) -> None:
    assert render(summary) == EXPECTED_REPORT


def test_asof_unmatched_shares_match_the_version_analytics_selects(
    build_result: BuildResult,
) -> None:
    con = duckdb.connect(str(build_result.db_path), read_only=True)
    try:
        for asof in (date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 31), date(2026, 11, 3)):
            frame = unmatched_shares(con, asof)
            assert frame.columns == [
                "fund_ticker",
                "as_of_date",
                "source_id",
                "unmatched_weight",
                "total_weight",
                "share",
            ]
            funds = analytics.funds_available(con)
            assert frame["fund_ticker"].n_unique() == frame.height
            for fund in funds:
                selected = analytics.holdings_asof(con, fund, asof)
                match = frame.filter(pl.col("fund_ticker") == fund)
                if selected.is_empty():
                    assert match.is_empty(), (asof, fund)
                    continue
                assert match.height == 1, (asof, fund)
                securities = selected.filter(
                    ~pl.col("asset_class").str.to_lowercase().is_in(["cash", "fx", "derivative"])
                )
                unmatched = float(
                    securities.filter(pl.col("security_id").is_null())["weight"].abs().sum()
                )
                total = float(securities["weight"].abs().sum())
                row = match.row(0, named=True)
                assert row["as_of_date"] == selected["as_of_date"][0]
                assert row["source_id"] == selected["source_id"][0]
                assert row["unmatched_weight"] == pytest.approx(unmatched)
                assert row["total_weight"] == pytest.approx(total)
                assert row["share"] == pytest.approx(unmatched / total if total else 0.0)
    finally:
        con.close()
