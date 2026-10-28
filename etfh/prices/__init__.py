"""Daily price sources for FR-3.2: the terms gate plus two implementations.

``TiingoPrices`` is the metered, terms-gated real source; ``SyntheticPrices``
is deterministic and offline for tests and the demo. Both return the structural
``PriceSource`` frame declared in wave-08 interfaces §6:

``symbol: str, date: date, close: f64`` sorted by symbol then date, with
unknown symbols absent rather than an error. ``etfh.eventstudy`` declares the
protocol; this package implements it without importing that module.
"""

from __future__ import annotations

from etfh.prices.budget import BudgetExceededError, PriceBudget, PriceBudgetUsage
from etfh.prices.frames import CLOSE_SCHEMA, closes_frame
from etfh.prices.symbols import cached_figi_resolver, symbol_for
from etfh.prices.synthetic import SyntheticPrices
from etfh.prices.tiingo import (
    ApiKeyMissingError,
    PriceFetchError,
    PriceSourceError,
    TiingoPrices,
)

__all__ = [
    "CLOSE_SCHEMA",
    "ApiKeyMissingError",
    "BudgetExceededError",
    "PriceBudget",
    "PriceBudgetUsage",
    "PriceFetchError",
    "PriceSourceError",
    "SyntheticPrices",
    "TiingoPrices",
    "cached_figi_resolver",
    "closes_frame",
    "symbol_for",
]
