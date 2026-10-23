"""End-to-end Phase 2 checks: fund metrics -> flows (FR-2.1) and backfill (FR-2.2).

These tests run the CLI in-process against a synthetic raw store, so they cover
the whole path a live run takes: registry and terms gate -> collector or
derived metrics source -> build -> ``fund_metrics`` -> analytics -> CLI output,
and the real backfill orchestration -> raw store -> report. Nothing here reads
the network or the wall clock.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from etfh.cli import app
from etfh.types import RawFile
from tests._build_helpers import (
    CONFIG_DIR,
    METRICS_FUND,
    FakeMetricsParser,
    make_metrics_config,
    metrics_payload,
    write_metrics_raw,
)

runner = CliRunner()

_TWO_WEEKS = [
    (date(2026, 10, 20), 24.0, 900_000.0, 21_600_000.0),
    (date(2026, 10, 21), 25.0, 1_000_000.0, 25_000_000.0),
    (date(2026, 10, 22), 25.5, 1_050_000.0, 26_775_000.0),
]


def _build_metrics_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a database whose only input is one synthetic metrics history."""
    config_dir = make_metrics_config(tmp_path)
    raw_root = tmp_path / "raw"
    write_metrics_raw(
        raw_root,
        metrics_payload(_TWO_WEEKS),
        fetched_at=datetime(2026, 10, 22, 22, tzinfo=UTC),
        as_of_hint=date(2026, 10, 22),
    )
    parser = FakeMetricsParser()
    monkeypatch.setattr("etfh.build.find_metrics_parsers", lambda raw, source: [parser])
    out_dir = tmp_path / "out"
    result = runner.invoke(
        app,
        ["build", "--raw", str(raw_root), "--out", str(out_dir), "--config", str(config_dir)],
    )
    assert result.exit_code == 0, result.output
    assert f"fund metrics {len(_TWO_WEEKS)} rows" in result.output
    return out_dir / "etfh.duckdb"


def test_metrics_to_flows_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _build_metrics_database(tmp_path, monkeypatch)

    result = runner.invoke(
        app,
        [
            "flows",
            "--fund",
            METRICS_FUND,
            "--from",
            "2026-10-21",
            "--to",
            "2026-10-22",
            "--db",
            str(db),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.output)
    assert payload["fund"] == METRICS_FUND
    method = "Δshares × NAV (estimate; ignores distributions and in-kind effects)"
    assert payload["method"] == method
    assert [row["as_of_date"] for row in payload["rows"]] == ["2026-10-21", "2026-10-22"]
    first, second = payload["rows"]
    # (1,000,000 - 900,000) x 25.0 and (1,050,000 - 1,000,000) x 25.5
    assert first["flow_estimate"] == "2500000.00"
    assert first["prev_as_of_date"] == "2026-10-20"
    assert first["gap_days"] == 1
    assert second["flow_estimate"] == "1275000.00"
    assert second["shares_outstanding"] == "1050000.0000"
    assert second["nav_per_unit"] == "25.5000"

    human = runner.invoke(
        app,
        [
            "flows",
            "--fund",
            METRICS_FUND,
            "--from",
            "2026-10-21",
            "--to",
            "2026-10-22",
            "--db",
            str(db),
        ],
    )
    assert human.exit_code == 0, human.output
    assert "2 row(s)" in human.output
    assert "method: Δshares × NAV (estimate; ignores distributions and in-kind effects)" in (
        human.output
    )


def test_built_metrics_appear_in_the_qa_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _build_metrics_database(tmp_path, monkeypatch)

    result = runner.invoke(app, ["qa", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "fund metrics:" in result.output
    assert "funds with metrics: 1" in result.output
    assert "nav_positive" not in result.output


class _CountingCollector:
    """A collector stand-in that records the fund/source pairs the CLI visits."""

    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        self.calls: list[str] = []

    def fetch(self, fund: Any, day: date, client: Any, store: Any) -> list[RawFile]:
        self.calls.append(fund.ticker)
        return []


def test_collect_aum_uses_the_real_config_and_derives_purpose_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collectors: dict[str, _CountingCollector] = {}

    def fake_collector_for(source: Any) -> _CountingCollector | None:
        if source.id == "purpose_metrics":
            return None  # derived from purpose_daily, no collector (D65)
        return collectors.setdefault(source.id, _CountingCollector(source.id))

    monkeypatch.setattr("etfh.cli.collector_for", fake_collector_for)
    monkeypatch.setenv("ETFH_USER_AGENT", "etfh-tests/1 (tests@example.com)")

    result = runner.invoke(
        app,
        [
            "collect-aum",
            "--date",
            "2026-10-23",
            "--raw",
            str(tmp_path / "raw"),
            "--config",
            str(CONFIG_DIR),
        ],
    )

    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert lines.count("derived: purpose_metrics from purpose_daily") == 1
    assert "unchanged source=bmo_metrics fund=ZEM" in lines
    assert any(line.startswith("unchanged source=evolve_metrics ") for line in lines)
    assert not any("source=purpose_daily" in line for line in lines)
    assert "purpose_metrics" not in collectors  # no collector is looked up for a derived source
    assert collectors["bmo_metrics"].calls  # the real metrics source is collected


class _HistoricalCollector:
    """A historical collector that stores a file for every quarter except March."""

    def __init__(self, source: Any) -> None:
        self.source = source
        self.source_id = source.id
        self.calls: list[tuple[str, date]] = []

    def fetch_historical(
        self, fund: Any, quarter_end: date, client: Any, store: Any
    ) -> list[RawFile]:
        self.calls.append((fund.ticker, quarter_end))
        if quarter_end.month == 3:
            return []
        raw = store.write(
            b"%PDF-1.4 synthetic quarterly disclosure look-alike\n",
            source_id=self.source.id,
            issuer=fund.issuer,
            fund_ticker=fund.ticker,
            url=f"https://example.test/{fund.ticker}-{quarter_end.isoformat()}.pdf",
            fetched_at=datetime(2026, 10, 23, 12, tzinfo=UTC),
            ext="pdf",
            content_type="application/pdf",
            as_of_hint=quarter_end,
        )
        return [raw]


def test_backfill_qpd_end_to_end_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from etfh.registry import funds_for_source, load_funds, load_sources

    sources = load_sources(CONFIG_DIR / "sources.yaml")
    funds = funds_for_source(load_funds(CONFIG_DIR / "funds.csv", sources), "vanguard_ca_qpd")
    requested = len(funds) * 4  # four calendar quarters, one of them a March quarter
    missing = len(funds)
    present = requested - missing

    monkeypatch.setattr("etfh.backfill.collector_for", lambda source: _HistoricalCollector(source))
    monkeypatch.setenv("ETFH_USER_AGENT", "etfh-tests/1 (tests@example.com)")
    raw_root = tmp_path / "raw"
    command = [
        "backfill-qpd",
        "--issuer",
        "vanguard_ca",
        "--quarters",
        "4",
        "--raw",
        str(raw_root),
        "--config",
        str(CONFIG_DIR),
    ]

    first = runner.invoke(app, command)

    assert first.exit_code == 0, first.output
    lines = first.output.strip().splitlines()
    assert sum(1 for line in lines if line.startswith("fetched ")) == present
    assert sum(1 for line in lines if line.startswith("missing ")) == missing
    assert lines[-1] == (
        f"backfill vanguard_ca: {present} fetched, 0 present, {missing} missing "
        f"({requested} fund-quarter(s))"
    )

    second = runner.invoke(app, command)

    assert second.exit_code == 0, second.output
    lines = second.output.strip().splitlines()
    assert sum(1 for line in lines if line.startswith("fetched ")) == 0
    assert sum(1 for line in lines if line.startswith("present ")) == present
    assert sum(1 for line in lines if line.startswith("missing ")) == missing
    assert lines[-1] == (
        f"backfill vanguard_ca: 0 fetched, {present} present, {missing} missing "
        f"({requested} fund-quarter(s))"
    )
