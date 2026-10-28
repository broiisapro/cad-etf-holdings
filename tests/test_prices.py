"""Price-source config and gate, and the commands that must ignore prices.

The real source is ``tiingo_prices`` (D78/D84). Nothing in the holdings
pipeline may fetch or parse it: no fund lists it, ``collect``/``collect-aum``
never look it up, and a stored price response never becomes a holdings or
fund-metrics row.
"""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from typer.testing import CliRunner

from etfh.build import build
from etfh.cli import app
from etfh.collect.registry import all_collectors, collector_for
from etfh.registry import Source, is_collectable, load_funds, load_sources
from etfh.storage import RawStore, read_meta
from etfh.types import Decision, SourceKind

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
runner = CliRunner()

TIINGO_CONDITIONS = [
    "internal research use only (free-plan licence)",
    (
        "raw prices never committed, published or redistributed; "
        "only aggregate event-study statistics may be shown"
    ),
    "stay within free-plan limits: 50 requests/hour, 1,000/day, 500 unique symbols/month",
    "API key from TIINGO_API_KEY, never logged or stored",
]


def _sources() -> dict[str, Source]:
    return load_sources(CONFIG_DIR / "sources.yaml")


class TestCommittedPricesSource:
    def test_tiingo_prices_matches_the_human_decision(self) -> None:
        source = _sources()["tiingo_prices"]
        assert source.issuer == "tiingo"
        assert source.kind is SourceKind.prices
        assert source.format.value == "json"
        assert source.update_frequency == "daily"
        assert source.supported is True
        assert source.terms.decision is Decision.allowed_with_conditions
        assert source.terms.reviewer == "Moksh Siruvani"
        assert source.terms.reviewed_on == date(2026, 10, 28)
        assert source.terms.redistribution_allowed is False
        assert source.terms.conditions == TIINGO_CONDITIONS
        assert source.terms.url == "https://www.tiingo.com/tos"
        assert "api.tiingo.com/tiingo/daily/<TICKER>/prices" in source.url_patterns[0]

    def test_no_fund_collects_prices(self) -> None:
        sources = _sources()
        funds = load_funds(CONFIG_DIR / "funds.csv", sources)
        assert all("tiingo_prices" not in fund.source_ids for fund in funds)

    def test_no_holdings_collector_claims_the_prices_source(self) -> None:
        source = _sources()["tiingo_prices"]
        assert collector_for(source) is None
        assert all(collector.source_id != "tiingo_prices" for collector in all_collectors())
        assert is_collectable(source)

    def test_pending_review_copy_is_refused(self, tmp_path: Path) -> None:
        config = scratch_config(tmp_path, decision="pending_review", conditions=[])
        source = load_sources(config / "sources.yaml")["tiingo_prices"]
        assert not is_collectable(source)

    def test_not_allowed_copy_is_refused(self, tmp_path: Path) -> None:
        config = scratch_config(tmp_path, decision="not_allowed", conditions=[])
        source = load_sources(config / "sources.yaml")["tiingo_prices"]
        assert not is_collectable(source)


def scratch_config(tmp_path: Path, *, decision: str, conditions: list[str]) -> Path:
    """Copy the committed config and override only the Tiingo terms decision."""
    config = tmp_path / "config"
    config.mkdir()
    for name in ("funds.csv", "validation.yaml", "security_overrides.csv"):
        source = CONFIG_DIR / name
        if source.exists():
            shutil.copy(source, config / name)
    data = yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8"))
    for entry in data["sources"]:
        if entry["id"] == "tiingo_prices":
            entry["terms"]["decision"] = decision
            entry["terms"]["conditions"] = conditions
    (config / "sources.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return config


class TestCollectCommandsIgnorePrices:
    def test_collect_never_asks_for_a_price_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pairs: list[tuple[str, str]] = []

        def fake_pair(fund: Any, source_id: str, *args: Any, **kwargs: Any) -> None:
            pairs.append((fund.ticker, source_id))

        monkeypatch.setattr("etfh.cli._collect_pair", fake_pair)
        monkeypatch.setenv("ETFH_USER_AGENT", "etfh-tests/1 (tests@example.com)")
        result = runner.invoke(
            app,
            [
                "collect",
                "--date",
                "2026-10-28",
                "--raw",
                str(tmp_path / "raw"),
                "--config",
                str(CONFIG_DIR),
            ],
        )
        assert result.exit_code == 0, result.output
        assert pairs
        assert all(source_id != "tiingo_prices" for _fund, source_id in pairs)

    def test_collect_aum_never_asks_for_a_price_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pairs: list[tuple[str, str]] = []

        def fake_pair(fund: Any, source_id: str, *args: Any, **kwargs: Any) -> None:
            pairs.append((fund.ticker, source_id))

        monkeypatch.setattr("etfh.cli._collect_pair", fake_pair)
        monkeypatch.setenv("ETFH_USER_AGENT", "etfh-tests/1 (tests@example.com)")
        result = runner.invoke(
            app,
            [
                "collect-aum",
                "--date",
                "2026-10-28",
                "--raw",
                str(tmp_path / "raw"),
                "--config",
                str(CONFIG_DIR),
            ],
        )
        assert result.exit_code == 0, result.output
        assert all(source_id != "tiingo_prices" for _fund, source_id in pairs)


def test_build_never_turns_a_price_response_into_holdings(tmp_path: Path) -> None:
    """A stored price JSON has no holdings parser and must not reach the tables.

    ``etfh build`` sees every file in the raw store; a prices file therefore has
    to be rejected as non-holdings rather than parsed. This asserts the part that
    matters for data integrity: no holdings and no fund-metrics rows come from
    it, and the build still completes.
    """
    raw = tmp_path / "raw"
    body = (
        b'[{"date":"2026-10-23T00:00:00.000Z","close":100.0,"adjClose":99.5,'
        b'"high":101.0,"low":98.0,"open":99.0,"volume":1000,"splitFactor":1.0,"divCash":0.0}]'
    )
    RawStore(raw).write(
        body,
        source_id="tiingo_prices",
        issuer="tiingo",
        fund_ticker="AAPL",
        url="https://api.tiingo.com/tiingo/daily/AAPL/prices?startDate=2026-10-19&endDate=2026-10-23",
        fetched_at=datetime(2026, 10, 24, 12, tzinfo=UTC),
        ext="json",
        content_type="application/json",
        http_status=200,
        as_of_hint=date(2026, 10, 23),
    )
    out = tmp_path / "out"
    result = build(raw, out, config_dir=CONFIG_DIR)
    assert result.n_holdings == 0
    assert result.n_fund_metrics == 0
    connection = duckdb.connect(str(out / "etfh.duckdb"), read_only=True)
    try:
        assert connection.execute("SELECT count(*) FROM holdings").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM fund_metrics").fetchone() == (0,)
    finally:
        connection.close()
    stored = list((raw / "tiingo" / "AAPL").glob("*/*.meta.json"))
    assert len(stored) == 1
    assert read_meta(stored[0])["source_id"] == "tiingo_prices"


def test_build_result_is_reproducible_with_a_price_file(tmp_path: Path) -> None:
    """A price file must not make two builds of the same store differ."""
    raw = tmp_path / "raw"
    RawStore(raw).write(
        b'[{"date":"2026-10-23T00:00:00.000Z","close":100.0,"adjClose":99.5}]',
        source_id="tiingo_prices",
        issuer="tiingo",
        fund_ticker="SPY",
        url="https://api.tiingo.com/tiingo/daily/SPY/prices?startDate=2026-10-19&endDate=2026-10-23",
        fetched_at=datetime(2026, 10, 24, 12, tzinfo=UTC),
        ext="json",
        content_type="application/json",
        http_status=200,
        as_of_hint=date(2026, 10, 23),
    )
    first = build(raw, tmp_path / "a", config_dir=CONFIG_DIR)
    second = build(raw, tmp_path / "b", config_dir=CONFIG_DIR)
    assert first.n_holdings == second.n_holdings == 0
    assert (tmp_path / "a" / "etfh.duckdb").read_bytes() == (
        tmp_path / "b" / "etfh.duckdb"
    ).read_bytes()
