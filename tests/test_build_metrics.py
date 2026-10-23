"""Tests for the fund-metrics path of the build (issue #150)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pytest

from etfh.schema import FundMetrics
from tests._build_helpers import (
    METRICS_FUND,
    METRICS_SOURCE_ID,
    RAW_ROOT,
    FakeMetricsParser,
    build_metrics_db,
    copy_raw_files,
    make_metrics_config,
    metrics_error,
    metrics_payload,
    no_metrics_findings,
    write_metrics_raw,
)

_TWO_DAYS = [
    (date(2026, 10, 29), 25.0, 1_000_000.0, 25_000_000.0),
    (date(2026, 10, 30), 25.5, 1_050_000.0, 26_775_000.0),
]
_SYNA_DAY = [(date(2026, 10, 29), 25.0, 1_000_000.0, 25_000_000.0)]


def _query(db_path: Path, sql: str) -> pl.DataFrame:
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        return connection.execute(sql).pl()
    finally:
        connection.close()


def test_metrics_only_file_produces_rows_and_a_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = make_metrics_config(tmp_path)
    raw_root = tmp_path / "raw"
    raw = write_metrics_raw(
        raw_root,
        metrics_payload(_TWO_DAYS),
        fetched_at=datetime(2026, 10, 31, 12, tzinfo=UTC),
        as_of_hint=date(2026, 10, 30),
    )
    out_dir = tmp_path / "out"
    result = build_metrics_db(
        out_dir,
        raw_root=raw_root,
        config_dir=config_dir,
        metrics_parsers=[FakeMetricsParser()],
        monkeypatch=monkeypatch,
    )
    db_path = out_dir / "etfh.duckdb"
    frame = _query(db_path, "SELECT * FROM fund_metrics ORDER BY as_of_date")
    assert frame.columns == list(FundMetrics.to_schema().columns)
    assert frame.height == 2
    assert frame["fund_ticker"].to_list() == [METRICS_FUND, METRICS_FUND]
    assert frame["as_of_date"].to_list() == [date(2026, 10, 29), date(2026, 10, 30)]
    assert frame["file_version"].to_list() == [1, 1]
    assert frame["nav_per_unit"].to_list() == [25.0, 25.5]
    assert frame["shares_outstanding"].to_list() == [1_000_000.0, 1_050_000.0]
    assert frame["aum"].to_list() == [25_000_000.0, 26_775_000.0]
    assert frame["currency"].to_list() == ["CAD", "CAD"]
    assert frame["source_id"].to_list() == [METRICS_SOURCE_ID, METRICS_SOURCE_ID]
    assert frame["raw_file_sha256"].to_list() == [raw.sha256, raw.sha256]
    assert frame["known_at"].to_list() == [datetime(2026, 10, 31, 12, tzinfo=UTC)] * 2

    versions = _query(
        db_path,
        "SELECT source_id, fund_ticker, as_of_date, file_version, status FROM file_versions "
        "ORDER BY as_of_date",
    )
    assert versions.height == 2
    assert versions["source_id"].to_list() == [METRICS_SOURCE_ID, METRICS_SOURCE_ID]
    assert versions["status"].to_list() == ["included", "included"]
    assert versions["file_version"].to_list() == [1, 1]
    assert result.n_fund_metrics == 2
    assert result.n_file_versions == 2
    assert result.n_holdings == 0
    assert _query(db_path, "SELECT COUNT(*) AS n FROM qa_events")["n"][0] == 0
    assert (out_dir / "parquet" / "fund_metrics.parquet").is_file()


def test_metrics_parser_can_read_a_holdings_source_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``purpose_metrics`` reads ``purpose_daily`` files (interfaces-06 §2)."""
    config_dir = make_metrics_config(tmp_path)
    raw_root = tmp_path / "raw"
    copy_raw_files(RAW_ROOT, raw_root, {("SYNA", date(2026, 10, 29))})
    out_dir = tmp_path / "out"
    parser = FakeMetricsParser(claims=lambda raw: raw.source_id == "syn_csv", fixed_rows=_SYNA_DAY)
    seen: list[tuple[str, str]] = []

    def recording_validator(parsed: Any, *, source: Any, config: Any) -> list[Any]:
        seen.append((source.id, parsed.frame["source_id"][0]))
        return []

    result = build_metrics_db(
        out_dir,
        raw_root=raw_root,
        config_dir=config_dir,
        metrics_parsers=[parser],
        metrics_validator=recording_validator,
        monkeypatch=monkeypatch,
    )
    db_path = out_dir / "etfh.duckdb"
    assert result.n_holdings == 25
    metrics = _query(db_path, "SELECT * FROM fund_metrics")
    assert metrics.height == 1
    assert metrics["fund_ticker"].to_list() == ["SYNA"]
    assert metrics["source_id"].to_list() == [METRICS_SOURCE_ID]
    # the validator sees the metrics source, not the raw file's source
    assert seen == [(METRICS_SOURCE_ID, METRICS_SOURCE_ID)]
    versions = _query(
        db_path,
        "SELECT source_id, status FROM file_versions ORDER BY source_id",
    )
    assert versions.rows() == [("syn_csv", "included"), (METRICS_SOURCE_ID, "included")]


def test_republished_metrics_file_creates_a_second_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = make_metrics_config(tmp_path)
    raw_root = tmp_path / "raw"
    first = write_metrics_raw(
        raw_root,
        metrics_payload([(date(2026, 10, 30), 25.0, 1_000_000.0, 25_000_000.0)]),
        fetched_at=datetime(2026, 10, 30, 22, tzinfo=UTC),
        as_of_hint=date(2026, 10, 30),
    )
    second = write_metrics_raw(
        raw_root,
        metrics_payload([(date(2026, 10, 30), 25.0, 1_200_000.0, 30_000_000.0)]),
        fetched_at=datetime(2026, 10, 31, 14, tzinfo=UTC),
        as_of_hint=date(2026, 10, 30),
    )
    out_dir = tmp_path / "out"
    build_metrics_db(
        out_dir,
        raw_root=raw_root,
        config_dir=config_dir,
        metrics_parsers=[FakeMetricsParser()],
        monkeypatch=monkeypatch,
    )
    db_path = out_dir / "etfh.duckdb"
    metrics = _query(
        db_path,
        "SELECT file_version, shares_outstanding, raw_file_sha256 FROM fund_metrics "
        "ORDER BY file_version",
    )
    assert metrics["file_version"].to_list() == [1, 2]
    assert metrics["shares_outstanding"].to_list() == [1_000_000.0, 1_200_000.0]
    assert metrics["raw_file_sha256"].to_list() == [first.sha256, second.sha256]
    versions = _query(
        db_path,
        "SELECT file_version, known_at, status FROM file_versions ORDER BY file_version",
    )
    assert versions["file_version"].to_list() == [1, 2]
    assert versions["known_at"].to_list() == [
        datetime(2026, 10, 30, 22, tzinfo=UTC),
        datetime(2026, 10, 31, 14, tzinfo=UTC),
    ]
    assert versions["status"].to_list() == ["included", "included"]


def test_metrics_error_excludes_only_the_metrics_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = make_metrics_config(tmp_path)
    raw_root = tmp_path / "raw"
    copy_raw_files(RAW_ROOT, raw_root, {("SYNA", date(2026, 10, 29))})
    out_dir = tmp_path / "out"
    result = build_metrics_db(
        out_dir,
        raw_root=raw_root,
        config_dir=config_dir,
        metrics_parsers=[
            FakeMetricsParser(claims=lambda raw: raw.source_id == "syn_csv", fixed_rows=_SYNA_DAY)
        ],
        metrics_validator=metrics_error,
        monkeypatch=monkeypatch,
    )
    db_path = out_dir / "etfh.duckdb"
    versions = _query(
        db_path,
        "SELECT source_id, status FROM file_versions ORDER BY source_id",
    )
    assert versions.rows() == [("syn_csv", "included"), (METRICS_SOURCE_ID, "excluded")]
    assert result.n_holdings == 25
    assert result.n_fund_metrics == 0
    assert _query(db_path, "SELECT COUNT(*) AS n FROM fund_metrics")["n"][0] == 0
    events = _query(
        db_path,
        'SELECT source_id, "check", severity, excluded FROM qa_events',
    )
    assert events.rows() == [(METRICS_SOURCE_ID, "nav_positive", "error", True)]


def test_holdings_error_does_not_exclude_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = make_metrics_config(tmp_path)
    raw_root = tmp_path / "raw"
    # SYNC is the fixture whose holdings weights do not sum to one.
    copy_raw_files(RAW_ROOT, raw_root, {("SYNC", date(2026, 10, 29))})
    out_dir = tmp_path / "out"
    result = build_metrics_db(
        out_dir,
        raw_root=raw_root,
        config_dir=config_dir,
        metrics_parsers=[
            FakeMetricsParser(claims=lambda raw: raw.source_id == "syn_csv", fixed_rows=_SYNA_DAY)
        ],
        metrics_validator=no_metrics_findings,
        monkeypatch=monkeypatch,
    )
    db_path = out_dir / "etfh.duckdb"
    assert result.n_holdings == 0
    versions = _query(
        db_path,
        "SELECT source_id, status FROM file_versions ORDER BY source_id",
    )
    assert versions.rows() == [("syn_csv", "excluded"), (METRICS_SOURCE_ID, "included")]
    assert result.n_fund_metrics == 1
    checks = _query(db_path, 'SELECT DISTINCT "check" FROM qa_events ORDER BY "check"')
    assert checks["check"].to_list() == ["weight_sum"]


def test_metrics_parser_error_is_an_error_event_without_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = make_metrics_config(tmp_path)
    raw_root = tmp_path / "raw"
    copy_raw_files(RAW_ROOT, raw_root, {("SYNA", date(2026, 10, 29))})
    out_dir = tmp_path / "out"
    result = build_metrics_db(
        out_dir,
        raw_root=raw_root,
        config_dir=config_dir,
        metrics_parsers=[
            FakeMetricsParser(
                claims=lambda raw: raw.source_id == "syn_csv", raises="not a metrics history"
            )
        ],
        monkeypatch=monkeypatch,
    )
    db_path = out_dir / "etfh.duckdb"
    # the holdings file is still included and its rows are untouched
    assert result.n_holdings == 25
    assert result.n_fund_metrics == 0
    versions = _query(db_path, "SELECT source_id, status FROM file_versions")
    assert versions.rows() == [("syn_csv", "included")]
    events = _query(
        db_path,
        'SELECT source_id, "check", severity, excluded FROM qa_events',
    )
    assert events.rows() == [("syn_csv", "metrics_parse_error", "error", True)]


def test_unknown_metrics_source_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = make_metrics_config(tmp_path)
    raw_root = tmp_path / "raw"
    write_metrics_raw(
        raw_root,
        metrics_payload(_TWO_DAYS),
        fetched_at=datetime(2026, 10, 31, 12, tzinfo=UTC),
    )
    out_dir = tmp_path / "out"
    result = build_metrics_db(
        out_dir,
        raw_root=raw_root,
        config_dir=config_dir,
        metrics_parsers=[FakeMetricsParser(source_id="ghost_metrics", claims=lambda raw: True)],
        monkeypatch=monkeypatch,
    )
    db_path = out_dir / "etfh.duckdb"
    assert result.n_fund_metrics == 0
    assert result.n_file_versions == 0
    events = _query(
        db_path,
        'SELECT source_id, fund_ticker, "check", severity, excluded FROM qa_events',
    )
    assert events.rows() == [("ghost_metrics", METRICS_FUND, "unknown_source", "error", True)]


def test_parser_shas_are_replaced_by_the_parsed_file_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = make_metrics_config(tmp_path)
    raw_root = tmp_path / "raw"
    raw = write_metrics_raw(
        raw_root,
        metrics_payload(_TWO_DAYS),
        fetched_at=datetime(2026, 10, 31, 12, tzinfo=UTC),
    )
    out_dir = tmp_path / "out"
    build_metrics_db(
        out_dir,
        raw_root=raw_root,
        config_dir=config_dir,
        metrics_parsers=[FakeMetricsParser(old_sha="f" * 64)],
        monkeypatch=monkeypatch,
    )
    frame = _query(out_dir / "etfh.duckdb", "SELECT DISTINCT raw_file_sha256 FROM fund_metrics")
    assert frame["raw_file_sha256"].to_list() == [raw.sha256]


def test_published_at_becomes_known_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = make_metrics_config(tmp_path)
    raw_root = tmp_path / "raw"
    published = datetime(2026, 10, 30, 4, tzinfo=UTC)
    write_metrics_raw(
        raw_root,
        metrics_payload(_TWO_DAYS),
        fetched_at=datetime(2026, 11, 2, 12, tzinfo=UTC),
    )
    out_dir = tmp_path / "out"
    build_metrics_db(
        out_dir,
        raw_root=raw_root,
        config_dir=config_dir,
        metrics_parsers=[FakeMetricsParser(published_at=published)],
        monkeypatch=monkeypatch,
    )
    known = _query(out_dir / "etfh.duckdb", "SELECT DISTINCT known_at FROM file_versions")
    assert known["known_at"].to_list() == [published]


def test_holdings_outputs_do_not_change_when_metrics_are_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metrics path is additive: same raw holdings, byte-identical outputs."""
    config_dir = make_metrics_config(tmp_path)
    without = tmp_path / "raw_holdings"
    copy_raw_files(RAW_ROOT, without, {("SYNA", date(2026, 10, 29)), ("SYNB", date(2026, 10, 29))})
    with_metrics = tmp_path / "raw_with_metrics"
    copy_raw_files(
        RAW_ROOT, with_metrics, {("SYNA", date(2026, 10, 29)), ("SYNB", date(2026, 10, 29))}
    )
    write_metrics_raw(
        with_metrics,
        metrics_payload(_TWO_DAYS),
        fetched_at=datetime(2026, 10, 31, 12, tzinfo=UTC),
    )
    build_metrics_db(
        tmp_path / "a",
        raw_root=without,
        config_dir=config_dir,
        metrics_parsers=[],
        monkeypatch=monkeypatch,
    )
    build_metrics_db(
        tmp_path / "b",
        raw_root=with_metrics,
        config_dir=config_dir,
        metrics_parsers=[FakeMetricsParser()],
        monkeypatch=monkeypatch,
    )
    for table in ("holdings", "holdings_changes"):
        left = (tmp_path / "a" / "parquet" / f"{table}.parquet").read_bytes()
        right = (tmp_path / "b" / "parquet" / f"{table}.parquet").read_bytes()
        assert left == right, table
    for report in ("unmatched.csv", "fuzzy_proposals.csv", "name_proposals.csv"):
        left = (tmp_path / "a" / "reports" / report).read_bytes()
        right = (tmp_path / "b" / "reports" / report).read_bytes()
        assert left == right, report
    holdings_a = _query(tmp_path / "a" / "etfh.duckdb", "SELECT * FROM holdings")
    holdings_b = _query(tmp_path / "b" / "etfh.duckdb", "SELECT * FROM holdings")
    assert holdings_a.equals(holdings_b)


def test_fund_metrics_output_is_sorted_and_rebuilds_byte_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = make_metrics_config(tmp_path)
    raw_root = tmp_path / "raw"
    write_metrics_raw(
        raw_root,
        metrics_payload(list(reversed(_TWO_DAYS))),
        fetched_at=datetime(2026, 10, 31, 12, tzinfo=UTC),
    )
    write_metrics_raw(
        raw_root,
        metrics_payload([(date(2026, 10, 28), 24.5, 990_000.0, 24_255_000.0)]),
        fetched_at=datetime(2026, 10, 29, 12, tzinfo=UTC),
    )
    for name in ("a", "b"):
        build_metrics_db(
            tmp_path / name,
            raw_root=raw_root,
            config_dir=config_dir,
            metrics_parsers=[FakeMetricsParser()],
            monkeypatch=monkeypatch,
        )
    left = (tmp_path / "a" / "parquet" / "fund_metrics.parquet").read_bytes()
    right = (tmp_path / "b" / "parquet" / "fund_metrics.parquet").read_bytes()
    assert left == right
    frame = pl.read_parquet(tmp_path / "a" / "parquet" / "fund_metrics.parquet")
    assert frame.equals(frame.sort(["fund_ticker", "as_of_date", "file_version", "source_id"]))
    assert frame["as_of_date"].to_list() == [
        date(2026, 10, 28),
        date(2026, 10, 29),
        date(2026, 10, 30),
    ]


def test_default_validator_reports_an_inconsistent_aum_as_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The build imports E's real validator when none is injected."""
    config_dir = make_metrics_config(tmp_path)
    raw_root = tmp_path / "raw"
    write_metrics_raw(
        raw_root,
        metrics_payload([(date(2026, 10, 29), 25.0, 1_000_000.0, 100_000_000.0)]),
        fetched_at=datetime(2026, 10, 31, 12, tzinfo=UTC),
    )
    out_dir = tmp_path / "out"
    result = build_metrics_db(
        out_dir,
        raw_root=raw_root,
        config_dir=config_dir,
        metrics_parsers=[FakeMetricsParser()],
        metrics_validator=None,
        monkeypatch=monkeypatch,
    )
    db_path = out_dir / "etfh.duckdb"
    assert result.n_fund_metrics == 1
    events = _query(
        db_path,
        'SELECT source_id, "check", severity, excluded FROM qa_events',
    )
    assert events.rows() == [(METRICS_SOURCE_ID, "aum_consistency", "warning", False)]
    status = _query(db_path, "SELECT status FROM file_versions")
    assert status["status"].to_list() == ["included"]


def test_default_validator_excludes_a_metrics_file_with_negative_shares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = make_metrics_config(tmp_path)
    raw_root = tmp_path / "raw"
    write_metrics_raw(
        raw_root,
        metrics_payload([(date(2026, 10, 29), 25.0, -1.0, 25.0)]),
        fetched_at=datetime(2026, 10, 31, 12, tzinfo=UTC),
    )
    out_dir = tmp_path / "out"
    result = build_metrics_db(
        out_dir,
        raw_root=raw_root,
        config_dir=config_dir,
        metrics_parsers=[FakeMetricsParser()],
        metrics_validator=None,
        monkeypatch=monkeypatch,
    )
    db_path = out_dir / "etfh.duckdb"
    assert result.n_fund_metrics == 0
    status = _query(db_path, "SELECT status FROM file_versions")
    assert status["status"].to_list() == ["excluded"]
    events = _query(
        db_path,
        'SELECT "check", severity, excluded FROM qa_events ORDER BY "check"',
    )
    assert "shares_nonnegative" in events["check"].to_list()
    assert set(events["severity"].to_list()) == {"error"}
    assert set(events["excluded"].to_list()) == {True}


def test_a_metrics_source_file_is_never_parsed_as_holdings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broad PDF/CSV holdings parser must not touch a metrics source's files."""
    import etfh.build as build_module

    config_dir = make_metrics_config(tmp_path)
    raw_root = tmp_path / "raw"
    write_metrics_raw(
        raw_root,
        metrics_payload(_TWO_DAYS),
        fetched_at=datetime(2026, 10, 31, 12, tzinfo=UTC),
    )

    class BroadHoldingsParser:
        def can_parse(self, raw: Any) -> bool:
            return True

        def parse(self, raw: Any) -> Any:
            raise AssertionError("the holdings path must not run for a metrics source")

    monkeypatch.setattr(build_module, "find_parser", lambda raw, source: BroadHoldingsParser())
    out_dir = tmp_path / "out"
    result = build_metrics_db(
        out_dir,
        raw_root=raw_root,
        config_dir=config_dir,
        metrics_parsers=[FakeMetricsParser()],
        monkeypatch=monkeypatch,
    )
    db_path = out_dir / "etfh.duckdb"
    assert result.n_holdings == 0
    assert result.n_fund_metrics == 2
    versions = _query(db_path, "SELECT source_id, status FROM file_versions")
    assert versions.rows() == [
        (METRICS_SOURCE_ID, "included"),
        (METRICS_SOURCE_ID, "included"),
    ]
    assert _query(db_path, "SELECT COUNT(*) AS n FROM qa_events")["n"][0] == 0


def test_a_metrics_source_without_a_parser_is_reported_as_no_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = make_metrics_config(tmp_path)
    raw_root = tmp_path / "raw"
    write_metrics_raw(
        raw_root,
        metrics_payload(_TWO_DAYS),
        fetched_at=datetime(2026, 10, 31, 12, tzinfo=UTC),
        as_of_hint=date(2026, 10, 30),
    )
    out_dir = tmp_path / "out"
    result = build_metrics_db(
        out_dir,
        raw_root=raw_root,
        config_dir=config_dir,
        metrics_parsers=[],
        monkeypatch=monkeypatch,
    )
    db_path = out_dir / "etfh.duckdb"
    assert result.n_fund_metrics == 0
    assert result.n_excluded_files == 1
    versions = _query(db_path, "SELECT source_id, as_of_date, status FROM file_versions")
    assert versions.rows() == [(METRICS_SOURCE_ID, date(2026, 10, 30), "excluded")]
    events = _query(
        db_path,
        'SELECT source_id, "check", severity, excluded FROM qa_events',
    )
    assert events.rows() == [(METRICS_SOURCE_ID, "no_parser", "error", True)]
