"""Local request budget for a metered price API (D84).

Tiingo's free plan allows 50 requests per hour, 1,000 per day and 500 unique
symbols per calendar month. The budget is persisted as JSON so separate
commands (``etfh prices fetch``, ``etfh event-study``) share one meter instead
of each starting from zero. It is checked *before* a request is made: a call
that would cross a limit raises :class:`BudgetExceededError` and leaves the
stored counters unchanged.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["BudgetExceededError", "PriceBudget", "PriceBudgetUsage"]

_HOUR_FORMAT = "%Y-%m-%dT%H"
_DAY_FORMAT = "%Y-%m-%d"
_MONTH_FORMAT = "%Y-%m"


class BudgetExceededError(RuntimeError):
    """Raised before a request that the price budget would not allow."""

    def __init__(self, limit: str, *, allowed: int, used: int, requested: int) -> None:
        self.limit = limit
        self.allowed = allowed
        self.used = used
        self.requested = requested
        super().__init__(
            f"price budget exceeded: {limit} limit {allowed}, used {used}, requested {requested}"
        )


@dataclass(frozen=True, slots=True)
class PriceBudgetUsage:
    """A read-only snapshot of the counters for the current hour/day/month."""

    hour_requests: int
    day_requests: int
    month_symbols: int


class PriceBudget:
    """A persisted request meter: ``per_hour``, ``per_day`` and ``per_month`` symbols.

    One request fetches one symbol's daily closes, so ``consume`` counts one
    request per symbol and adds the symbol to the calendar month's unique
    symbol set. Buckets roll over on their own: an hour/day/month key that no
    longer matches "now" starts from zero.
    """

    def __init__(
        self,
        state_path: str | Path,
        *,
        per_hour: int = 50,
        per_day: int = 1000,
        symbols_per_month: int = 500,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if per_hour < 1 or per_day < 1 or symbols_per_month < 1:
            raise ValueError("price budget limits must all be at least 1")
        self.state_path = Path(state_path)
        self.per_hour = per_hour
        self.per_day = per_day
        self.symbols_per_month = symbols_per_month
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def usage(self, *, now: datetime | None = None) -> PriceBudgetUsage:
        """Return the counters that apply at ``now`` (default: the clock)."""
        state = self._load()
        moment = self._now(now)
        return PriceBudgetUsage(
            hour_requests=self._counter(state, "hour", _HOUR_FORMAT, moment),
            day_requests=self._counter(state, "day", _DAY_FORMAT, moment),
            month_symbols=len(self._symbols(state, moment)),
        )

    def check(self, symbols: Sequence[str], *, now: datetime | None = None) -> None:
        """Raise :class:`BudgetExceededError` if ``symbols`` would exceed a limit."""
        self._reserve(symbols, now=now)

    def consume(self, symbols: Sequence[str], *, now: datetime | None = None) -> None:
        """Count ``symbols`` as one request each, or raise before counting them."""
        if not symbols:
            return
        self._save(self._reserve(symbols, now=now))

    def _reserve(self, symbols: Sequence[str], *, now: datetime | None) -> dict[str, Any]:
        state = self._load()
        moment = self._now(now)
        requested = len(symbols)
        if requested == 0:
            return state
        hour_requests = self._counter(state, "hour", _HOUR_FORMAT, moment)
        day_requests = self._counter(state, "day", _DAY_FORMAT, moment)
        month_symbols = self._symbols(state, moment)

        if hour_requests + requested > self.per_hour:
            raise BudgetExceededError(
                "hourly requests",
                allowed=self.per_hour,
                used=hour_requests,
                requested=requested,
            )
        if day_requests + requested > self.per_day:
            raise BudgetExceededError(
                "daily requests",
                allowed=self.per_day,
                used=day_requests,
                requested=requested,
            )
        merged = month_symbols | {symbol.strip().upper() for symbol in symbols}
        if len(merged) > self.symbols_per_month:
            raise BudgetExceededError(
                "monthly unique symbols",
                allowed=self.symbols_per_month,
                used=len(month_symbols),
                requested=len(merged) - len(month_symbols),
            )

        state["hour"] = moment.strftime(_HOUR_FORMAT)
        state["hour_requests"] = hour_requests + requested
        state["day"] = moment.strftime(_DAY_FORMAT)
        state["day_requests"] = day_requests + requested
        state["month"] = moment.strftime(_MONTH_FORMAT)
        state["month_symbols"] = sorted(merged)
        return state

    def _now(self, now: datetime | None) -> datetime:
        moment = now if now is not None else self._clock()
        if moment.tzinfo is None:
            raise ValueError("price budget needs a timezone-aware datetime")
        return moment.astimezone(UTC)

    def _counter(self, state: dict[str, Any], name: str, fmt: str, moment: datetime) -> int:
        if state.get(name) != moment.strftime(fmt):
            return 0
        value = state.get(f"{name}_requests", 0)
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"{self.state_path}: invalid {name}_requests counter")
        return value

    def _symbols(self, state: dict[str, Any], moment: datetime) -> set[str]:
        if state.get("month") != moment.strftime(_MONTH_FORMAT):
            return set()
        value = state.get("month_symbols", [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"{self.state_path}: invalid month_symbols list")
        return {symbol.strip().upper() for symbol in value}

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        text = self.state_path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError(f"{self.state_path}: price budget state must be a JSON object")
        return data

    def _save(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
        fd, temp_name = tempfile.mkstemp(
            dir=self.state_path.parent, prefix=".budget.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.state_path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
