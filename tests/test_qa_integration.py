"""Validation and QA against the real build output (no fakes).

Runs :func:`etfh.build.build` on the committed synthetic raw store, then
checks the QA summary, the report text and the exit code. The snapshot below
is the report for the committed fixtures; it only changes when a parser,
the security master or the build changes what it produces for them.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from etfh.build import BuildResult, build
from etfh.qa import QaSummary, exit_code, render, summarize

FIXTURE_RAW = Path(__file__).resolve().parent / "fixtures" / "raw_synthetic"
FIXTURE_MANIFEST = Path(__file__).resolve().parent / "fixtures" / "manifest.json"

EXPECTED_REPORT = (
    "QA report\n"
    "files: 15 total (13 included, 2 excluded)\n"
    "excluded files:\n"
    "  SYNC as_of=2026-10-29 source=syn_csv\n"
    "    error unit_contradiction: declared unit fraction but weights total 0.8500 "
    "(expected ~1.00 within 1.0%)\n"
    "    error weight_sum: sum=0.8500 expected=1±0.005 "
    "(asset_class=equity, delta=0.1500)\n"
    "  SYND as_of=2026-10-29 source=syn_xlsx\n"
    "    error duplicate_security: security_id=ISIN:CA600000AA15 appears 2 times "
    "(rows 2, 3)\n"
    "warnings: 0\n"
    "unmatched securities: 30 row(s), showing up to 10:\n"
    "  SYNB Cash and Cash Equivalents\n"
    "  SYNB Cash and Cash Equivalents\n"
    "  SYNB Cash and Cash Equivalents\n"
    "  SYNB Cash and Cash Equivalents\n"
    "  SYNB Cash and Cash Equivalents\n"
    "  SYNF Birchwood Timber Corp.\n"
    "  SYNF Cash and Cash Equivalents\n"
    "  SYNF Cedarline Energy Corp.\n"
    "  SYNF Dunmore Mining Inc.\n"
    "  SYNF Elmsworth Capital Corp.\n"
)


@pytest.fixture(scope="module")
def build_result(tmp_path_factory: pytest.TempPathFactory) -> BuildResult:
    out = tmp_path_factory.mktemp("qa-integration")
    return build(FIXTURE_RAW, out)


@pytest.fixture(scope="module")
def summary(build_result: BuildResult) -> QaSummary:
    con = duckdb.connect(str(build_result.db_path), read_only=True)
    try:
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
