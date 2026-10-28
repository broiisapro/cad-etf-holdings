"""Phase 3 end to end on synthetic data: build → events → event study → raw tools.

The chain the epic promises is checked on the committed fixtures, offline:

1. ``etfh build`` turns the synthetic raw store into the database;
2. ``etfh.events.large_additions`` finds the large additions in it;
3. the event study measures them with ``SyntheticPrices`` in which every event's
   day 0 carries an injected move, so the public summary must recover the sign
   and the approximate size of that move;
4. ``etfh verify-raw`` accepts the fixtures and ``etfh sync-raw`` copies them
   into a second store that verifies too.

The CLI itself is exercised for the parts a library call cannot prove: the
``--source synthetic`` wiring, the printed summary, the output files and the
public/internal split of the written numbers.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

import duckdb
import polars as pl
import pytest
from typer.testing import CliRunner

from etfh.build import build
from etfh.cli import app
from etfh.events import large_additions
from etfh.eventstudy import run_event_study
from etfh.prices import SyntheticPrices
from tests._build_helpers import CONFIG_DIR, RAW_ROOT

runner = CliRunner()
_WIDE = {"COLUMNS": "200"}
_SHOCK = 0.05
_SUMMARY_LINE = re.compile(r"events (\d+): used (\d+)")


def _weekday_on_or_after(day: date) -> date:
    """The first weekday on or after ``day`` (``SyntheticPrices`` trades weekdays)."""
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _test_symbol_for(
    security_id: str | None, security_ticker: str | None, exchange: str | None
) -> str | None:
    """Test-only ticker resolution: the security's own ticker, whatever the venue.

    The real ``etfh.prices.symbol_for`` accepts US venues only, and the
    committed fixtures are TSX-listed, so every synthetic event would otherwise
    be skipped as ``no_symbol``. ``etfh event-study --source synthetic`` does the
    same thing deliberately (demo behaviour).
    """
    del security_id, exchange
    return None if security_ticker is None else security_ticker.upper().replace(".", "-")


def _day0_shocks(events: pl.DataFrame, size: float) -> dict[tuple[str, date], float]:
    """Inject ``size`` on the day 0 of every event in ``events``."""
    shocks: dict[tuple[str, date], float] = {}
    for row in events.iter_rows(named=True):
        symbol = _test_symbol_for(row["security_id"], row["security_ticker"], row["exchange"])
        if symbol is None:
            continue
        shocks[(symbol, _weekday_on_or_after(row["known_date"]))] = size
    return shocks


@pytest.fixture(scope="module")
def synthetic_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out_dir = tmp_path_factory.mktemp("phase3-e2e")
    return build(RAW_ROOT, out_dir, config_dir=CONFIG_DIR).db_path


@pytest.fixture(scope="module")
def events(synthetic_db: Path) -> pl.DataFrame:
    connection = duckdb.connect(str(synthetic_db), read_only=True)
    try:
        return large_additions(connection)
    finally:
        connection.close()


def test_the_synthetic_store_produces_large_addition_events(events: pl.DataFrame) -> None:
    assert events.height == 39
    assert set(events["event_type"].to_list()) == {"add", "increase"}
    assert set(events["known_date"].to_list()) == {
        date(2026, 10, 30),
        date(2026, 11, 2),
        date(2026, 11, 3),
    }
    assert events["security_ticker"].null_count() == 0


def test_an_injected_day_zero_jump_is_recovered_in_sign_and_size(
    events: pl.DataFrame,
) -> None:
    """The event study must see the +5 % the fixture prices carry on day 0."""
    prices = SyntheticPrices(shocks=_day0_shocks(events, _SHOCK))
    result = run_event_study(events, prices, symbol_for=_test_symbol_for)

    summary = result.to_public_json()
    assert summary["n_events"] == events.height
    assert summary["n_used"] == 31  # 39 events, 8 collapsed as duplicates
    assert summary["n_used"] + sum(summary["skipped"].values()) == summary["n_events"]
    day0 = summary["windows"]["[0,1]"]
    # exp(0.05) - 1 = 0.0513; the walk's noise is ~1.5 % daily, so a +/-4 %
    # band over 31 events is wide enough to be stable and narrow enough to
    # prove the size, not just the sign.
    assert 0.03 < day0["mean_car"] < 0.08
    assert day0["t_stat"] is not None and day0["t_stat"] > 5
    assert day0["ci_low"] is not None and day0["ci_low"] > 0.0
    assert summary["label"] == "exploratory"


def test_a_negative_injection_is_recovered_too(events: pl.DataFrame) -> None:
    prices = SyntheticPrices(shocks=_day0_shocks(events, -_SHOCK))
    result = run_event_study(events, prices, symbol_for=_test_symbol_for)
    day0 = result.to_public_json()["windows"]["[0,1]"]
    assert -0.08 < day0["mean_car"] < -0.03
    assert day0["ci_high"] is not None and day0["ci_high"] < 0.0


def test_the_cli_wires_the_same_events_and_marks_the_result_exploratory(
    synthetic_db: Path, tmp_path: Path
) -> None:
    out = tmp_path / "study"
    result = runner.invoke(
        app,
        [
            "event-study",
            "--db",
            str(synthetic_db),
            "--raw",
            str(RAW_ROOT),
            "--out",
            str(out),
            "--source",
            "synthetic",
            "--config",
            str(CONFIG_DIR),
        ],
        env=_WIDE,
    )
    assert result.exit_code == 0, result.output
    match = _SUMMARY_LINE.search(result.output)
    assert match is not None, result.output
    assert (int(match.group(1)), int(match.group(2))) == (39, 31)
    assert "exploratory" in result.output
    assert "synthetic (offline simulation" in result.output
    assert {path.name for path in out.iterdir()} == {
        "summary.json",
        "car_by_day.csv",
        "per_event.internal.csv",
    }
    for name in ("summary.json", "car_by_day.csv", "per_event.internal.csv"):
        assert str(out / name) in result.output

    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["n_events"] == 39
    assert summary["n_used"] == 31
    assert summary["label"] == "exploratory"
    assert set(summary["skipped"]) == {
        "no_symbol",
        "no_prices",
        "short_estimation",
        "window_incomplete",
        "duplicate",
    }


def test_the_public_outputs_name_no_fund_or_security(
    synthetic_db: Path, events: pl.DataFrame, tmp_path: Path
) -> None:
    """D79/D84: identifiers stay in ``per_event.internal.csv`` only."""
    out = tmp_path / "public-safety"
    result = runner.invoke(
        app,
        [
            "event-study",
            "--db",
            str(synthetic_db),
            "--raw",
            str(RAW_ROOT),
            "--out",
            str(out),
            "--source",
            "synthetic",
            "--config",
            str(CONFIG_DIR),
        ],
        env=_WIDE,
    )
    assert result.exit_code == 0, result.output
    identifiers = set(events["fund_ticker"].to_list())
    identifiers |= {str(value) for value in events["security_id"].to_list()}
    identifiers |= set(events["security_name"].to_list())
    identifiers |= set(events["security_ticker"].to_list())

    public_text = result.output
    public_text += (out / "summary.json").read_text(encoding="utf-8")
    public_text += (out / "car_by_day.csv").read_text(encoding="utf-8")
    for identifier in identifiers:
        assert identifier not in public_text, identifier

    internal = (out / "per_event.internal.csv").read_text(encoding="utf-8")
    assert internal.splitlines()[0].startswith("fund_ticker,security_id")
    assert "ISIN:" in internal


def test_the_window_option_decides_which_sub_windows_are_reported(
    synthetic_db: Path, tmp_path: Path
) -> None:
    """``--window`` reaches the study: -3,3 cannot report [0,5] or [-5,5]."""
    out = tmp_path / "narrow"
    result = runner.invoke(
        app,
        [
            "event-study",
            "--db",
            str(synthetic_db),
            "--raw",
            str(RAW_ROOT),
            "--out",
            str(out),
            "--source",
            "synthetic",
            "--window",
            "-3,3",
            "--config",
            str(CONFIG_DIR),
        ],
        env=_WIDE,
    )
    assert result.exit_code == 0, result.output
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["window"] == [-3, 3]
    assert set(summary["windows"]) == {"[-1,1]", "[0,1]"}
    assert "CAR[0,5]" not in result.output
    assert "CAR[-1,1]" in result.output


def test_the_market_model_option_is_honoured(synthetic_db: Path, tmp_path: Path) -> None:
    """``--model market_model`` runs the OLS variant and still reports aggregates."""
    out = tmp_path / "market-model"
    result = runner.invoke(
        app,
        [
            "event-study",
            "--db",
            str(synthetic_db),
            "--raw",
            str(RAW_ROOT),
            "--out",
            str(out),
            "--source",
            "synthetic",
            "--model",
            "market_model",
            "--config",
            str(CONFIG_DIR),
        ],
        env=_WIDE,
    )
    assert result.exit_code == 0, result.output
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["model"] == "market_model"
    # The synthetic walk starts in 1990, so every event has a full estimation
    # window; nothing is skipped for want of history.
    assert summary["skipped"]["short_estimation"] == 0
    assert summary["n_used"] == 31


def test_verify_raw_accepts_the_committed_fixtures() -> None:
    result = runner.invoke(app, ["verify-raw", "--raw", str(RAW_ROOT)], env=_WIDE)
    assert result.exit_code == 0, result.output
    assert "15 file(s) checked, 0 problem(s)" in result.output


def test_sync_raw_round_trip_verifies_the_copy(tmp_path: Path) -> None:
    destination = tmp_path / "data-repo" / "raw"
    first = runner.invoke(
        app, ["sync-raw", "--from", str(RAW_ROOT), "--to", str(destination)], env=_WIDE
    )
    assert first.exit_code == 0, first.output
    assert "15 copied, 0 identical, 0 conflict(s)" in first.output

    verified = runner.invoke(app, ["verify-raw", "--raw", str(destination)], env=_WIDE)
    assert verified.exit_code == 0, verified.output
    assert "15 file(s) checked, 0 problem(s)" in verified.output

    again = runner.invoke(
        app, ["sync-raw", "--from", str(RAW_ROOT), "--to", str(destination)], env=_WIDE
    )
    assert again.exit_code == 0, again.output
    assert "0 copied, 15 identical, 0 conflict(s)" in again.output
    copied_files = [path for path in destination.rglob("*") if path.is_file()]
    assert sum(path.name.endswith(".meta.json") for path in copied_files) == 15
    assert sum(not path.name.endswith(".meta.json") for path in copied_files) == 15


def test_the_whole_phase_runs_offline(
    synthetic_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No key, no network: the synthetic path must not need either."""
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    result = runner.invoke(
        app,
        [
            "event-study",
            "--db",
            str(synthetic_db),
            "--raw",
            str(tmp_path / "no-raw"),
            "--out",
            str(tmp_path / "offline"),
            "--source",
            "synthetic",
            "--config",
            str(CONFIG_DIR),
        ],
        env=_WIDE,
    )
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "no-raw").exists()
