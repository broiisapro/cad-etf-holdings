"""CLI tests: collect gating, build, qa, overlap --json, changes and bad arguments."""

from __future__ import annotations

import json
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
COMMANDS = ("collect", "parse", "build", "qa", "overlap", "changes")

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
