"""Tests for the exploratory event study (issue #188).

The price source is a fake defined in this file: ``etfh.prices`` is written by
another issue of the same wave and cannot be imported here.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from etfh.eventstudy import (
    EVENT_COLUMNS,
    SKIP_REASONS,
    EventStudyResult,
    run_event_study,
    write_outputs,
)

PRICE_SCHEMA: dict[str, Any] = {
    "symbol": pl.String,
    "date": pl.Date,
    "close": pl.Float64,
}

EVENT_SCHEMA: dict[str, Any] = {
    "fund_ticker": pl.String,
    "security_id": pl.String,
    "security_name": pl.String,
    "security_ticker": pl.String,
    "exchange": pl.String,
    "event_type": pl.String,
    "portfolio_date": pl.Date,
    "known_date": pl.Date,
    "weight_before": pl.Float64,
    "weight_after": pl.Float64,
    "delta": pl.Float64,
    "source_kind": pl.String,
}


class FakePrices:
    """In-file stand-in for the ``PriceSource`` protocol.

    Filters a fixed frame like a real source would: unknown symbols are absent,
    dates outside the requested range are dropped, rows are sorted.
    """

    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame
        self.calls: list[tuple[tuple[str, ...], date, date]] = []

    def daily_closes(self, symbols: Sequence[str], start: date, end: date) -> pl.DataFrame:
        self.calls.append((tuple(symbols), start, end))
        if self.frame.height == 0:
            return self.frame
        return self.frame.filter(
            pl.col("symbol").is_in(list(symbols)) & pl.col("date").is_between(start, end)
        ).sort(["symbol", "date"])


def _weekdays(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _days() -> list[date]:
    return _weekdays(date(2026, 1, 5), date(2026, 10, 30))


def _close_path(returns: list[float | None], start: float = 100.0) -> list[float]:
    closes = [start]
    for value in returns[1:]:
        closes.append(closes[-1] * (1.0 + (0.0 if value is None else value)))
    return closes


def _flat_benchmark(days: Sequence[date]) -> tuple[list[float | None], list[float]]:
    returns: list[float | None] = [None] + [0.0] * (len(days) - 1)
    return returns, _close_path(returns)


def _noisy_benchmark(days: Sequence[date]) -> tuple[list[float | None], list[float]]:
    returns: list[float | None] = [None] + [
        0.004 * math.sin(0.37 * index) for index in range(1, len(days))
    ]
    return returns, _close_path(returns)


def _symbol_path(
    days: Sequence[date],
    benchmark_returns: Sequence[float | None],
    *,
    alpha: float = 0.0,
    beta: float = 0.0,
    shocks: Mapping[int, float] | None = None,
) -> list[float]:
    injected = shocks or {}
    returns: list[float | None] = [None]
    for index in range(1, len(days)):
        benchmark = benchmark_returns[index]
        returns.append(
            alpha + beta * (0.0 if benchmark is None else benchmark) + injected.get(index, 0.0)
        )
    return _close_path(returns)


def _prices(series: Mapping[str, tuple[Sequence[date], Sequence[float]]]) -> pl.DataFrame:
    rows = [
        {"symbol": symbol, "date": day, "close": close}
        for symbol, (days, closes) in series.items()
        for day, close in zip(days, closes, strict=True)
    ]
    return pl.DataFrame(rows, schema=PRICE_SCHEMA).sort(["symbol", "date"])


def _event(
    fund: str,
    security_id: str,
    known_date: date,
    *,
    name: str = "Fictional Holdings Corp",
    ticker: str = "FCORP",
    exchange: str = "XNAS",
    event_type: str = "add",
) -> dict[str, Any]:
    return {
        "fund_ticker": fund,
        "security_id": security_id,
        "security_name": name,
        "security_ticker": ticker,
        "exchange": exchange,
        "event_type": event_type,
        "portfolio_date": known_date,
        "known_date": known_date,
        "weight_before": 0.0,
        "weight_after": 0.012,
        "delta": 0.012,
        "source_kind": "daily",
    }


def _events(*rows: dict[str, Any]) -> pl.DataFrame:
    return pl.DataFrame(list(rows), schema=EVENT_SCHEMA)


def _mapper(mapping: Mapping[str, str]) -> Any:
    def resolve(
        security_id: str | None, security_ticker: str | None, exchange: str | None
    ) -> str | None:
        return None if security_id is None else mapping.get(security_id)

    return resolve


def _day_index(days: Sequence[date], day: date) -> int:
    return list(days).index(day)


# --------------------------------------------------------------------------- #
# Known effects and the two models
# --------------------------------------------------------------------------- #


def test_market_adjusted_recovers_injected_abnormal_return() -> None:
    days = _days()
    benchmark_returns, benchmark_closes = _flat_benchmark(days)
    index = 150
    known = days[index]
    series = {"SPY": (days, benchmark_closes)}
    mapping = {}
    rows = []
    for offset, ticker in enumerate(("AAA", "BBB", "CCC")):
        security_id = f"ISIN:TEST0000000{offset}"
        mapping[security_id] = ticker
        series[ticker] = (
            days,
            _symbol_path(days, benchmark_returns, shocks={index: 0.02}),
        )
        rows.append(_event("FNDA", security_id, known))

    result = run_event_study(
        _events(*rows), FakePrices(_prices(series)), symbol_for=_mapper(mapping)
    )

    assert result.summary["n_events"] == 3
    assert result.summary["n_used"] == 3
    assert result.summary["model"] == "market_adjusted"
    assert result.summary["windows"]["[0,1]"]["mean_car"] == pytest.approx(0.02, abs=1e-12)
    assert result.summary["windows"]["[-1,1]"]["mean_car"] == pytest.approx(0.02, abs=1e-12)
    assert result.summary["windows"]["[-5,5]"]["mean_car"] == pytest.approx(0.02, abs=1e-12)
    by_day = result.car_by_day
    assert by_day.filter(pl.col("relative_day") == 0)["mean_ar"].item() == pytest.approx(
        0.02, abs=1e-12
    )
    assert by_day.filter(pl.col("relative_day") == 1)["mean_ar"].item() == pytest.approx(
        0.0, abs=1e-15
    )
    assert by_day["n"].to_list() == [3] * 11


def test_market_model_recovers_beta_and_alpha() -> None:
    days = _days()
    benchmark_returns, benchmark_closes = _noisy_benchmark(days)
    index = 150
    known = days[index]
    path = _symbol_path(days, benchmark_returns, alpha=0.0002, beta=1.4, shocks={index: 0.02})
    frame = _prices({"SPY": (days, benchmark_closes), "AAA": (days, path)})
    events = _events(_event("FNDA", "ISIN:AAA", known))

    result = run_event_study(
        events,
        FakePrices(frame),
        symbol_for=_mapper({"ISIN:AAA": "AAA"}),
        model="market_model",
    )

    assert result.summary["n_used"] == 1
    row = result.per_event.row(0, named=True)
    assert row["beta"] == pytest.approx(1.4, abs=1e-9)
    assert row["alpha"] == pytest.approx(0.0002, abs=1e-9)
    assert row["estimation_days"] == 110
    assert result.summary["windows"]["[0,1]"]["mean_car"] == pytest.approx(0.02, abs=1e-9)


def test_market_model_requires_benchmark_variation() -> None:
    days = _days()
    _, benchmark_closes = _flat_benchmark(days)
    index = 150
    frame = _prices(
        {
            "SPY": (days, benchmark_closes),
            "AAA": (days, _symbol_path(days, [None] * len(days), shocks={index: 0.01})),
        }
    )
    result = run_event_study(
        _events(_event("FNDA", "ISIN:AAA", days[index])),
        FakePrices(frame),
        symbol_for=_mapper({"ISIN:AAA": "AAA"}),
        model="market_model",
    )
    assert result.summary["n_used"] == 0
    assert result.summary["skipped"]["short_estimation"] == 1


# --------------------------------------------------------------------------- #
# Event dating and duplicate handling
# --------------------------------------------------------------------------- #


def test_day_zero_is_the_first_trading_day_on_or_after_known_date() -> None:
    days = _days()
    benchmark_returns, benchmark_closes = _flat_benchmark(days)
    price_index = 150
    saturday = date(2026, 8, 8)
    assert saturday.weekday() == 5
    frame = _prices(
        {
            "SPY": (days, benchmark_closes),
            "AAA": (days, _symbol_path(days, benchmark_returns, shocks={price_index: 0.02})),
        }
    )
    result = run_event_study(
        _events(_event("FNDA", "ISIN:AAA", saturday)),
        FakePrices(frame),
        symbol_for=_mapper({"ISIN:AAA": "AAA"}),
    )
    assert result.summary["n_used"] == 1
    assert result.per_event.row(0, named=True)["event_day"] == date(2026, 8, 10)


def test_duplicate_symbol_and_day_count_once_and_keep_the_first_fund() -> None:
    days = _days()
    benchmark_returns, benchmark_closes = _flat_benchmark(days)
    index = 150
    known = days[index]
    path = _symbol_path(days, benchmark_returns, shocks={index: 0.02})
    frame = _prices(
        {
            "SPY": (days, benchmark_closes),
            "AAA": (days, path),
            "BBB": (days, _symbol_path(days, benchmark_returns, shocks={index: 0.01})),
        }
    )
    events = _events(
        _event("FNDB", "ISIN:SAME", known),
        _event("FNDA", "ISIN:SAME", known),
        _event("FNDC", "ISIN:OTHER", known),
    )
    result = run_event_study(
        events,
        FakePrices(frame),
        symbol_for=_mapper({"ISIN:SAME": "AAA", "ISIN:OTHER": "BBB"}),
    )
    assert result.summary["n_events"] == 3
    assert result.summary["n_used"] == 2
    assert result.summary["skipped"]["duplicate"] == 1
    assert result.per_event["fund_ticker"].to_list() == ["FNDA", "FNDC"]
    assert "duplicate" in result.summary["notes"][0]


# --------------------------------------------------------------------------- #
# Skip reasons
# --------------------------------------------------------------------------- #


def test_events_without_a_symbol_are_skipped() -> None:
    days = _days()
    benchmark_returns, benchmark_closes = _flat_benchmark(days)
    index = 150
    frame = _prices(
        {
            "SPY": (days, benchmark_closes),
            "AAA": (days, _symbol_path(days, benchmark_returns, shocks={index: 0.02})),
        }
    )
    events = _events(
        _event("FNDA", "TKR:XTSE:NOPE", days[index]),
        _event("FNDB", "ISIN:AAA", days[index]),
    )
    result = run_event_study(events, FakePrices(frame), symbol_for=_mapper({"ISIN:AAA": "AAA"}))
    assert result.summary["n_used"] == 1
    assert result.summary["skipped"]["no_symbol"] == 1


def test_symbols_without_prices_are_skipped() -> None:
    days = _days()
    benchmark_returns, benchmark_closes = _flat_benchmark(days)
    index = 150
    path = _symbol_path(days, benchmark_returns, shocks={index: 0.02})
    frame = _prices({"SPY": (days, benchmark_closes), "AAA": (days, path)})
    events = _events(
        _event("FNDA", "ISIN:AAA", days[index]),
        _event("FNDB", "ISIN:ABSENT", days[index]),
    )
    result = run_event_study(
        events,
        FakePrices(frame),
        symbol_for=_mapper({"ISIN:AAA": "AAA", "ISIN:ABSENT": "ABSENT"}),
    )
    assert result.summary["n_used"] == 1
    assert result.summary["skipped"]["no_prices"] == 1


def test_short_price_history_is_skipped_as_short_estimation() -> None:
    days = _days()
    benchmark_returns, benchmark_closes = _noisy_benchmark(days)
    index = 150
    path = _symbol_path(days, benchmark_returns, beta=1.0, shocks={index: 0.02})
    short_days = days[index - 20 :]
    short_closes = path[index - 20 :]
    frame = _prices({"SPY": (days, benchmark_closes), "AAA": (short_days, short_closes)})
    result = run_event_study(
        _events(_event("FNDA", "ISIN:AAA", days[index])),
        FakePrices(frame),
        symbol_for=_mapper({"ISIN:AAA": "AAA"}),
        model="market_model",
    )
    assert result.summary["n_used"] == 0
    assert result.summary["skipped"]["short_estimation"] == 1


def test_windows_that_run_past_the_grid_are_skipped() -> None:
    days = _days()
    _, benchmark_closes = _flat_benchmark(days)
    frame = _prices({"SPY": (days, benchmark_closes)})
    result = run_event_study(
        _events(_event("FNDA", "ISIN:AAA", days[-2])),
        FakePrices(frame),
        symbol_for=_mapper({"ISIN:AAA": "AAA"}),
    )
    assert result.summary["n_used"] == 0
    assert result.summary["skipped"]["window_incomplete"] == 1
    assert all(stats["n"] == 0 for stats in result.summary["windows"].values())


def test_events_far_beyond_the_benchmark_are_skipped() -> None:
    days = _days()
    _, benchmark_closes = _flat_benchmark(days)
    frame = _prices({"SPY": (days, benchmark_closes)})
    result = run_event_study(
        _events(_event("FNDA", "ISIN:AAA", days[-1] + timedelta(days=90))),
        FakePrices(frame),
        symbol_for=_mapper({"ISIN:AAA": "AAA"}),
    )
    assert result.summary["skipped"]["window_incomplete"] == 1


def test_every_skip_reason_is_counted_with_a_zero_baseline() -> None:
    result = run_event_study(
        _events(), FakePrices(pl.DataFrame(schema=PRICE_SCHEMA)), symbol_for=_mapper({})
    )
    assert tuple(result.summary["skipped"]) == SKIP_REASONS
    assert all(count == 0 for count in result.summary["skipped"].values())


def test_used_and_skipped_events_add_up_to_the_input() -> None:
    days = _days()
    benchmark_returns, benchmark_closes = _noisy_benchmark(days)
    index = 150
    known = days[index]
    good = _symbol_path(days, benchmark_returns, beta=1.0, shocks={index: 0.02})
    short_days = days[index - 20 :]
    frame = _prices(
        {
            "SPY": (days, benchmark_closes),
            "AAA": (days, good),
            "SHORT": (short_days, _symbol_path(days, benchmark_returns, beta=1.0)[index - 20 :]),
        }
    )
    events = _events(
        _event("FNDA", "ISIN:GOOD", known),
        _event("FNDB", "ISIN:GOOD", known),
        _event("FNDC", "TKR:XTSE:NONE", known),
        _event("FNDD", "ISIN:ABSENT", known),
        _event("FNDE", "ISIN:GOOD", days[-2]),
        _event("FNDF", "ISIN:SHORT", known),
        _event("FNDG", "ISIN:GOOD", None),
    )
    mapping = {"ISIN:GOOD": "AAA", "ISIN:ABSENT": "ABSENT", "ISIN:SHORT": "SHORT"}
    result = run_event_study(
        events,
        FakePrices(frame),
        symbol_for=_mapper(mapping),
        model="market_model",
    )
    assert result.summary["n_events"] == 7
    assert result.summary["n_used"] == 1
    assert result.summary["skipped"] == {
        "no_symbol": 1,
        "no_prices": 1,
        "short_estimation": 1,
        "window_incomplete": 2,
        "duplicate": 1,
    }
    assert sum(result.summary["skipped"].values()) + result.summary["n_used"] == 7


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def _varied_result(seed: int = 7, bootstrap: int = 500) -> EventStudyResult:
    days = _days()
    benchmark_returns, benchmark_closes = _flat_benchmark(days)
    index = 150
    known = days[index]
    shocks = [0.03, -0.01, 0.015, 0.0, 0.005]
    series = {"SPY": (days, benchmark_closes)}
    mapping = {}
    rows = []
    for offset, shock in enumerate(shocks):
        ticker = f"S{offset}"
        security_id = f"ISIN:VAR{offset}"
        mapping[security_id] = ticker
        series[ticker] = (
            days,
            _symbol_path(days, benchmark_returns, shocks={index: shock}),
        )
        rows.append(_event("FNDA", security_id, known))
    return run_event_study(
        _events(*rows),
        FakePrices(_prices(series)),
        symbol_for=_mapper(mapping),
        bootstrap=bootstrap,
        seed=seed,
    )


def test_bootstrap_interval_is_deterministic_for_a_seed() -> None:
    first = _varied_result(seed=7)
    second = _varied_result(seed=7)
    other = _varied_result(seed=11)
    for label in first.summary["windows"]:
        left = first.summary["windows"][label]
        right = second.summary["windows"][label]
        assert (left["ci_low"], left["ci_high"]) == (right["ci_low"], right["ci_high"])
    assert (
        first.summary["windows"]["[0,1]"]["ci_low"],
        first.summary["windows"]["[0,1]"]["ci_high"],
    ) != (
        other.summary["windows"]["[0,1]"]["ci_low"],
        other.summary["windows"]["[0,1]"]["ci_high"],
    )


def test_bootstrap_interval_brackets_the_cross_sectional_mean() -> None:
    result = _varied_result()
    for label, stats in result.summary["windows"].items():
        assert stats["n"] == 5, label
        assert stats["ci_low"] <= stats["mean_car"] <= stats["ci_high"], label
        assert stats["ci_low"] < stats["ci_high"], label


def test_t_statistic_uses_the_cross_sectional_standard_error() -> None:
    result = _varied_result()
    values = [0.03, -0.01, 0.015, 0.0, 0.005]
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    expected = mean / math.sqrt(variance / len(values))
    stats = result.summary["windows"]["[0,1]"]
    assert stats["mean_car"] == pytest.approx(mean, abs=1e-12)
    assert stats["t_stat"] == pytest.approx(expected, abs=1e-6)


def test_car_by_day_is_cumulative_and_public_safe() -> None:
    result = _varied_result()
    frame = result.car_by_day
    assert frame.columns == ["relative_day", "mean_ar", "mean_car", "n"]
    assert frame["relative_day"].to_list() == list(range(-5, 6))
    running = 0.0
    for row in frame.iter_rows(named=True):
        running += row["mean_ar"]
        assert row["mean_car"] == pytest.approx(running, abs=1e-12)


def test_only_sub_windows_inside_the_requested_window_are_reported() -> None:
    days = _days()
    benchmark_returns, benchmark_closes = _flat_benchmark(days)
    index = 150
    frame = _prices(
        {
            "SPY": (days, benchmark_closes),
            "AAA": (days, _symbol_path(days, benchmark_returns, shocks={index: 0.02})),
        }
    )
    result = run_event_study(
        _events(_event("FNDA", "ISIN:AAA", days[index])),
        FakePrices(frame),
        symbol_for=_mapper({"ISIN:AAA": "AAA"}),
        window=(0, 3),
    )
    assert list(result.summary["windows"]) == ["[0,1]"]
    row = result.per_event.row(0, named=True)
    assert row["car_0_1"] == pytest.approx(0.02, abs=1e-12)
    assert row["car_m1_1"] is None
    assert row["car_0_5"] is None


# --------------------------------------------------------------------------- #
# Empty and invalid inputs
# --------------------------------------------------------------------------- #


def test_empty_events_give_a_valid_empty_result_without_fetching() -> None:
    prices = FakePrices(pl.DataFrame(schema=PRICE_SCHEMA))
    result = run_event_study(_events(), prices, symbol_for=_mapper({}))
    assert prices.calls == []
    assert result.summary["label"] == "exploratory"
    assert result.summary["n_events"] == 0
    assert result.summary["n_used"] == 0
    assert set(result.summary["windows"]) == {"[-1,1]", "[0,1]", "[0,5]", "[-5,5]"}
    assert all(stats["n"] == 0 for stats in result.summary["windows"].values())
    assert all(stats["mean_car"] is None for stats in result.summary["windows"].values())
    assert result.per_event.height == 0
    assert result.per_event.columns == list(
        [
            "fund_ticker",
            "security_id",
            "security_name",
            "security_ticker",
            "exchange",
            "event_type",
            "portfolio_date",
            "known_date",
            "event_day",
            "symbol",
            "estimation_days",
            "alpha",
            "beta",
            "car_m1_1",
            "car_0_1",
            "car_0_5",
            "car_m5_5",
        ]
    )
    assert result.car_by_day.height == 0
    assert result.car_by_day.columns == ["relative_day", "mean_ar", "mean_car", "n"]
    assert json.dumps(result.to_public_json()) == json.dumps(result.to_public_json())


def test_missing_benchmark_is_an_error_not_a_skip() -> None:
    days = _days()
    benchmark_returns, _ = _flat_benchmark(days)
    frame = _prices({"AAA": (days, _symbol_path(days, benchmark_returns, shocks={150: 0.01}))})
    with pytest.raises(ValueError, match="benchmark"):
        run_event_study(
            _events(_event("FNDA", "ISIN:AAA", days[150])),
            FakePrices(frame),
            symbol_for=_mapper({"ISIN:AAA": "AAA"}),
        )


def test_a_benchmark_without_usable_closes_is_an_error() -> None:
    days = _days()
    frame = _prices({"SPY": (days, [0.0] * len(days))})
    with pytest.raises(ValueError, match="usable"):
        run_event_study(
            _events(_event("FNDA", "ISIN:AAA", days[150])),
            FakePrices(frame),
            symbol_for=_mapper({"ISIN:AAA": "AAA"}),
        )


def test_an_unusable_close_is_missing_price_data_not_a_crash() -> None:
    days = _days()
    benchmark_returns, benchmark_closes = _flat_benchmark(days)
    index = 150
    path = _symbol_path(days, benchmark_returns, shocks={index: 0.02})
    path[index] = 0.0
    frame = _prices({"SPY": (days, benchmark_closes), "AAA": (days, path)})
    result = run_event_study(
        _events(_event("FNDA", "ISIN:AAA", days[index])),
        FakePrices(frame),
        symbol_for=_mapper({"ISIN:AAA": "AAA"}),
    )
    assert result.summary["n_used"] == 0
    assert result.summary["skipped"]["no_prices"] == 1


def test_missing_event_columns_are_rejected() -> None:
    frame = pl.DataFrame({"fund_ticker": ["FNDA"]})
    with pytest.raises(ValueError, match="missing columns"):
        run_event_study(
            frame, FakePrices(pl.DataFrame(schema=PRICE_SCHEMA)), symbol_for=_mapper({})
        )


def test_estimate_must_precede_the_event_window() -> None:
    with pytest.raises(ValueError, match="estimation"):
        run_event_study(
            _events(),
            FakePrices(pl.DataFrame(schema=PRICE_SCHEMA)),
            symbol_for=_mapper({}),
            window=(-5, 5),
            estimation=(-120, -5),
        )


# --------------------------------------------------------------------------- #
# Output files and the public/internal split
# --------------------------------------------------------------------------- #


def test_write_outputs_names_the_files_and_keeps_identifiers_internal(
    tmp_path: Path,
) -> None:
    days = _days()
    benchmark_returns, benchmark_closes = _flat_benchmark(days)
    index = 150
    frame = _prices(
        {
            "SPY": (days, benchmark_closes),
            "ZZZ": (days, _symbol_path(days, benchmark_returns, shocks={index: 0.02})),
        }
    )
    events = _events(
        _event(
            "FNDZZ",
            "ISIN:CA0000000001",
            days[index],
            name="Zeta Fictional Mining",
            ticker="ZZZ",
        )
    )
    result = run_event_study(
        events, FakePrices(frame), symbol_for=_mapper({"ISIN:CA0000000001": "ZZZ"})
    )
    written = write_outputs(result, tmp_path / "study")

    assert [path.name for path in written] == [
        "car_by_day.csv",
        "per_event.internal.csv",
        "summary.json",
    ]
    assert all(path.exists() for path in written)

    public_text = (tmp_path / "study" / "summary.json").read_text(encoding="utf-8")
    car_text = (tmp_path / "study" / "car_by_day.csv").read_text(encoding="utf-8")
    internal_text = (tmp_path / "study" / "per_event.internal.csv").read_text(encoding="utf-8")
    for identifier in ("FNDZZ", "CA0000000001", "Zeta Fictional Mining", "ZZZ"):
        assert identifier not in public_text
        assert identifier not in car_text
        assert identifier in internal_text

    document = json.loads(public_text)
    assert document["label"] == "exploratory"
    assert document["benchmark"] == "SPY"
    assert document["n_used"] == 1
    assert document == result.to_public_json()
    assert list(document["skipped"]) == sorted(SKIP_REASONS)


def test_to_public_json_contains_no_identifiers() -> None:
    result = _varied_result()
    text = json.dumps(result.to_public_json(), sort_keys=True)
    assert "ISIN:" not in text
    assert "FNDA" not in text
    assert "event_day" not in text
    for identifier in result.per_event["security_name"].to_list():
        assert identifier not in text


def test_events_contract_columns_are_the_documented_ones() -> None:
    assert EVENT_COLUMNS == (
        "fund_ticker",
        "security_id",
        "security_name",
        "security_ticker",
        "exchange",
        "event_type",
        "portfolio_date",
        "known_date",
    )


def test_fetch_range_covers_estimation_and_the_forward_window() -> None:
    days = _days()
    benchmark_returns, benchmark_closes = _flat_benchmark(days)
    index = 150
    known = days[index]
    frame = _prices(
        {
            "SPY": (days, benchmark_closes),
            "AAA": (days, _symbol_path(days, benchmark_returns, shocks={index: 0.02})),
        }
    )
    prices = FakePrices(frame)
    run_event_study(
        _events(_event("FNDA", "ISIN:AAA", known)),
        prices,
        symbol_for=_mapper({"ISIN:AAA": "AAA"}),
    )
    symbols, start, end = prices.calls[0]
    assert symbols == ("AAA", "SPY")
    assert start <= known - timedelta(days=190)
    assert end >= known + timedelta(days=10)
    assert _day_index(days, known) == index
