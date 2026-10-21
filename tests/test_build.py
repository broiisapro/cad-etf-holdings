"""Tests for the deterministic build: versions, exclusion, changes and outputs."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pytest

from etfh.build import NAME_PROPOSAL_COLUMNS, BuildResult, build, connect
from etfh.storage import RawStore
from tests._build_helpers import CONFIG_DIR, RAW_ROOT, build_fixture_db, copy_raw_files

_SYNA_2026_10_29 = RAW_ROOT / "synthetic" / "SYNA" / "2026-10-29" / "20261029T220000Z.csv"


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, BuildResult]:
    out_dir = tmp_path_factory.mktemp("fixture-build")
    return out_dir, build_fixture_db(out_dir)


def _query(out_dir: Path, sql: str) -> pl.DataFrame:
    connection = duckdb.connect(str(out_dir / "etfh.duckdb"), read_only=True)
    try:
        return connection.execute(sql).pl()
    finally:
        connection.close()


def test_republished_file_creates_new_version_not_overwrite(
    built: tuple[Path, BuildResult],
) -> None:
    out_dir, _ = built
    versions = _query(
        out_dir,
        "SELECT file_version, known_at, raw_file_sha256, path FROM file_versions "
        "WHERE source_id = 'syn_xlsx' AND fund_ticker = 'SYNB' "
        "AND as_of_date = DATE '2026-10-30' ORDER BY file_version",
    )
    assert versions.height == 2
    rows = versions.rows()
    assert rows[0][0] == 1
    assert rows[0][1] == datetime(2026, 10, 30, 22, 0, tzinfo=UTC)
    assert rows[1][0] == 2
    assert rows[1][1] == datetime(2026, 10, 31, 14, 0, tzinfo=UTC)
    assert len(set(versions["raw_file_sha256"].to_list())) == 2
    assert rows[1][3].endswith("20261031T140000Z.xlsx")
    holdings_versions = _query(
        out_dir,
        "SELECT DISTINCT file_version, raw_file_sha256 FROM holdings "
        "WHERE fund_ticker = 'SYNB' AND as_of_date = DATE '2026-10-30' "
        "ORDER BY file_version",
    )
    assert holdings_versions["file_version"].to_list() == [1, 2]
    assert set(holdings_versions["raw_file_sha256"].to_list()) == set(
        versions["raw_file_sha256"].to_list()
    )


def test_same_sha_stored_twice_is_one_version_with_earliest_known_at(
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "raw"
    store = RawStore(raw_root)
    content = _SYNA_2026_10_29.read_bytes()
    for as_of_hint, fetched_at in (
        (date(2026, 10, 29), datetime(2026, 10, 29, 22, tzinfo=UTC)),
        (date(2026, 10, 30), datetime(2026, 10, 30, 3, tzinfo=UTC)),
    ):
        store.write(
            content,
            source_id="syn_csv",
            issuer="synthetic",
            fund_ticker="SYNA",
            url="synthetic://fixtures/SYNA.csv",
            fetched_at=fetched_at,
            ext="csv",
            content_type="text/csv",
            as_of_hint=as_of_hint,
        )
    result = build_fixture_db(tmp_path / "out", raw_root=raw_root)
    versions = _query(
        tmp_path / "out",
        "SELECT file_version, known_at FROM file_versions "
        "WHERE source_id = 'syn_csv' AND fund_ticker = 'SYNA' "
        "AND as_of_date = DATE '2026-10-29'",
    )
    assert versions.height == 1
    assert versions["file_version"].to_list() == [1]
    assert versions["known_at"][0] == datetime(2026, 10, 29, 22, tzinfo=UTC)
    assert result.n_included_files == 1
    assert result.n_holdings == 25


def test_second_build_is_byte_identical(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    copy_raw_files(
        RAW_ROOT,
        raw_root,
        {
            ("SYNA", date(2026, 10, 29)),
            ("SYNA", date(2026, 10, 30)),
            ("SYNB", date(2026, 10, 29)),
            ("SYNB", date(2026, 10, 30)),
            ("SYNC", date(2026, 10, 29)),
        },
    )
    first = build_fixture_db(tmp_path / "a", raw_root=raw_root)
    second = build_fixture_db(tmp_path / "b", raw_root=raw_root)
    assert first.n_holdings == second.n_holdings
    assert first.n_qa_events == 1
    assert first.n_changes > 0
    for table in ("file_versions", "sources", "holdings", "qa_events", "holdings_changes"):
        left = (tmp_path / "a" / "parquet" / f"{table}.parquet").read_bytes()
        right = (tmp_path / "b" / "parquet" / f"{table}.parquet").read_bytes()
        assert left == right, table
    for report in ("unmatched.csv", "fuzzy_proposals.csv", "name_proposals.csv"):
        left = (tmp_path / "a" / "reports" / report).read_bytes()
        right = (tmp_path / "b" / "reports" / report).read_bytes()
        assert left == right, report


def test_two_bad_files_are_excluded_from_holdings(
    built: tuple[Path, BuildResult],
) -> None:
    out_dir, result = built
    assert result.n_file_versions == 15
    assert result.n_included_files == 13
    assert result.n_excluded_files == 2
    excluded = _query(
        out_dir,
        "SELECT fund_ticker, status FROM file_versions "
        "WHERE status = 'excluded' ORDER BY fund_ticker",
    )
    assert excluded["fund_ticker"].to_list() == ["SYNC", "SYND"]
    assert set(excluded["status"].to_list()) == {"excluded"}
    assert (
        _query(
            out_dir,
            "SELECT COUNT(*) AS n FROM holdings WHERE fund_ticker IN ('SYNC', 'SYND')",
        )["n"][0]
        == 0
    )
    events = _query(
        out_dir,
        'SELECT "check", fund_ticker, severity, excluded FROM qa_events '
        "WHERE excluded ORDER BY fund_ticker",
    )
    assert events["fund_ticker"].to_list() == ["SYNC", "SYND"]
    assert events["check"].to_list() == ["weight_sum", "duplicate_security"]
    assert set(events["severity"].to_list()) == {"error"}
    assert set(events["excluded"].to_list()) == {True}


def test_unparseable_unknown_source_and_unknown_fund_files_are_reported(
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "raw"
    store = RawStore(raw_root)
    content = _SYNA_2026_10_29.read_bytes()
    store.write(
        content,
        source_id="syn_csv",
        issuer="synthetic",
        fund_ticker="SYNA",
        url="synthetic://fixtures/SYNA.csv",
        fetched_at=datetime(2026, 10, 29, 22, tzinfo=UTC),
        ext="csv",
        as_of_hint=date(2026, 10, 29),
    )
    store.write(
        b"free text, not a holdings file\n",
        source_id="syn_csv",
        issuer="synthetic",
        fund_ticker="SYNA",
        url="synthetic://fixtures/SYNA.txt",
        fetched_at=datetime(2026, 10, 29, 22, 30, tzinfo=UTC),
        ext="txt",
        as_of_hint=date(2026, 10, 29),
    )
    store.write(
        content,
        source_id="mystery_source",
        issuer="synthetic",
        fund_ticker="SYNA",
        url="synthetic://fixtures/mystery.csv",
        fetched_at=datetime(2026, 10, 29, 23, tzinfo=UTC),
        ext="csv",
        as_of_hint=date(2026, 10, 28),
    )
    store.write(
        content,
        source_id="syn_csv",
        issuer="synthetic",
        fund_ticker="NOPE",
        url="synthetic://fixtures/NOPE.csv",
        fetched_at=datetime(2026, 10, 29, 23, tzinfo=UTC),
        ext="csv",
        as_of_hint=date(2026, 10, 29),
    )
    result = build_fixture_db(tmp_path / "out", raw_root=raw_root)
    checks = set(
        _query(tmp_path / "out", 'SELECT DISTINCT "check" FROM qa_events')["check"].to_list()
    )
    assert checks == {"no_parser", "unknown_source", "unknown_fund"}
    assert result.n_file_versions == 4
    assert result.n_included_files == 1
    assert result.n_excluded_files == 3
    assert result.n_qa_events == 3
    versions = _query(
        tmp_path / "out",
        "SELECT source_id, fund_ticker, status FROM file_versions",
    )
    assert set(versions.rows()) == {
        ("mystery_source", "SYNA", "excluded"),
        ("syn_csv", "NOPE", "excluded"),
        ("syn_csv", "SYNA", "excluded"),
        ("syn_csv", "SYNA", "included"),
    }


def test_no_errored_raw_file_is_missing_from_file_versions(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    store = RawStore(raw_root)
    store.write(
        b"not a holdings file at all\n",
        source_id="syn_csv",
        issuer="synthetic",
        fund_ticker="SYNA",
        url="synthetic://fixtures/bad.csv",
        fetched_at=datetime(2026, 10, 29, 22, tzinfo=UTC),
        ext="csv",
        as_of_hint=date(2026, 10, 29),
    )
    store.write(
        b"no parser knows this\n",
        source_id="syn_csv",
        issuer="synthetic",
        fund_ticker="SYNA",
        url="synthetic://fixtures/bad.txt",
        fetched_at=datetime(2026, 10, 29, 22, 1, tzinfo=UTC),
        ext="txt",
        as_of_hint=date(2026, 10, 29),
    )
    result = build_fixture_db(tmp_path / "out", raw_root=raw_root)
    out_dir = tmp_path / "out"
    for raw in RawStore(raw_root).iter_raw():
        status = _query(
            out_dir,
            f"SELECT status FROM file_versions WHERE raw_file_sha256 = '{raw.sha256}'",
        )
        assert status.height >= 1, f"{raw.path} has no file_versions row"
        assert set(status["status"].to_list()) == {"excluded"}
    assert result.n_file_versions == 2
    assert result.n_excluded_files == 2
    assert (
        _query(
            out_dir,
            "SELECT COUNT(*) AS n FROM file_versions fv WHERE NOT EXISTS "
            "(SELECT 1 FROM qa_events qe WHERE qe.raw_file_sha256 = fv.raw_file_sha256 "
            "AND qe.excluded AND qe.severity = 'error')",
        )["n"][0]
        == 0
    )


def test_failed_file_without_a_hint_uses_the_fetch_date_as_asof(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    store = RawStore(raw_root)
    store.write(
        b"no parser for this\n",
        source_id="syn_csv",
        issuer="synthetic",
        fund_ticker="SYNA",
        url="synthetic://fixtures/unknown.txt",
        fetched_at=datetime(2026, 10, 30, 2, tzinfo=UTC),
        ext="txt",
        as_of_hint=None,
    )
    build_fixture_db(tmp_path / "out", raw_root=raw_root)
    versions = _query(
        tmp_path / "out",
        "SELECT as_of_date, status FROM file_versions",
    )
    assert versions.rows() == [(date(2026, 10, 29), "excluded")]


def test_sources_table_records_the_build_config(built: tuple[Path, BuildResult]) -> None:
    out_dir, _ = built
    frame = _query(out_dir, "SELECT * FROM sources ORDER BY source_id")
    assert frame.columns == ["source_id", "issuer", "kind", "format"]
    rows = {row[0]: row[1:] for row in frame.rows()}
    assert {"syn_csv", "syn_xlsx", "syn_json", "syn_pdf"} <= set(rows)
    assert rows["syn_csv"] == ("synthetic", "synthetic", "csv")
    assert rows["syn_xlsx"] == ("synthetic", "synthetic", "xlsx")
    assert rows["syn_pdf"] == ("synthetic", "synthetic", "pdf")
    assert any(kind == "daily_holdings" for _issuer, kind, _format in rows.values())
    assert any(kind == "quarterly_disclosure" for _issuer, kind, _format in rows.values())


def test_partial_list_files_get_an_info_event_not_an_exclusion(
    built: tuple[Path, BuildResult],
) -> None:
    out_dir, _ = built
    events = _query(
        out_dir,
        "SELECT fund_ticker, severity, excluded FROM qa_events WHERE \"check\" = 'partial_list'",
    )
    assert events.rows() == [("SYNF", "info", False)]
    status = _query(
        out_dir,
        "SELECT status FROM file_versions WHERE fund_ticker = 'SYNF'",
    )
    assert status["status"].to_list() == ["included"]


def test_pdf_version_known_at_is_the_stated_publication_date(
    built: tuple[Path, BuildResult],
) -> None:
    out_dir, _ = built
    known = _query(
        out_dir,
        "SELECT known_at, status FROM file_versions WHERE source_id = 'syn_pdf'",
    )
    assert known.height == 1
    assert known["known_at"][0] == datetime(2026, 10, 28, 4, 0, tzinfo=UTC)
    assert known["status"][0] == "included"


def test_changes_cover_consecutive_dates_with_threshold(
    built: tuple[Path, BuildResult],
) -> None:
    out_dir, _ = built
    pairs = _query(
        out_dir,
        "SELECT DISTINCT from_as_of, to_as_of FROM holdings_changes "
        "WHERE fund_ticker = 'SYNA' ORDER BY from_as_of",
    )
    assert list(zip(pairs["from_as_of"], pairs["to_as_of"], strict=True)) == [
        (date(2026, 10, 29), date(2026, 10, 30)),
        (date(2026, 10, 30), date(2026, 11, 2)),
        (date(2026, 11, 2), date(2026, 11, 3)),
    ]
    latest = _query(
        out_dir,
        "SELECT change_type FROM holdings_changes WHERE fund_ticker = 'SYNA' "
        "AND from_as_of = DATE '2026-11-02' AND to_as_of = DATE '2026-11-03'",
    )
    assert {"add", "drop"} <= set(latest["change_type"].to_list())
    deltas = _query(
        out_dir,
        "SELECT delta, weight_before, weight_after FROM holdings_changes "
        "WHERE change_type = 'weight_change'",
    )
    assert deltas.height > 0
    for delta, before, after in deltas.rows():
        assert abs(delta) >= 0.001
        assert abs((after - before) - delta) < 1e-12
    assert (
        _query(
            out_dir,
            "SELECT COUNT(*) AS n FROM holdings_changes WHERE change_type = 'add' "
            "AND weight_before IS NOT NULL",
        )["n"][0]
        == 0
    )
    assert (
        _query(
            out_dir,
            "SELECT COUNT(*) AS n FROM holdings_changes WHERE change_type = 'drop' "
            "AND weight_after IS NOT NULL",
        )["n"][0]
        == 0
    )


def test_every_table_is_written_in_its_documented_order(
    built: tuple[Path, BuildResult],
) -> None:
    out_dir, _ = built
    keys = {
        "sources": ["source_id"],
        "file_versions": ["source_id", "fund_ticker", "as_of_date", "file_version"],
        "holdings": [
            "fund_ticker",
            "as_of_date",
            "file_version",
            "security_id",
            "security_name",
            "security_ticker",
            "isin",
            "cusip",
            "currency",
            "weight",
        ],
        "qa_events": [
            "source_id",
            "fund_ticker",
            "as_of_date",
            "check",
            "detail",
            "raw_file_sha256",
        ],
        "holdings_changes": [
            "fund_ticker",
            "from_as_of",
            "to_as_of",
            "change_type",
            "security_id",
        ],
    }
    for table, sort_key in keys.items():
        frame = pl.read_parquet(out_dir / "parquet" / f"{table}.parquet")
        assert frame.equals(frame.sort(sort_key)), table


def test_name_proposals_report_offers_reviewed_names(
    built: tuple[Path, BuildResult],
) -> None:
    """The build proposes names for unmatched holdings but applies nothing."""
    out_dir, result = built
    frame = pl.read_csv(out_dir / "reports" / "name_proposals.csv")
    assert frame.columns == list(NAME_PROPOSAL_COLUMNS)
    assert result.n_name_proposals == frame.height > 0
    assert set(frame["confidence"].to_list()) <= {"exact", "fuzzy"}
    assert frame["candidate_security_id"].null_count() == 0
    assert frame["normalized_name"].null_count() == 0
    # proposals are review input only: the ids they name are not applied here
    assert result.n_unmatched > 0


class _FigiMaster:
    """Security master stand-in that emits FIGI ids and records prefetch calls."""

    def __init__(self) -> None:
        self.prefetched: list[pl.DataFrame] = []

    def prefetch(self, frames: object) -> None:
        self.prefetched.extend(list(frames))  # type: ignore[call-overload]

    def assign(self, frame: pl.DataFrame) -> pl.DataFrame:
        return frame.with_columns(
            pl.Series("security_id", ["FIGI:BBG000000001"] * frame.height, dtype=pl.String)
        )


def test_openfigi_prefetches_frames_and_counts_figi_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_root = tmp_path / "raw"
    RawStore(raw_root).write(
        _SYNA_2026_10_29.read_bytes(),
        source_id="syn_csv",
        issuer="synthetic",
        fund_ticker="SYNA",
        url="synthetic://fixtures/SYNA.csv",
        fetched_at=datetime(2026, 10, 29, 22, tzinfo=UTC),
        ext="csv",
        as_of_hint=date(2026, 10, 29),
    )
    master = _FigiMaster()
    result = build(
        raw_root,
        tmp_path / "out",
        config_dir=CONFIG_DIR,
        security_master=master,
        validator=lambda *args, **kwargs: [],
        figi=object(),  # type: ignore[arg-type]
    )
    assert len(master.prefetched) == 1
    assert master.prefetched[0].height == 25
    assert result.n_figi == result.n_holdings == 25
    figi_rows = _query(
        tmp_path / "out",
        "SELECT COUNT(*) AS n FROM holdings WHERE security_id LIKE 'FIGI:%'",
    )
    assert figi_rows["n"][0] == 25


def test_build_without_openfigi_skips_prefetch(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    RawStore(raw_root).write(
        _SYNA_2026_10_29.read_bytes(),
        source_id="syn_csv",
        issuer="synthetic",
        fund_ticker="SYNA",
        url="synthetic://fixtures/SYNA.csv",
        fetched_at=datetime(2026, 10, 29, 22, tzinfo=UTC),
        ext="csv",
        as_of_hint=date(2026, 10, 29),
    )
    master = _FigiMaster()
    build(
        raw_root,
        tmp_path / "out",
        config_dir=CONFIG_DIR,
        security_master=master,
        validator=lambda *args, **kwargs: [],
    )
    assert master.prefetched == []


def test_build_openfigi_assigns_figi_ids_with_a_cached_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: Any
) -> None:
    import httpx

    from etfh.secmaster.openfigi import FigiLookup

    requests_seen: list[list[dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        jobs = json.loads(request.content)
        requests_seen.append(jobs)
        entries = []
        for job in jobs:
            digest = hashlib.sha1(job["idValue"].encode(), usedforsecurity=False).hexdigest()
            entries.append(
                {"data": [{"compositeFIGI": f"BBG{digest[:10].upper()}", "exchCode": "CN"}]}
            )
        return httpx.Response(200, json=entries)

    monkeypatch.setenv("ETFH_USER_AGENT", "etfh-tests/1 (tests@example.com)")
    figi = FigiLookup(
        tmp_path / "figi-cache",
        transport=httpx.MockTransport(handler),
        clock=fake_clock.time,
        sleep=fake_clock.sleep,
    )
    result = build(RAW_ROOT, tmp_path / "out", config_dir=CONFIG_DIR, figi=figi)
    assert len(requests_seen) >= 1
    assert all(
        job.get("idType") in {"ID_ISIN", "TICKER"} for batch in requests_seen for job in batch
    )
    assert result.n_figi > 0
    assert result.n_figi < result.n_holdings  # cash and identifier-less rows keep their ids
    ids = _query(
        tmp_path / "out",
        "SELECT security_id FROM holdings WHERE security_id LIKE 'FIGI:%'",
    )["security_id"].to_list()
    assert len(ids) == result.n_figi
    assert all(identifier.startswith("FIGI:BBG") for identifier in ids)
    assert any(path.suffix == ".json" for path in (tmp_path / "figi-cache").iterdir())


def test_default_build_reports_no_figi_rows(tmp_path: Path) -> None:
    result = build(RAW_ROOT, tmp_path / "out", config_dir=CONFIG_DIR)
    assert result.n_figi == 0
    frame = _query(
        tmp_path / "out", "SELECT COUNT(*) AS n FROM holdings WHERE security_id LIKE 'FIGI:%'"
    )
    assert frame["n"][0] == 0


def test_connect_honours_etfh_duckdb_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETFH_DUCKDB_THREADS", "2")
    connection = connect(":memory:")
    try:
        assert connection.execute("SELECT current_setting('threads')").fetchone() == (2,)
    finally:
        connection.close()
    monkeypatch.delenv("ETFH_DUCKDB_THREADS")
    connection = connect(":memory:")
    try:
        default = connection.execute("SELECT current_setting('threads')").fetchone()
    finally:
        connection.close()
    assert default is not None


def test_connect_rejects_a_bad_thread_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETFH_DUCKDB_THREADS", "many")
    with pytest.raises(ValueError, match="ETFH_DUCKDB_THREADS"):
        connect(":memory:")
    monkeypatch.setenv("ETFH_DUCKDB_THREADS", "0")
    with pytest.raises(ValueError, match="ETFH_DUCKDB_THREADS"):
        connect(":memory:")


def test_build_opens_duckdb_through_the_connect_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import etfh.build as build_module

    raw_root = tmp_path / "raw"
    RawStore(raw_root).write(
        _SYNA_2026_10_29.read_bytes(),
        source_id="syn_csv",
        issuer="synthetic",
        fund_ticker="SYNA",
        url="synthetic://fixtures/SYNA.csv",
        fetched_at=datetime(2026, 10, 29, 22, tzinfo=UTC),
        ext="csv",
        as_of_hint=date(2026, 10, 29),
    )
    seen: list[tuple[str, bool]] = []
    real_connect = build_module.connect

    def spy(path: str | Path, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
        seen.append((Path(path).name, read_only))
        return real_connect(path, read_only=read_only)

    monkeypatch.setattr(build_module, "connect", spy)
    build_fixture_db(tmp_path / "out", raw_root=raw_root)
    assert seen == [("etfh.duckdb", False)]


def test_reports_and_result_counts_match_the_tables(
    built: tuple[Path, BuildResult],
) -> None:
    out_dir, result = built
    reports = out_dir / "reports"
    unmatched_lines = (reports / "unmatched.csv").read_text(encoding="utf-8").splitlines()
    assert unmatched_lines[0] == (
        "fund_ticker,as_of_date,security_name,security_ticker,exchange,source_id,raw_file_sha256"
    )
    assert len(unmatched_lines) - 1 == result.n_unmatched
    proposals_lines = (reports / "fuzzy_proposals.csv").read_text(encoding="utf-8").splitlines()
    assert proposals_lines[0] == "security_name,candidate_security_id,candidate_name,score"
    assert len(proposals_lines) > 1
    assert any("ISIN:" in line for line in proposals_lines[1:])
    counts = {
        table: _query(out_dir, f"SELECT COUNT(*) AS n FROM {table}")["n"][0]
        for table in ("file_versions", "holdings", "qa_events", "holdings_changes")
    }
    assert result.n_file_versions == counts["file_versions"]
    assert result.n_holdings == counts["holdings"]
    assert result.n_qa_events == counts["qa_events"]
    assert result.n_changes == counts["holdings_changes"]
    assert result.n_included_files + result.n_excluded_files == result.n_file_versions
    assert result.db_path == out_dir / "etfh.duckdb"
    assert result.db_path.is_file()
