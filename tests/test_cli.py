"""CLI tests: collect gating, build, qa, overlap --json, changes and bad arguments."""

from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from etfh.cli import app
from etfh.collect.registry import CollectError, collector_for
from etfh.http import PoliteClient
from etfh.registry import Source, TermsNotAllowedError, load_sources
from etfh.types import RawFile
from tests._build_helpers import CONFIG_DIR, FIXTURES, RAW_ROOT

runner = CliRunner()
_WIDE = {"COLUMNS": "200"}  # keep Rich error panels from truncating in CI
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Error panels are coloured in CI; compare on the plain text."""
    return _ANSI.sub("", text)


COMMANDS = (
    "collect",
    "parse",
    "build",
    "qa",
    "overlap",
    "changes",
    "spotcheck",
    "export",
    "review",
)

_PROPOSAL_HEADER = (
    "normalized_name,example_name,country,n_funds,total_weight,"
    "candidate_security_id,candidate_name,score,confidence\n"
)
_PROPOSALS = (
    _PROPOSAL_HEADER
    + "HYDRO ONE LTD,Hydro One Ltd,CA,2,0.0345,ISIN:CA448811AA17,Hydro One Ltd,100.0,exact\n"
    + "BANK OF NOVA SCOTIA,Bank of Nova Scotia (The),CA,1,0.012,"
    + "ISIN:CA0641491075,The Bank of Nova Scotia,100.0,exact\n"
    + "TORONTO DOMINION,Toronto-Dominion Bank,CA,1,0.021,"
    + "ISIN:CA8911605092,Toronto-Dominion Bank,94.2,fuzzy\n"
)


def _review_db(tmp_path: Path, proposals: str = _PROPOSALS) -> Path:
    """A fake build directory for the review commands.

    ``etfh review list|approve`` only read ``reports/name_proposals.csv`` next
    to the database and never open the database, so an empty file at the
    database path is enough to exercise the CLI.
    """
    db = tmp_path / "etfh.duckdb"
    db.write_bytes(b"")
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "name_proposals.csv").write_text(proposals, encoding="utf-8")
    return db


_SOURCES_YAML = """
sources:
  - id: fake_daily
    issuer: fake
    kind: daily_holdings
    format: csv
    url_patterns:
      - "https://example.test/<TICKER>.csv"
    update_frequency: daily
    supported: true
    terms:
      url: "https://example.test/terms"
      reviewer: "unit test"
      reviewed_on: 2026-10-16
      decision: allowed
  - id: fake_blocked
    issuer: fake
    kind: daily_holdings
    format: csv
    url_patterns:
      - "https://example.test/blocked/<TICKER>.csv"
    update_frequency: daily
    supported: true
    terms:
      url: "https://example.test/terms"
      reviewer: "unit test"
      reviewed_on: 2026-10-16
      decision: not_allowed
  - id: fake_unsupported
    issuer: fake
    kind: daily_holdings
    format: csv
    url_patterns:
      - "https://example.test/unsupported/<TICKER>.csv"
    update_frequency: daily
    supported: false
    terms:
      url: "https://example.test/terms"
      reviewer: "unit test"
      reviewed_on: 2026-10-16
      decision: allowed
"""

_FUNDS_CSV = (
    "ticker,issuer,name,asset_class,source_ids,exchange,params\n"
    "FAKE,fake,Fake Fund,equity,fake_daily;fake_blocked;fake_unsupported,XTSE,\n"
)


class FakeCollector:
    """Records calls and returns canned results, following the Collector protocol."""

    def __init__(self, source_id: str, files: list[RawFile] | None = None) -> None:
        self.source_id = source_id
        self.files = files or []
        self.calls: list[tuple[str, date]] = []
        self.error: Exception | None = None

    def fetch(self, fund: Any, day: date, client: PoliteClient, store: Any) -> list[RawFile]:
        self.calls.append((fund.ticker, day))
        if self.error is not None:
            raise self.error
        return list(self.files)


def _write_config(directory: Path, sources_yaml: str, funds_csv: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sources.yaml").write_text(sources_yaml, encoding="utf-8")
    (directory / "funds.csv").write_text(funds_csv, encoding="utf-8")
    return directory


@pytest.fixture(scope="module")
def fixture_build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out_dir = tmp_path_factory.mktemp("cli-build")
    result = runner.invoke(
        app,
        ["build", "--raw", str(RAW_ROOT), "--out", str(out_dir), "--config", str(CONFIG_DIR)],
    )
    assert result.exit_code == 0, result.output
    return out_dir


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in COMMANDS:
        assert command in result.output


def test_build_openfigi_constructs_the_cached_lookup_and_prints_figi_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from etfh.build import BuildResult

    created: dict[str, object] = {}

    class FakeFigiLookup:
        def __init__(self, cache_dir: Path, api_key: str | None = None, **kwargs: Any) -> None:
            created["cache_dir"] = cache_dir
            created["api_key"] = api_key

    captured: dict[str, object] = {}

    def fake_run_build(
        raw: Path, out: Path, *, config_dir: Path, figi: object = None
    ) -> BuildResult:
        captured["figi"] = figi
        return BuildResult(
            out_dir=Path(out),
            db_path=Path(out) / "etfh.duckdb",
            n_file_versions=0,
            n_included_files=0,
            n_excluded_files=0,
            n_holdings=0,
            n_changes=0,
            n_qa_events=0,
            n_unmatched=0,
            n_figi=7,
        )

    monkeypatch.setattr("etfh.cli.FigiLookup", FakeFigiLookup)
    monkeypatch.setattr("etfh.cli.run_build", fake_run_build)
    monkeypatch.setenv("OPENFIGI_API_KEY", "test-key")
    result = runner.invoke(
        app,
        [
            "build",
            "--raw",
            str(tmp_path / "raw"),
            "--out",
            str(tmp_path / "out"),
            "--openfigi",
            "--figi-cache",
            str(tmp_path / "figi-cache"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert isinstance(captured["figi"], FakeFigiLookup)
    assert created == {"cache_dir": tmp_path / "figi-cache", "api_key": "test-key"}
    assert "figi rows 7" in result.output


def test_build_without_openfigi_prints_no_figi_line(fixture_build: Path) -> None:
    result = runner.invoke(app, ["build", "--raw", str(RAW_ROOT), "--out", str(fixture_build)])
    assert result.exit_code == 0, result.output
    assert "figi" not in result.output


def test_build_command_reports_counts(fixture_build: Path) -> None:
    result = runner.invoke(app, ["build", "--raw", str(RAW_ROOT), "--out", str(fixture_build)])
    assert result.exit_code == 0, result.output
    assert "file versions 15 (13 included, 2 excluded)" in result.output
    assert "holdings 294 rows" in result.output
    assert (fixture_build / "etfh.duckdb").is_file()


def test_commands_work_with_the_ci_thread_limit_set(
    fixture_build: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETFH_DUCKDB_THREADS", "1")
    result = runner.invoke(app, ["qa", "--db", str(fixture_build / "etfh.duckdb")])
    assert result.exit_code == 0, result.output
    assert "files: 15 total (13 included, 2 excluded)" in result.output


def test_qa_exits_zero_with_the_two_bad_files_excluded(fixture_build: Path) -> None:
    result = runner.invoke(app, ["qa", "--db", str(fixture_build / "etfh.duckdb")])
    assert result.exit_code == 0, result.output
    assert "files: 15 total (13 included, 2 excluded)" in result.output
    assert "SYNC" in result.output
    assert "SYND" in result.output
    assert "weight_sum" in result.output
    assert "duplicate_security" in result.output


def test_qa_max_unmatched_share_exits_three_for_a_daily_file(tmp_path: Path) -> None:
    """The CLI option is wired to ``etfh.qa``: exit 3 when a daily file exceeds it.

    The build uses a security master that resolves nothing, so every daily
    file's unmatched weight share is 100% while the quarterly SYNF file stays
    out of the check (it is a top-holdings list). The synthetic sources are
    declared ``daily_holdings`` in a copy of the config so the threshold sees
    them as daily files.
    """

    class NullSecurityMaster:
        def assign(self, frame: Any) -> Any:
            import polars as pl

            return frame.with_columns(pl.lit(None, dtype=pl.String).alias("security_id"))

    from etfh.build import build
    from tests._build_helpers import fake_validator

    config = _daily_kind_config(tmp_path)
    out_dir = tmp_path / "out"
    result_build = build(
        RAW_ROOT,
        out_dir,
        config_dir=config,
        security_master=NullSecurityMaster(),
        validator=fake_validator,
    )
    assert result_build.n_included_files == 13
    result = runner.invoke(
        app,
        ["qa", "--db", str(out_dir / "etfh.duckdb"), "--max-unmatched-share", "0.5"],
    )
    assert result.exit_code == 3, result.output
    assert "unmatched threshold 50.00% exceeded by" in result.output
    assert "SYNF" not in result.output.split("unmatched threshold")[1]


def _daily_kind_config(tmp_path: Path) -> Path:
    """A config copy whose synthetic sources are declared ``daily_holdings``."""
    config = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, config)
    data = yaml.safe_load((config / "sources.yaml").read_text(encoding="utf-8"))
    for entry in data["sources"]:
        if entry["id"] in {"syn_csv", "syn_json", "syn_xlsx"}:
            entry["kind"] = "daily_holdings"
    (config / "sources.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return config


def test_qa_max_unmatched_share_ignores_quarterly_files(fixture_build: Path) -> None:
    result = runner.invoke(
        app,
        ["qa", "--db", str(fixture_build / "etfh.duckdb"), "--max-unmatched-share", "0.5"],
    )
    assert result.exit_code == 0, result.output
    assert "unmatched threshold 50.00%: no included daily file exceeds it" in result.output


def test_qa_max_unmatched_share_out_of_range_exits_two(fixture_build: Path) -> None:
    result = runner.invoke(
        app,
        ["qa", "--db", str(fixture_build / "etfh.duckdb"), "--max-unmatched-share", "1.5"],
        env=_WIDE,
    )
    assert result.exit_code == 2
    assert "must be between 0 and 1" in _plain(result.output)


def test_overlap_json_matches_the_expected_fixture(fixture_build: Path) -> None:
    result = runner.invoke(
        app,
        [
            "overlap",
            "SYNA",
            "SYNB",
            "--asof",
            "2026-11-02",
            "--db",
            str(fixture_build / "etfh.duckdb"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    expected = json.loads((FIXTURES / "expected_overlap.json").read_text(encoding="utf-8"))
    assert payload == expected
    expected_text = (FIXTURES / "expected_overlap.json").read_text(encoding="utf-8").rstrip("\n")
    assert result.output.strip() == expected_text


def test_overlap_human_output(fixture_build: Path) -> None:
    result = runner.invoke(
        app,
        [
            "overlap",
            "SYNA",
            "SYNB",
            "--asof",
            "2026-11-02",
            "--db",
            str(fixture_build / "etfh.duckdb"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "overlap SYNA / SYNB as of 2026-11-02: 0.2484 (24.84%)" in result.output
    assert "8 common holdings" in result.output
    assert "ISIN:CA600000AA15" in result.output


def test_changes_command_lists_pairs_in_range(fixture_build: Path) -> None:
    result = runner.invoke(
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
            str(fixture_build / "etfh.duckdb"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "28 changes for SYNA between 2026-10-29 and 2026-11-02" in result.output
    assert "2026-10-29 -> 2026-10-30" in result.output
    assert "2026-10-30 -> 2026-11-02" in result.output
    assert "2026-11-02 -> 2026-11-03" not in result.output


def test_parse_writes_parsed_parquet(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["parse", "--raw", str(RAW_ROOT), "--out", str(tmp_path / "parsed")],
    )
    assert result.exit_code == 0, result.output
    assert "15 files parsed, 0 failed" in result.output
    import polars as pl

    frame = pl.read_parquet(tmp_path / "parsed" / "parsed.parquet")
    assert frame.height == 331
    assert frame.columns[0] == "fund_ticker"


class DefaultConfigCollector:
    """A collector that falls back to its imported (default) source when unbound.

    Before wave 03 the CLI passed no source to the collector, so a collector
    like this one fetched with whatever ``config/sources.yaml`` sat in the
    working directory -- the ``--config`` in use was ignored. The registry now
    binds the CLI's ``Source`` into a copy, so the collector gates its request
    with it.
    """

    source_id = "fake_daily"
    source: Source | None = None

    def __init__(self, default_source: Source) -> None:
        self.default_source = default_source
        self.calls: list[date] = []

    def fetch(self, fund: Any, day: date, client: PoliteClient, store: Any) -> list[RawFile]:
        self.calls.append(day)
        source = self.source if self.source is not None else self.default_source
        client.get("https://example.test/FAKE.csv", source=source)
        return []


def _blocked_config(tmp_path: Path) -> Path:
    allowed = _write_config(tmp_path / "allowed", _SOURCES_YAML, _FUNDS_CSV)
    blocked = tmp_path / "blocked"
    blocked.mkdir(exist_ok=True)
    shutil.copy(allowed / "funds.csv", blocked / "funds.csv")
    data = yaml.safe_load((allowed / "sources.yaml").read_text(encoding="utf-8"))
    for entry in data["sources"]:
        if entry["id"] == "fake_daily":
            entry["terms"]["decision"] = "not_allowed"
    (blocked / "sources.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return blocked


def test_bound_collector_gates_requests_with_the_looked_up_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: Any
) -> None:
    allowed = _write_config(tmp_path / "allowed", _SOURCES_YAML, _FUNDS_CSV)
    allowed_source = load_sources(allowed / "sources.yaml")["fake_daily"]
    collector = DefaultConfigCollector(allowed_source)
    monkeypatch.setattr("etfh.collect.registry.all_collectors", lambda: [collector])
    monkeypatch.setenv("ETFH_USER_AGENT", "etfh-tests/1 (tests@example.com)")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"TICKER,PORTFOLIO_MWEIGHT\nFAKE,1.0\n")

    client = PoliteClient(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=fake_clock.time,
        sleep=fake_clock.sleep,
    )
    store = SimpleNamespace(write=lambda *args, **kwargs: None)
    not_allowed = load_sources(_blocked_config(tmp_path) / "sources.yaml")["fake_daily"]
    bound = collector_for(not_allowed)
    assert bound is not None
    with pytest.raises(TermsNotAllowedError):
        bound.fetch(SimpleNamespace(ticker="FAKE"), date(2026, 11, 2), client, store)
    assert requests == []
    assert collector.calls == [date(2026, 11, 2)]  # fetched, refused before any request
    bound_allowed = collector_for(allowed_source)
    assert bound_allowed is not None
    bound_allowed.fetch(SimpleNamespace(ticker="FAKE"), date(2026, 11, 2), client, store)
    assert requests == ["/robots.txt", "/FAKE.csv"]


class _RecordingClient:
    """A PoliteClient stand-in that records (and refuses to make) requests."""

    instances: list[_RecordingClient] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.requests: list[str] = []
        self.instances.append(self)

    def get(self, url: str, **kwargs: Any) -> Any:
        self.requests.append(url)
        raise AssertionError(f"unexpected HTTP request to {url}")

    def close(self) -> None:
        pass

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        pass


def test_collect_makes_no_requests_when_the_config_source_is_not_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = _blocked_config(tmp_path)
    allowed_source = load_sources(blocked / "sources.yaml")["fake_blocked"]
    collector = DefaultConfigCollector(allowed_source)
    monkeypatch.setattr("etfh.collect.registry.all_collectors", lambda: [collector])
    monkeypatch.setattr("etfh.cli.PoliteClient", _RecordingClient)
    monkeypatch.setenv("ETFH_USER_AGENT", "etfh-tests/1 (tests@example.com)")
    _RecordingClient.instances = []
    result = runner.invoke(
        app,
        [
            "collect",
            "--date",
            "2026-11-02",
            "--raw",
            str(tmp_path / "raw"),
            "--config",
            str(blocked),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "skipped: terms source=fake_daily fund=FAKE" in result.output
    assert collector.calls == []
    assert all(instance.requests == [] for instance in _RecordingClient.instances)


def test_cli_reads_the_database_through_the_thread_aware_connect(
    fixture_build: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import etfh.cli as cli_module

    seen: list[bool] = []
    real_connect = cli_module.connect_duckdb

    def spy(path: Any, *, read_only: bool = False) -> Any:
        seen.append(read_only)
        return real_connect(path, read_only=read_only)

    monkeypatch.setattr(cli_module, "connect_duckdb", spy)
    result = runner.invoke(app, ["qa", "--db", str(fixture_build / "etfh.duckdb")])
    assert result.exit_code == 0, result.output
    assert seen == [True]


def test_spotcheck_command_prints_sampled_raw_values(fixture_build: Path) -> None:
    result = runner.invoke(
        app,
        [
            "spotcheck",
            "--raw",
            str(RAW_ROOT),
            "--db",
            str(fixture_build / "etfh.duckdb"),
            "--n",
            "2",
            "--seed",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.output.startswith("spotcheck n=2 seed=1\n")
    assert "source=syn_csv issuer=synthetic" in result.output
    assert result.output.count(" | raw=") == 2
    assert result.output.count(" | weight=") == 2


def test_spotcheck_unknown_issuer_reports_nothing_found(fixture_build: Path) -> None:
    result = runner.invoke(
        app,
        [
            "spotcheck",
            "--raw",
            str(RAW_ROOT),
            "--db",
            str(fixture_build / "etfh.duckdb"),
            "--issuer",
            "nope",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "no included files found for issuer 'nope'" in result.output


def test_spotcheck_missing_database_exits_two() -> None:
    result = runner.invoke(app, ["spotcheck", "--db", "/nonexistent/etfh.duckdb"])
    assert result.exit_code == 2
    assert "database not found" in result.output


def test_export_command_writes_aggregates_only(fixture_build: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "exports"
    result = runner.invoke(
        app,
        [
            "export",
            "--db",
            str(fixture_build / "etfh.duckdb"),
            "--out",
            str(out_dir),
            "--asof",
            "2026-11-02",
            "--funds",
            "SYNA,SYNB",
        ],
    )
    assert result.exit_code == 0, result.output
    assert str(out_dir / "overlap_matrix.csv") in result.output
    assert str(out_dir / "concentration.csv") in result.output
    assert str(out_dir / "sector_breakdown.csv") in result.output
    import polars as pl

    concentration = pl.read_csv(out_dir / "concentration.csv")
    assert concentration["fund"].to_list() == ["SYNA", "SYNB"]
    text = "".join(
        (out_dir / name).read_text(encoding="utf-8")
        for name in ("overlap_matrix.csv", "concentration.csv", "sector_breakdown.csv")
    )
    assert "Quarrystone" not in text
    assert "ISIN:" not in text


def test_export_unknown_fund_exits_two(fixture_build: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "export",
            "--db",
            str(fixture_build / "etfh.duckdb"),
            "--out",
            str(tmp_path / "exports"),
            "--asof",
            "2026-11-02",
            "--funds",
            "NOPE",
        ],
    )
    assert result.exit_code == 2
    assert "unknown fund" in result.output


def test_bad_date_exits_two_with_a_message(fixture_build: Path) -> None:
    result = runner.invoke(
        app,
        [
            "overlap",
            "SYNA",
            "SYNB",
            "--asof",
            "not-a-date",
            "--db",
            str(fixture_build / "etfh.duckdb"),
        ],
    )
    assert result.exit_code == 2
    assert "not-a-date" in result.output


def test_missing_database_exits_two_with_a_message() -> None:
    result = runner.invoke(app, ["qa", "--db", "/nonexistent/etfh.duckdb"])
    assert result.exit_code == 2
    assert "database not found" in result.output


def test_unknown_fund_exits_two(fixture_build: Path) -> None:
    result = runner.invoke(
        app,
        [
            "overlap",
            "NOPE",
            "SYNB",
            "--asof",
            "2026-11-02",
            "--db",
            str(fixture_build / "etfh.duckdb"),
        ],
    )
    assert result.exit_code == 2
    assert "unknown fund" in result.output


def test_collect_skips_not_allowed_source_from_a_config_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    shutil.copy(CONFIG_DIR / "funds.csv", config / "funds.csv")
    data = yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8"))
    for entry in data["sources"]:
        if entry["id"] == "syn_xlsx":
            entry["terms"]["decision"] = "not_allowed"
    (config / "sources.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    lookups: list[str] = []

    def fake_collector_for(source: Any) -> Any:
        lookups.append(source.id)
        return FakeCollector(source.id)

    monkeypatch.setattr("etfh.cli.collector_for", fake_collector_for)
    monkeypatch.setenv("ETFH_USER_AGENT", "etfh-tests/1 (tests@example.com)")
    result = runner.invoke(
        app,
        [
            "collect",
            "--date",
            "2026-11-02",
            "--raw",
            str(tmp_path / "raw"),
            "--config",
            str(config),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "skipped: terms source=syn_xlsx fund=SYNB" in result.output
    assert "skipped: terms source=syn_blocked fund=SYNX" in result.output
    assert "skipped: synthetic source=syn_csv fund=SYNA" in result.output
    assert "syn_xlsx" not in lookups
    assert "syn_blocked" not in lookups


def test_collect_logs_one_line_per_pair_and_only_fetches_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path / "config", _SOURCES_YAML, _FUNDS_CSV)
    collector = FakeCollector("fake_daily")
    lookups: list[str] = []

    def fake_collector_for(source: Any) -> Any:
        lookups.append(source.id)
        return collector if source.id == "fake_daily" else None

    monkeypatch.setattr("etfh.cli.collector_for", fake_collector_for)
    monkeypatch.setenv("ETFH_USER_AGENT", "etfh-tests/1 (tests@example.com)")
    result = runner.invoke(
        app,
        [
            "collect",
            "--date",
            "2026-11-02",
            "--raw",
            str(tmp_path / "raw"),
            "--config",
            str(config),
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip().splitlines() == [
        "unchanged source=fake_daily fund=FAKE",
        "skipped: terms source=fake_blocked fund=FAKE decision=not_allowed",
        "skipped: unsupported source=fake_unsupported fund=FAKE",
    ]
    assert collector.calls == [("FAKE", date(2026, 11, 2))]
    assert lookups == ["fake_daily"]


def test_collect_logs_a_single_error_line_and_still_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path / "config", _SOURCES_YAML, _FUNDS_CSV)
    collector = FakeCollector("fake_daily")
    collector.error = CollectError("fake_daily", "FAKE", "HTTP 500 after 4 attempts")

    monkeypatch.setattr("etfh.cli.collector_for", lambda source: collector)
    monkeypatch.setenv("ETFH_USER_AGENT", "etfh-tests/1 (tests@example.com)")
    result = runner.invoke(
        app,
        [
            "collect",
            "--date",
            "2026-11-02",
            "--raw",
            str(tmp_path / "raw"),
            "--config",
            str(config),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "error source=fake_daily fund=FAKE: HTTP 500 after 4 attempts" in result.output


def test_collect_today_uses_the_toronto_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path / "config", _SOURCES_YAML, _FUNDS_CSV)
    collector = FakeCollector("fake_daily")
    monkeypatch.setattr("etfh.cli.collector_for", lambda source: collector)
    monkeypatch.setenv("ETFH_USER_AGENT", "etfh-tests/1 (tests@example.com)")
    result = runner.invoke(
        app,
        ["collect", "--date", "today", "--raw", str(tmp_path / "raw"), "--config", str(config)],
    )
    assert result.exit_code == 0, result.output
    expected = datetime.now(tz=ZoneInfo("America/Toronto")).date()
    assert collector.calls[0][1] == expected


def test_overlap_json_weights_are_four_decimal_strings(fixture_build: Path) -> None:
    result = runner.invoke(
        app,
        [
            "overlap",
            "SYNA",
            "SYNB",
            "--asof",
            "2026-11-02",
            "--db",
            str(fixture_build / "etfh.duckdb"),
            "--json",
        ],
    )
    payload = json.loads(result.output)
    assert isinstance(payload["overlap"], str)
    for entry in payload["common"]:
        assert entry["weight_a"] == f"{float(entry['weight_a']):.4f}"
        assert entry["weight_b"] == f"{float(entry['weight_b']):.4f}"


def test_review_list_prints_the_proposals_in_file_order(tmp_path: Path) -> None:
    db = _review_db(tmp_path)
    result = runner.invoke(app, ["review", "list", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert f"name proposals {db.parent / 'reports' / 'name_proposals.csv'}" in result.output
    assert "3 proposal(s); 3 selected; showing 3" in result.output
    lines = result.output.splitlines()
    assert "HYDRO ONE LTD -> ISIN:CA448811AA17" in lines[2]
    assert "BANK OF NOVA SCOTIA -> ISIN:CA0641491075" in lines[3]
    assert "TORONTO DOMINION -> ISIN:CA8911605092" in lines[4]


def test_review_list_filters_by_confidence_and_limit(tmp_path: Path) -> None:
    db = _review_db(tmp_path)
    exact = runner.invoke(app, ["review", "list", "--db", str(db), "--confidence", "exact"])
    assert exact.exit_code == 0, exact.output
    assert "2 selected; showing 2" in exact.output
    assert "TORONTO DOMINION" not in exact.output
    limited = runner.invoke(app, ["review", "list", "--db", str(db), "--limit", "1"])
    assert limited.exit_code == 0, limited.output
    assert "3 selected; showing 1" in limited.output
    assert "HYDRO ONE LTD" in limited.output
    assert "BANK OF NOVA SCOTIA" not in limited.output
    bad = runner.invoke(app, ["review", "list", "--db", str(db), "--limit", "-1"], env=_WIDE)
    assert bad.exit_code == 2
    assert "must be >= 0" in _plain(bad.output)


def test_review_list_reports_a_missing_database_or_report(tmp_path: Path) -> None:
    missing_db = runner.invoke(app, ["review", "list", "--db", str(tmp_path / "nope.duckdb")])
    assert missing_db.exit_code == 2
    assert "database not found" in missing_db.output
    empty = tmp_path / "build"
    empty.mkdir()
    (empty / "etfh.duckdb").write_bytes(b"")
    missing_report = runner.invoke(app, ["review", "list", "--db", str(empty / "etfh.duckdb")])
    assert missing_report.exit_code == 2
    assert "name proposals not found" in missing_report.output


def test_review_list_rejects_an_unknown_confidence_value(tmp_path: Path) -> None:
    db = _review_db(
        tmp_path,
        _PROPOSAL_HEADER + "ODD NAME,Odd,CA,1,0.1,ISIN:CA448811AA17,Odd,100.0,guessed\n",
    )
    result = runner.invoke(app, ["review", "list", "--db", str(db)])
    assert result.exit_code == 2
    assert "unknown confidence value(s): guessed" in result.output


def test_review_approve_requires_a_reviewer(tmp_path: Path) -> None:
    db = _review_db(tmp_path)
    overrides = tmp_path / "security_overrides.csv"
    result = runner.invoke(
        app,
        [
            "review",
            "approve",
            "--db",
            str(db),
            "--confidence",
            "exact",
            "--reviewer",
            "",
            "--overrides",
            str(overrides),
        ],
    )
    assert result.exit_code == 2
    assert "--reviewer is required and must not be empty" in result.output
    assert not overrides.exists()


def test_review_approve_without_the_reviewer_option_writes_nothing(tmp_path: Path) -> None:
    db = _review_db(tmp_path)
    overrides = tmp_path / "security_overrides.csv"
    result = runner.invoke(
        app,
        [
            "review",
            "approve",
            "--db",
            str(db),
            "--confidence",
            "exact",
            "--overrides",
            str(overrides),
        ],
        env=_WIDE,
    )
    assert result.exit_code == 2
    assert "--reviewer" in _plain(result.output)
    assert not overrides.exists()


def test_review_approve_needs_a_selector(tmp_path: Path) -> None:
    db = _review_db(tmp_path)
    result = runner.invoke(app, ["review", "approve", "--db", str(db), "--reviewer", "Tester"])
    assert result.exit_code == 2
    assert "pass --confidence exact|fuzzy or at least one --name" in result.output


def test_review_approve_rejects_a_name_without_a_proposal(tmp_path: Path) -> None:
    db = _review_db(tmp_path)
    result = runner.invoke(
        app,
        [
            "review",
            "approve",
            "--db",
            str(db),
            "--name",
            "NOT A PROPOSAL",
            "--reviewer",
            "Tester",
        ],
    )
    assert result.exit_code == 2
    assert "no proposal for 'NOT A PROPOSAL'" in result.output


def test_review_approve_dry_run_prints_and_writes_nothing(tmp_path: Path) -> None:
    db = _review_db(tmp_path)
    overrides = tmp_path / "security_overrides.csv"
    result = runner.invoke(
        app,
        [
            "review",
            "approve",
            "--db",
            str(db),
            "--confidence",
            "exact",
            "--reviewer",
            "Dry Runner",
            "--overrides",
            str(overrides),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dry run: would add 2 of 2 name override(s)" in result.output
    assert "reviewer='Dry Runner'" in result.output
    assert "name *|HYDRO ONE LTD -> ISIN:CA448811AA17" in result.output
    assert "name *|BANK OF NOVA SCOTIA -> ISIN:CA0641491075" in result.output
    assert "nothing written" in result.output
    assert not overrides.exists()


def test_review_approve_appends_the_selected_overrides(tmp_path: Path) -> None:
    db = _review_db(tmp_path)
    overrides = tmp_path / "security_overrides.csv"
    result = runner.invoke(
        app,
        [
            "review",
            "approve",
            "--db",
            str(db),
            "--confidence",
            "fuzzy",
            "--reviewer",
            "Tester",
            "--overrides",
            str(overrides),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "approved 1 name override(s) for reviewer='Tester'" in result.output
    assert "name *|TORONTO DOMINION -> ISIN:CA8911605092" in result.output
    text = overrides.read_text(encoding="utf-8").splitlines()
    assert text[0] == "match_type,match_value,security_id,reviewer,reviewed_on,note"
    assert len(text) == 2
    assert text[1].startswith("name,*|TORONTO DOMINION,ISIN:CA8911605092,Tester,")
    assert "confidence=fuzzy" in text[1]
    # a second run must not duplicate what the file already holds
    again = runner.invoke(
        app,
        [
            "review",
            "approve",
            "--db",
            str(db),
            "--confidence",
            "fuzzy",
            "--reviewer",
            "Tester",
            "--overrides",
            str(overrides),
        ],
    )
    assert again.exit_code == 0, again.output
    assert "already had an override" in again.output
    assert overrides.read_text(encoding="utf-8").splitlines() == text


def test_review_approve_by_name_selects_one_proposal(tmp_path: Path) -> None:
    db = _review_db(tmp_path)
    overrides = tmp_path / "security_overrides.csv"
    result = runner.invoke(
        app,
        [
            "review",
            "approve",
            "--db",
            str(db),
            "--name",
            "BANK OF NOVA SCOTIA",
            "--reviewer",
            "Tester",
            "--overrides",
            str(overrides),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "approved 1 name override(s)" in result.output
    assert "name *|BANK OF NOVA SCOTIA -> ISIN:CA0641491075" in result.output
    assert "*|HYDRO ONE LTD" not in result.output
    text = overrides.read_text(encoding="utf-8")
    assert "*|BANK OF NOVA SCOTIA" in text
    assert "*|HYDRO ONE LTD" not in text


def test_review_approve_refuses_to_change_a_reviewed_decision(tmp_path: Path) -> None:
    db = _review_db(tmp_path)
    overrides = tmp_path / "security_overrides.csv"
    overrides.write_text(
        "match_type,match_value,security_id,reviewer,reviewed_on,note\n"
        "name,*|TORONTO DOMINION,ISIN:CA0000000001,Someone,2026-10-01,earlier review\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "review",
            "approve",
            "--db",
            str(db),
            "--confidence",
            "fuzzy",
            "--reviewer",
            "Tester",
            "--overrides",
            str(overrides),
        ],
    )
    assert result.exit_code == 2
    assert "already reviewed as a different security id" in result.output
    assert "ISIN:CA0000000001" in result.output
    # the reviewed decision is untouched
    assert overrides.read_text(encoding="utf-8").count("\n") == 2
