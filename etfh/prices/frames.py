"""The frame every price source returns (wave-08 interfaces §6)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

import polars as pl

__all__ = ["CLOSE_SCHEMA", "closes_frame"]

#: ``symbol: str, date: date, close: f64`` — the contract with ``etfh.eventstudy``.
CLOSE_SCHEMA: dict[str, Any] = {
    "symbol": pl.String,
    "date": pl.Date,
    "close": pl.Float64,
}


def closes_frame(rows: Iterable[tuple[str, date, float]]) -> pl.DataFrame:
    """Build the contract frame from ``(symbol, date, close)`` rows.

    Rows are sorted by ``symbol`` then ``date`` and de-duplicated on the key
    (the last value wins), so two sources feeding the same day cannot make the
    frame order depend on insertion order. An empty input still returns the
    three typed columns.
    """
    unique: dict[tuple[str, date], float] = {}
    for symbol, day, close in rows:
        unique[(symbol, day)] = close
    if not unique:
        return pl.DataFrame(schema=CLOSE_SCHEMA)
    ordered = sorted(unique.items(), key=lambda item: item[0])
    return pl.DataFrame(
        [(symbol, day, close) for (symbol, day), close in ordered],
        schema=CLOSE_SCHEMA,
        orient="row",
    )
