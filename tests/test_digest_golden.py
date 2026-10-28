"""Golden digests over the committed synthetic store (FR-3.1)."""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import duckdb
import pytest

from etfh.build import build
from etfh.digest import build_digest, default_week_end, validate_digest
from etfh.registry import Fund, Source, load_funds, load_sources

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RAW_ROOT = FIXTURES / "raw_synthetic"
GOLDEN = FIXTURES / "digest_golden"
MANIFEST = FIXTURES / "manifest.json"
CONFIG = REPO_ROOT / "config"


def fixture_days() -> list[date]:
    """Every as-of date the synthetic fixture manifest promises."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    days = {
        date.fromisoformat(day)
        for fund in manifest["funds"].values()
        for day in fund["as_of_dates"]
    }
    return sorted(days)


def golden_week_ends() -> list[date]:
    """The last Friday the fixtures support, and the last day they support."""
    days = fixture_days()
    fridays = [day for day in days if day.weekday() == 4]
    return [max(fridays), days[-1]]


@pytest.fixture(scope="module")
def synthetic_db(tmp_path_factory: pytest.TempPathFactory) -> duckdb.DuckDBPyConnection:
    out_dir = tmp_path_factory.mktemp("digest-golden")
    build(RAW_ROOT, out_dir)
    connection = duckdb.connect(str(out_dir / "etfh.duckdb"), read_only=True)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def registries() -> tuple[dict[str, Source], list[Fund]]:
    sources = load_sources(CONFIG / "sources.yaml")
    return sources, load_funds(CONFIG / "funds.csv", sources)


def test_the_golden_weeks_are_the_ones_the_fixtures_support() -> None:
    assert golden_week_ends() == [date(2026, 10, 30), date(2026, 11, 3)]


@pytest.mark.parametrize("week_end", golden_week_ends(), ids=lambda day: day.isoformat())
def test_golden_digest(
    synthetic_db: duckdb.DuckDBPyConnection,
    registries: tuple[dict[str, Source], list[Fund]],
    week_end: date,
) -> None:
    sources, funds = registries
    doc = build_digest(synthetic_db, week_end, public=True, sources=sources, funds=funds)
    validate_digest(doc)
    golden = GOLDEN / f"{week_end.isoformat()}.json"
    assert golden.is_file()
    assert json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n" == golden.read_text(
        encoding="utf-8"
    )
    assert json.loads(golden.read_text(encoding="utf-8")) == doc


def test_two_builds_are_byte_identical(
    synthetic_db: duckdb.DuckDBPyConnection,
    registries: tuple[dict[str, Source], list[Fund]],
) -> None:
    sources, funds = registries
    week_end = golden_week_ends()[-1]
    first = build_digest(synthetic_db, week_end, public=True, sources=sources, funds=funds)
    second = build_digest(synthetic_db, week_end, public=True, sources=sources, funds=funds)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_the_synthetic_store_builds_well_under_the_budget(
    synthetic_db: duckdb.DuckDBPyConnection,
    registries: tuple[dict[str, Source], list[Fund]],
) -> None:
    sources, funds = registries
    week_end = golden_week_ends()[-1]
    started = time.perf_counter()
    build_digest(synthetic_db, week_end, public=True, sources=sources, funds=funds)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"digest build took {elapsed:.2f}s"


def test_golden_content_documents_the_fixture_weeks(
    synthetic_db: duckdb.DuckDBPyConnection,
    registries: tuple[dict[str, Source], list[Fund]],
) -> None:
    sources, funds = registries

    friday = build_digest(
        synthetic_db, golden_week_ends()[0], public=True, sources=sources, funds=funds
    )
    assert friday["week_start"] == "2026-10-23"
    assert friday["known_by"] == "2026-10-31T04:00:00+00:00"
    assert [entry["ticker"] for entry in friday["funds"]] == ["SYNA", "SYNB", "SYNE"]
    assert {entry["status"] for entry in friday["funds"]} == {"new_in_window"}
    assert friday["overlap_shifts"] == []
    assert friday["quality"] == {
        "files_included": 13,
        "files_excluded": 2,
        "funds_without_data": ["SYNC", "SYND", "SYNF"],
    }

    syna = friday["funds"][0]
    assert syna["portfolio_date_start"] == "2026-10-29"
    assert syna["portfolio_date_end"] == "2026-10-30"
    assert syna["counts"] == {"adds": 0, "drops": 0, "weight_changes": 2}
    assert syna["turnover"] == pytest.approx(0.02225, abs=1e-9)
    assert syna["flow_estimate_sum"] is None
    assert syna["securities_disclosed"] is True

    # No fund has a portfolio at the 2026-10-27 cutoff, so the 2026-11-03 week
    # is all new data and has no overlap shift to report either.
    tuesday = build_digest(
        synthetic_db, golden_week_ends()[1], public=True, sources=sources, funds=funds
    )
    assert tuesday["known_by"] == "2026-11-04T05:00:00+00:00"
    assert tuesday["overlap_shifts"] == []


def test_a_week_with_a_portfolio_at_both_cutoffs_reports_overlap_shifts(
    synthetic_db: duckdb.DuckDBPyConnection,
    registries: tuple[dict[str, Source], list[Fund]],
) -> None:
    sources, funds = registries
    doc = build_digest(synthetic_db, date(2026, 11, 5), public=True, sources=sources, funds=funds)
    validate_digest(doc)

    assert {entry["status"] for entry in doc["funds"]} == {"ok"}
    assert [entry["ticker"] for entry in doc["funds"]] == ["SYNA", "SYNB", "SYNE"]
    assert [shift["fund_a"] for shift in doc["overlap_shifts"]] == ["SYNA", "SYNA", "SYNB"]
    assert [shift["fund_b"] for shift in doc["overlap_shifts"]] == ["SYNB", "SYNE", "SYNE"]
    assert doc["overlap_shifts"][0]["overlap_start"] == pytest.approx(0.2725)
    assert all(abs(shift["delta"]) >= 0.01 for shift in doc["overlap_shifts"])


def test_default_week_end_is_the_latest_friday() -> None:
    assert default_week_end(date(2026, 10, 30)) == date(2026, 10, 30)
    assert default_week_end(date(2026, 10, 31)) == date(2026, 10, 30)
    assert default_week_end(date(2026, 11, 1)) == date(2026, 10, 30)
    assert default_week_end(date(2026, 11, 3)) == date(2026, 10, 30)
    assert default_week_end(date(2026, 11, 6)) == date(2026, 11, 6)
