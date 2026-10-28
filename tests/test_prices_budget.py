"""PriceBudget: persisted hourly/daily/monthly limits checked before a request."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from etfh.prices import BudgetExceededError, PriceBudget, PriceBudgetUsage

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def _budget(path: Path, **kwargs: int) -> PriceBudget:
    return PriceBudget(path, clock=lambda: NOW, **kwargs)


def test_defaults_match_the_free_plan(tmp_path: Path) -> None:
    budget = _budget(tmp_path / "budget.json")
    assert budget.per_hour == 50
    assert budget.per_day == 1000
    assert budget.symbols_per_month == 500


def test_usage_starts_empty(tmp_path: Path) -> None:
    assert _budget(tmp_path / "budget.json").usage() == PriceBudgetUsage(
        hour_requests=0, day_requests=0, month_symbols=0
    )


def test_consume_records_requests_and_symbols(tmp_path: Path) -> None:
    budget = _budget(tmp_path / "budget.json")
    budget.consume(["aapl", "SPY"])
    assert budget.usage() == PriceBudgetUsage(hour_requests=2, day_requests=2, month_symbols=2)
    state = json.loads((tmp_path / "budget.json").read_text())
    assert state["month_symbols"] == ["AAPL", "SPY"]
    assert state["hour"] == "2026-10-28T12"


def test_state_is_shared_between_instances(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    _budget(path).consume(["AAPL"])
    assert _budget(path).usage().hour_requests == 1


def test_hourly_limit_refuses_before_overrunning(tmp_path: Path) -> None:
    budget = _budget(tmp_path / "budget.json", per_hour=2)
    budget.consume(["AAPL", "SPY"])
    with pytest.raises(BudgetExceededError) as excinfo:
        budget.consume(["QQQ"])
    assert excinfo.value.limit == "hourly requests"
    assert excinfo.value.allowed == 2
    assert excinfo.value.used == 2
    assert "hourly requests" in str(excinfo.value)
    assert budget.usage().hour_requests == 2


def test_daily_limit_refuses_across_hours(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    moments = [datetime(2026, 10, 28, 9, tzinfo=UTC), datetime(2026, 10, 28, 10, tzinfo=UTC)]
    first = PriceBudget(path, per_hour=50, per_day=2, clock=lambda: moments[0])
    first.consume(["AAPL", "SPY"])
    second = PriceBudget(path, per_hour=50, per_day=2, clock=lambda: moments[1])
    with pytest.raises(BudgetExceededError, match="daily requests"):
        second.consume(["QQQ"])
    assert second.usage(now=moments[1]).day_requests == 2


def test_monthly_unique_symbols_are_capped_but_repeats_are_free(tmp_path: Path) -> None:
    budget = _budget(tmp_path / "budget.json", symbols_per_month=2)
    budget.consume(["AAPL"])
    budget.consume(["AAPL"])  # same symbol: a request, no new symbol
    budget.consume(["SPY"])
    with pytest.raises(BudgetExceededError) as excinfo:
        budget.consume(["QQQ"])
    assert excinfo.value.limit == "monthly unique symbols"
    assert budget.usage() == PriceBudgetUsage(hour_requests=3, day_requests=3, month_symbols=2)


def test_buckets_roll_over(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    moments = [
        datetime(2026, 10, 31, 23, tzinfo=UTC),
        datetime(2026, 11, 1, 0, tzinfo=UTC),
        datetime(2026, 11, 2, 0, tzinfo=UTC),
    ]
    PriceBudget(path, clock=lambda: moments[0]).consume(["AAPL"])
    an_hour_later = PriceBudget(path, clock=lambda: datetime(2026, 11, 1, 0, tzinfo=UTC))
    assert an_hour_later.usage().hour_requests == 0
    assert an_hour_later.usage().day_requests == 0
    assert an_hour_later.usage().month_symbols == 0  # new calendar month
    an_hour_later.consume(["SPY"])
    next_day = PriceBudget(path, clock=lambda: datetime(2026, 11, 2, 0, tzinfo=UTC))
    assert next_day.usage().day_requests == 0
    assert next_day.usage().month_symbols == 1  # SPY stays counted for November


def test_check_does_not_spend_the_budget(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    budget = _budget(path, per_hour=1)
    budget.check(["AAPL"])
    assert budget.usage().hour_requests == 0
    assert not path.exists()
    budget.consume(["AAPL"])
    with pytest.raises(BudgetExceededError):
        budget.check(["SPY"])


def test_empty_symbol_list_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    budget = _budget(path)
    budget.consume([])
    assert not path.exists()
    assert budget.usage().hour_requests == 0


def test_limits_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        PriceBudget(tmp_path / "b.json", per_hour=0)


def test_naive_clock_is_rejected(tmp_path: Path) -> None:
    naive = datetime(2026, 10, 28, 12, 0)  # noqa: DTZ001 - the point of the test
    budget = PriceBudget(tmp_path / "b.json", clock=lambda: naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        budget.consume(["AAPL"])
