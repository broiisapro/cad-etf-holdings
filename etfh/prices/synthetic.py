"""Offline synthetic daily closes: deterministic per (seed, symbol).

Used by tests and the demo so nothing in the suite or the published demo
depends on a metered API. The series is a geometric random walk on weekdays;
the close for a given ``(seed, symbol, date)`` does not depend on the range a
caller asks for, so overlapping windows agree exactly.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, timedelta

import polars as pl

from etfh.prices.frames import closes_frame

__all__ = ["SyntheticPrices"]

#: Fixed walk start: every date on or after this Monday is priced the same way
#: no matter which window is requested.
_ANCHOR = date(1990, 1, 1)
_BASE_PRICE = 100.0
_DAILY_DRIFT = 0.0002
_BENCHMARK_VOL = 0.008
_DEFAULT_VOL = 0.015
_BENCHMARK = "SPY"


class SyntheticPrices:
    """Deterministic weekday closes for any symbol, including the ``SPY`` benchmark.

    ``shocks[(symbol, day)] = r`` multiplies that day's close by ``exp(r)`` on
    top of the walk, so a test can inject a known event-day move.
    """

    def __init__(
        self,
        seed: int = 7,
        shocks: Mapping[tuple[str, date], float] | None = None,
    ) -> None:
        self.seed = seed
        self.shocks = dict(shocks or {})

    def daily_closes(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
    ) -> pl.DataFrame:
        """Return ``symbol``/``date``/``close`` rows for weekdays in ``[start, end]``."""
        if start > end:
            raise ValueError(f"start {start.isoformat()} is after end {end.isoformat()}")
        wanted = sorted({symbol.strip().upper() for symbol in symbols if symbol and symbol.strip()})
        rows = [
            (symbol, day, close)
            for symbol in wanted
            for day, close in self._walk(symbol, start, end)
        ]
        return closes_frame(rows)

    def _walk(self, symbol: str, start: date, end: date) -> Iterator[tuple[date, float]]:
        digest = hashlib.sha256(f"{self.seed}|{symbol}".encode()).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))
        volatility = _BENCHMARK_VOL if symbol == _BENCHMARK else _DEFAULT_VOL
        price = _BASE_PRICE
        day = _ANCHOR
        while day <= end:
            if day.weekday() < 5:
                price *= math.exp(_DAILY_DRIFT + volatility * rng.gauss(0.0, 1.0))
                shock = self.shocks.get((symbol, day))
                if shock:
                    price *= math.exp(shock)
                if day >= start:
                    yield day, round(price, 6)
            day += timedelta(days=1)
