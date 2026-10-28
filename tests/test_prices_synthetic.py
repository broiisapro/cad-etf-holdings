"""SyntheticPrices: deterministic weekday closes for tests and the demo."""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from etfh.prices import CLOSE_SCHEMA, SyntheticPrices

START = date(2026, 10, 19)  # Monday
END = date(2026, 10, 30)  # Friday


def test_schema_and_sort_order() -> None:
    frame = SyntheticPrices().daily_closes(["SPY", "AAPL"], START, END)
    assert frame.columns == ["symbol", "date", "close"]
    assert frame.schema == CLOSE_SCHEMA
    assert frame.sort(["symbol", "date"]).equals(frame)
    assert set(frame["symbol"].unique().to_list()) == {"AAPL", "SPY"}


def test_only_weekdays_inside_the_range() -> None:
    frame = SyntheticPrices().daily_closes(["AAPL"], START, END)
    days = frame["date"].to_list()
    assert days[0] == START
    assert days[-1] == END
    assert all(day.weekday() < 5 for day in days)
    assert len(days) == 10


def test_same_seed_repeats_exactly() -> None:
    first = SyntheticPrices(seed=7).daily_closes(["AAPL"], START, END)
    second = SyntheticPrices(seed=7).daily_closes(["AAPL"], START, END)
    assert first.equals(second)


def test_the_same_day_does_not_depend_on_the_window() -> None:
    wide = SyntheticPrices().daily_closes(["AAPL"], date(2026, 1, 1), END)
    narrow = SyntheticPrices().daily_closes(["AAPL"], START, END)
    overlapping = wide.filter(pl.col("date").is_between(START, END))
    assert overlapping.equals(narrow)


def test_a_different_seed_changes_the_series() -> None:
    first = SyntheticPrices(seed=7).daily_closes(["AAPL"], START, END)
    other = SyntheticPrices(seed=8).daily_closes(["AAPL"], START, END)
    assert not first.equals(other)


def test_symbols_are_independent_and_deduplicated() -> None:
    frame = SyntheticPrices().daily_closes(["AAPL", "aapl", "MSFT"], START, END)
    assert frame.height == 20
    aapl = frame.filter(pl.col("symbol") == "AAPL")["close"].to_list()
    msft = frame.filter(pl.col("symbol") == "MSFT")["close"].to_list()
    assert aapl != msft


def test_shocks_add_a_return_on_that_day_only() -> None:
    shock_day = date(2026, 10, 21)
    base = SyntheticPrices().daily_closes(["SPY"], START, END)
    shocked = SyntheticPrices(shocks={("SPY", shock_day): 0.05}).daily_closes(["SPY"], START, END)
    base_close = base.filter(pl.col("date") == shock_day)["close"].item()
    shocked_close = shocked.filter(pl.col("date") == shock_day)["close"].item()
    # Closes are rounded to six decimals, so compare at that precision.
    assert shocked_close == pytest.approx(base_close * 1.0512710963760241, rel=1e-6, abs=1e-5)
    before = shocked.filter(pl.col("date") < shock_day)
    assert before.equals(base.filter(pl.col("date") < shock_day))


def test_benchmark_is_available() -> None:
    frame = SyntheticPrices().daily_closes(["SPY"], START, END)
    assert frame.height == 10
    assert (frame["close"] > 0).all()


def test_empty_symbol_list_returns_a_typed_empty_frame() -> None:
    frame = SyntheticPrices().daily_closes([], START, END)
    assert frame.height == 0
    assert frame.schema == CLOSE_SCHEMA


def test_reversed_range_is_a_caller_error() -> None:
    with pytest.raises(ValueError, match="after end"):
        SyntheticPrices().daily_closes(["AAPL"], END, START)
