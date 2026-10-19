"""Tests for ``etfh export`` (issue #77, FR-1.12)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import polars as pl
import pytest

from etfh import analytics
from etfh.analytics import FundNotFoundError
from etfh.export import export_aggregates
from tests._build_helpers import BuildResult, build_fixture_db

ASOF = date(2026, 11, 2)
_FORBIDDEN_COLUMNS = (
    "security_id",
    "security_name",
    "security_ticker",
    "isin",
    "cusip",
    "raw_file_sha256",
)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, BuildResult, Path]:
    root = tmp_path_factory.mktemp("export")
    result = build_fixture_db(root / "out")
    return root, result, root / "exports"


def _connection(result: BuildResult) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(result.db_path), read_only=True)


def _read(path: Path) -> pl.DataFrame:
    return pl.read_csv(path, try_parse_dates=True)


def test_writes_the_three_aggregate_files(built: tuple[Path, BuildResult, Path]) -> None:
    _, result, out_dir = built
    connection = _connection(result)
    try:
        paths = export_aggregates(connection, out_dir, asof=ASOF)
    finally:
        connection.close()
    assert [path.name for path in paths] == [
        "overlap_matrix.csv",
        "concentration.csv",
        "sector_breakdown.csv",
    ]
    assert all(path.is_file() for path in paths)
    assert {path.name for path in out_dir.iterdir()} == {
        "overlap_matrix.csv",
        "concentration.csv",
        "sector_breakdown.csv",
    }


def test_columns_are_aggregates_only(built: tuple[Path, BuildResult, Path]) -> None:
    _, result, out_dir = built
    connection = _connection(result)
    try:
        export_aggregates(connection, out_dir, asof=ASOF)
    finally:
        connection.close()
    matrix = _read(out_dir / "overlap_matrix.csv")
    concentration = _read(out_dir / "concentration.csv")
    breakdown = _read(out_dir / "sector_breakdown.csv")
    assert matrix.columns == ["fund_a", "fund_b", "overlap"]
    assert concentration.columns == [
        "fund",
        "as_of_date",
        "n_holdings",
        "top10_weight",
        "hhi",
        "partial",
    ]
    assert breakdown.columns == ["fund", "as_of_date", "sector", "weight"]
    for frame in (matrix, concentration, breakdown):
        for column in _FORBIDDEN_COLUMNS:
            assert column not in frame.columns


def test_no_security_names_identifiers_or_weights_leak_into_the_files(
    built: tuple[Path, BuildResult, Path],
) -> None:
    _, result, out_dir = built
    connection = _connection(result)
    try:
        export_aggregates(connection, out_dir, asof=ASOF)
    finally:
        connection.close()
    holdings = pl.read_parquet(result.out_dir / "parquet" / "holdings.parquet")
    names = holdings["security_name"].unique().to_list()
    identifiers = (
        holdings["security_id"].drop_nulls().unique().to_list()
        + holdings["isin"].drop_nulls().unique().to_list()
        + holdings["cusip"].drop_nulls().unique().to_list()
    )
    for name in ("overlap_matrix.csv", "concentration.csv", "sector_breakdown.csv"):
        text = (out_dir / name).read_text(encoding="utf-8")
        for security_name in names:
            assert security_name not in text
        for identifier in identifiers:
            assert identifier not in text
        assert "Quarrystone" not in text
        assert "CA600016AA74" not in text


def test_values_match_the_analytics_layer(built: tuple[Path, BuildResult, Path]) -> None:
    _, result, out_dir = built
    connection = _connection(result)
    try:
        export_aggregates(connection, out_dir, asof=ASOF, funds=["SYNA", "SYNB"])
        sy_na = analytics.concentration(connection, "SYNA", ASOF)
        sy_nb = analytics.concentration(connection, "SYNB", ASOF)
        expected_overlap = analytics.overlap(connection, "SYNA", "SYNB", ASOF)
    finally:
        connection.close()
    concentration = _read(out_dir / "concentration.csv")
    assert concentration.height == 2
    assert concentration["fund"].to_list() == ["SYNA", "SYNB"]
    row = concentration.filter(concentration["fund"] == "SYNA").row(0, named=True)
    assert row["as_of_date"] == sy_na.as_of_date
    assert row["n_holdings"] == sy_na.n_holdings
    assert row["top10_weight"] == pytest.approx(sy_na.top10_weight)
    assert row["hhi"] == pytest.approx(sy_na.hhi)
    assert row["partial"] is sy_na.partial
    row_b = concentration.filter(concentration["fund"] == "SYNB").row(0, named=True)
    assert row_b["n_holdings"] == sy_nb.n_holdings
    matrix = _read(out_dir / "overlap_matrix.csv")
    assert matrix.height == 4
    value = matrix.filter((matrix["fund_a"] == "SYNA") & (matrix["fund_b"] == "SYNB"))["overlap"][0]
    assert value == pytest.approx(expected_overlap.overlap)


def test_funds_filter_limits_every_file(built: tuple[Path, BuildResult, Path]) -> None:
    _, result, out_dir = built
    connection = _connection(result)
    try:
        export_aggregates(connection, out_dir, asof=ASOF, funds=["SYNA"])
    finally:
        connection.close()
    matrix = _read(out_dir / "overlap_matrix.csv")
    concentration = _read(out_dir / "concentration.csv")
    breakdown = _read(out_dir / "sector_breakdown.csv")
    assert matrix["fund_a"].to_list() == ["SYNA"]
    assert matrix["fund_b"].to_list() == ["SYNA"]
    assert concentration["fund"].to_list() == ["SYNA"]
    assert breakdown["fund"].unique().to_list() == ["SYNA"]
    assert breakdown["weight"].sum() == pytest.approx(1.0, abs=0.02)


def test_sector_breakdown_is_sorted_by_fund_then_weight(
    built: tuple[Path, BuildResult, Path],
) -> None:
    _, result, out_dir = built
    connection = _connection(result)
    try:
        export_aggregates(connection, out_dir, asof=ASOF, funds=["SYNA", "SYNB"])
    finally:
        connection.close()
    breakdown = _read(out_dir / "sector_breakdown.csv")
    assert breakdown.height > 2
    for fund in ("SYNA", "SYNB"):
        weights = breakdown.filter(breakdown["fund"] == fund)["weight"].to_list()
        assert weights == sorted(weights, reverse=True)
        assert sum(weights) == pytest.approx(1.0, abs=0.02)


def test_unknown_fund_raises(built: tuple[Path, BuildResult, Path]) -> None:
    _, result, out_dir = built
    connection = _connection(result)
    try:
        with pytest.raises(FundNotFoundError):
            export_aggregates(connection, out_dir, asof=ASOF, funds=["NOPE"])
    finally:
        connection.close()
