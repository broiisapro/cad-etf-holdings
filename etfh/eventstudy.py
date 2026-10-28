"""Exploratory event study of price reactions around large ETF additions.

What it does
------------
:func:`run_event_study` takes the events produced by
``etfh.events.large_additions`` (one row per security that a fund added or
raised sharply) and the daily closes from a :class:`PriceSource`, and measures
how the security's return behaved around the day the market could first have
known about the change. It returns the per-event numbers (internal, they name
securities and funds), the day-by-day cross-sectional average (public-safe),
and a summary of the aggregate statistics (public-safe).

Method
------
Trading days
    The benchmark's dates are the calendar. Day 0 of an event is the first
    benchmark trading day on or after the event's ``known_date`` (the first day
    the market could know about the holding). If the benchmark has no trading
    day within :data:`MAX_DAY0_LAG` calendar days of ``known_date``, the grid
    does not cover the event and the event is skipped as ``window_incomplete``.

Returns
    Simple (not log) daily returns of split- and dividend-adjusted closes:
    ``r_t = close_t / close_{t-1} - 1``. A return needs the previous trading
    day's close, so an event whose window starts on the grid's first date cannot
    be measured and is skipped. Closes that are null or not strictly positive
    are treated as missing.

Abnormal returns
    ``market_adjusted``: ``AR_t = r_t - r_benchmark,t``. No estimation is
    needed, so this model works for events with a short price history.
    ``market_model``: ``alpha`` and ``beta`` come from an ordinary least-squares
    regression of the security's return on the benchmark's return over the
    estimation window (default relative days -120..-11). At least
    :data:`MIN_ESTIMATION_DAYS` (60) days with both returns present are
    required; a shorter (or degenerate, zero benchmark variance) estimation
    leaves the event skipped as ``short_estimation``. Then
    ``AR_t = r_t - (alpha + beta * r_benchmark,t)``.

Cumulative abnormal returns
    ``CAR[a,b] = sum(AR_t for t in a..b)``. Four sub-windows are reported when
    they fit entirely inside ``window``: ``[-1,1]``, ``[0,1]``, ``[0,5]`` and
    ``[-5,5]``. Sub-windows are never clipped: a window too narrow simply
    reports fewer of them.

Cross-section
    For each reported sub-window the summary gives the mean CAR over the used
    events, its t-statistic using the cross-sectional standard error
    (``mean / (sd / sqrt(n))``, sample ``sd``, ``n - 1`` denominator, null when
    ``n < 2`` or the dispersion is zero), and a seeded 95 % percentile bootstrap
    confidence interval. The bootstrap resamples the per-event CARs with
    replacement ``bootstrap`` times using :class:`random.Random` seeded with
    ``seed`` and takes the 2.5th and 97.5th percentiles by linear
    interpolation. The bootstrap describes sampling noise in this small
    cross-section; it is not a hypothesis test.

Duplicates
    Several funds can hold the same security and report the addition on the
    same day. That is one piece of price news, not several, so events sharing a
    resolved price symbol and day 0 are collapsed to one observation: the first
    in ``(known_date, fund_ticker, security_id)`` order is kept and every other
    one is counted under the ``duplicate`` skip reason. This is why
    ``n_used`` can be below ``n_events`` even when prices are complete.

Skips
    Events that cannot be measured are counted, never silently dropped. The
    reasons are ``no_symbol`` (``symbol_for`` returned ``None``), ``no_prices``
    (the source returned nothing for the symbol, or a required trading day is
    missing), ``short_estimation`` (fewer than 60 usable estimation days, or no
    benchmark variation), ``window_incomplete`` (no day 0 within
    :data:`MAX_DAY0_LAG` days of ``known_date``, a null ``known_date`` or
    ``portfolio_date``, or the event window does not fit the fetched grid) and
    ``duplicate`` (another event already claimed this symbol and day 0). An
    event with a null ``known_date`` has no day 0 at all and is counted as
    ``window_incomplete`` before symbol resolution. Everything else runs
    ``no_symbol``, then ``window_incomplete``, then ``duplicate``, then
    ``no_prices``, then ``short_estimation``; the first failing check names the
    reason, so every event is either used once or counted once. For each event
    ``n_events == n_used + sum(skipped.values())``.

Public output
    :meth:`EventStudyResult.to_public_json` and :attr:`EventStudyResult.car_by_day`
    contain aggregate statistics only - no fund tickers, no security ids or
    names, no individual event dates. :attr:`EventStudyResult.per_event` is the
    internal table and is the only place identifiers appear; :func:`write_outputs`
    writes it as ``per_event.internal.csv``.

Limits worth stating plainly: the study is exploratory and measures a
cross-section with no other controls (size, sector, market moves beyond the
benchmark); symbol resolution is US-listed only (see ``etfh.prices.symbol_for``)
so Canadian-listed lines drop out as ``no_symbol``; event dates are the first
day the holdings change was *known* to this dataset, which can lag the
portfolio date; and a small ``n_used`` makes the means and intervals noisy.
"""

from __future__ import annotations

import json
import math
import random
from bisect import bisect_left
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import polars as pl

__all__ = [
    "EVENT_COLUMNS",
    "MIN_ESTIMATION_DAYS",
    "SKIP_REASONS",
    "SUB_WINDOWS",
    "EventStudyResult",
    "PriceSource",
    "run_event_study",
    "write_outputs",
]

Model = Literal["market_adjusted", "market_model"]

#: Minimum number of usable estimation days for the market model.
MIN_ESTIMATION_DAYS = 60

#: Sub-windows reported when they fit inside the requested ``window``.
SUB_WINDOWS: tuple[tuple[str, int, int], ...] = (
    ("[-1,1]", -1, 1),
    ("[0,1]", 0, 1),
    ("[0,5]", 0, 5),
    ("[-5,5]", -5, 5),
)

#: Every skip reason, in reporting order. All keys are always present in the
#: summary, with zero when nothing was skipped for that reason.
SKIP_REASONS: tuple[str, ...] = (
    "no_symbol",
    "no_prices",
    "short_estimation",
    "window_incomplete",
    "duplicate",
)

#: Columns :func:`run_event_study` reads from the events frame (interfaces §5).
EVENT_COLUMNS: tuple[str, ...] = (
    "fund_ticker",
    "security_id",
    "security_name",
    "security_ticker",
    "exchange",
    "event_type",
    "portfolio_date",
    "known_date",
)

#: Calendar padding around the event dates when prices are fetched: enough to
#: cover 120 estimation trading days before day 0 and the forward window after
#: it. A grid that is still too short is a counted skip, not an error.
_PAD_BEFORE = timedelta(days=400)
_PAD_AFTER = timedelta(days=30)

#: Longest gap, in calendar days, between ``known_date`` and day 0 that still
#: counts as "the benchmark covers this event". Exchange closures never last
#: longer than a few days.
MAX_DAY0_LAG = 10

_METHOD_TEXT = (
    "Trading days are the benchmark's dates; day 0 is the first benchmark "
    "trading day on or after known_date. Returns are simple daily returns of "
    "adjusted closes. market_adjusted: AR = r - r_benchmark. market_model: "
    "OLS alpha and beta over the estimation window (>= 60 usable days), "
    "AR = r - (alpha + beta * r_benchmark). CAR[a,b] sums AR over the "
    "sub-window. Cross-sectional mean, t-statistic from the cross-sectional "
    "standard error and a seeded percentile bootstrap 95% interval."
)

_NOTES: tuple[str, ...] = (
    "Events sharing a resolved symbol and day 0 across funds are one "
    "observation: the first in (known_date, fund_ticker, security_id) order is "
    "kept and the rest are counted as 'duplicate'.",
    "Events that cannot be measured are counted by the first failing check: "
    "no_symbol, window_incomplete, duplicate, no_prices, short_estimation; an "
    "event without a known_date is window_incomplete. n_events always equals "
    "n_used plus the sum of the skip counts.",
    "Sub-windows are reported only when they fit entirely inside the "
    "requested window and are never clipped.",
    "The bootstrap interval describes sampling noise in a small "
    "cross-section; it is not a hypothesis test.",
)

_DISCLAIMER = (
    "Exploratory aggregate statistics only; not investment advice. The price "
    "source is terms-restricted (internal research use); raw prices are never "
    "published."
)

_PER_EVENT_SCHEMA: dict[str, Any] = {
    "fund_ticker": pl.String,
    "security_id": pl.String,
    "security_name": pl.String,
    "security_ticker": pl.String,
    "exchange": pl.String,
    "event_type": pl.String,
    "portfolio_date": pl.Date,
    "known_date": pl.Date,
    "event_day": pl.Date,
    "symbol": pl.String,
    "estimation_days": pl.Int64,
    "alpha": pl.Float64,
    "beta": pl.Float64,
    "car_m1_1": pl.Float64,
    "car_0_1": pl.Float64,
    "car_0_5": pl.Float64,
    "car_m5_5": pl.Float64,
}

_CAR_BY_DAY_SCHEMA: dict[str, Any] = {
    "relative_day": pl.Int64,
    "mean_ar": pl.Float64,
    "mean_car": pl.Float64,
    "n": pl.Int64,
}

_CAR_COLUMNS: dict[str, str] = {
    "[-1,1]": "car_m1_1",
    "[0,1]": "car_0_1",
    "[0,5]": "car_0_5",
    "[-5,5]": "car_m5_5",
}


class PriceSource(Protocol):
    """Where daily adjusted closes come from (interfaces §6).

    ``daily_closes`` returns a frame with columns ``symbol: str``,
    ``date: date`` and ``close: f64`` (split/dividend-adjusted), sorted by
    ``symbol, date``. Unknown symbols are absent, never an error.
    """

    def daily_closes(self, symbols: Sequence[str], start: date, end: date) -> pl.DataFrame: ...


@dataclass(frozen=True)
class EventStudyResult:
    """Outcome of one event study run.

    ``per_event`` is internal: one row per used event with fund and security
    identifiers. ``car_by_day`` and ``summary`` are public-safe aggregates.
    """

    per_event: pl.DataFrame
    car_by_day: pl.DataFrame
    summary: dict[str, Any]

    def to_public_json(self) -> dict[str, Any]:
        """Return a JSON-ready copy of :attr:`summary` (aggregates only).

        The round-trip through :mod:`json` both deep-copies and proves that the
        summary holds only JSON types.
        """
        public: dict[str, Any] = json.loads(json.dumps(self.summary))
        return public


@dataclass(frozen=True)
class _Event:
    """One input event, already validated and date-typed."""

    fund_ticker: str
    security_id: str
    security_name: str
    security_ticker: str | None
    exchange: str | None
    event_type: str
    portfolio_date: date
    known_date: date


@dataclass(frozen=True)
class _Series:
    """One symbol's closes and returns, aligned to the benchmark's dates."""

    closes: list[float | None]
    returns: list[float | None]


@dataclass(frozen=True)
class _Grid:
    """The benchmark's trading-day grid and its returns."""

    dates: list[date]
    index: dict[date, int]
    returns: list[float | None]

    def first_on_or_after(self, day: date) -> date | None:
        position = bisect_left(self.dates, day)
        if position >= len(self.dates):
            return None
        return self.dates[position]


@dataclass(frozen=True)
class _UsedEvent:
    """One measured event, the internal row of the study."""

    event: _Event
    symbol: str
    event_day: date
    alpha: float | None
    beta: float | None
    estimation_days: int
    cars: dict[str, float]
    ars: list[float]


def run_event_study(
    events: pl.DataFrame,
    prices: PriceSource,
    *,
    symbol_for: Callable[[str | None, str | None, str | None], str | None],
    benchmark: str = "SPY",
    window: tuple[int, int] = (-5, 5),
    estimation: tuple[int, int] = (-120, -11),
    model: Model = "market_adjusted",
    bootstrap: int = 2000,
    seed: int = 7,
) -> EventStudyResult:
    """Measure abnormal returns around each event.

    See the module docstring for the method. ``events`` must carry the columns
    of interfaces §5 (``EVENT_COLUMNS`` are the ones read here); extra columns
    such as weights are ignored. Raises ``ValueError`` when the events frame is
    missing columns or when the benchmark has no prices at all, because neither
    can be turned into a counted per-event skip.

    Every other reason an event cannot be measured is counted in
    ``summary["skipped"]``; nothing is dropped silently.
    """
    _validate_arguments(window, estimation, model, bootstrap)
    fitted = [
        (label, low, high)
        for label, low, high in SUB_WINDOWS
        if window[0] <= low and high <= window[1]
    ]
    n_events = events.height
    skipped = dict.fromkeys(SKIP_REASONS, 0)

    def empty() -> EventStudyResult:
        return _empty_result(
            n_events=n_events,
            skipped=skipped,
            model=model,
            benchmark=benchmark,
            window=window,
            estimation=estimation,
            bootstrap=bootstrap,
            seed=seed,
            fitted=fitted,
        )

    dated, undated = _ordered_events(events)
    skipped["window_incomplete"] += undated

    candidates: list[tuple[_Event, str]] = []
    for event in dated:
        symbol = symbol_for(event.security_id, event.security_ticker, event.exchange)
        if symbol is None:
            skipped["no_symbol"] += 1
            continue
        candidates.append((event, symbol))

    if not candidates:
        return empty()

    start = min(event.known_date for event, _ in candidates) - _PAD_BEFORE
    end = max(event.known_date for event, _ in candidates) + _PAD_AFTER
    symbols = sorted({benchmark, *(symbol for _, symbol in candidates)})
    frame = _price_rows(prices.daily_closes(symbols, start, end))
    grid = _build_grid(frame, benchmark)
    closes_by_symbol = _closes_by_symbol(frame, symbols)

    seen: set[tuple[str, date]] = set()
    series_cache: dict[str, _Series | None] = {}
    used: list[_UsedEvent] = []

    def symbol_series(symbol: str) -> _Series | None:
        if symbol not in series_cache:
            series_cache[symbol] = _series(closes_by_symbol.get(symbol), grid)
        return series_cache[symbol]

    for event, symbol in candidates:
        event_day = grid.first_on_or_after(event.known_date)
        if event_day is None or (event_day - event.known_date).days > MAX_DAY0_LAG:
            skipped["window_incomplete"] += 1
            continue
        day0 = grid.index[event_day]
        first = day0 + window[0]
        last = day0 + window[1]
        if first < 1 or last >= len(grid.dates):
            skipped["window_incomplete"] += 1
            continue

        key = (symbol, event_day)
        if key in seen:
            skipped["duplicate"] += 1
            continue
        seen.add(key)

        if any(grid.returns[position] is None for position in range(first, last + 1)):
            skipped["window_incomplete"] += 1
            continue

        series = symbol_series(symbol)
        if series is None:
            skipped["no_prices"] += 1
            continue
        if any(series.returns[position] is None for position in range(first, last + 1)):
            skipped["no_prices"] += 1
            continue

        alpha: float | None = None
        beta: float | None = None
        estimation_days = 0
        if model == "market_model":
            pairs = _estimation_pairs(series, grid, day0, estimation)
            estimate = _ols(pairs) if len(pairs) >= MIN_ESTIMATION_DAYS else None
            if estimate is None:
                skipped["short_estimation"] += 1
                continue
            alpha, beta = estimate
            estimation_days = len(pairs)

        ars: list[float] = []
        for position in range(first, last + 1):
            security_return = series.returns[position]
            benchmark_return = grid.returns[position]
            if security_return is None or benchmark_return is None:
                ars = []
                break
            if model == "market_model":
                expected = _require(alpha) + _require(beta) * benchmark_return
            else:
                expected = benchmark_return
            ars.append(security_return - expected)
        if not ars:
            skipped["window_incomplete"] += 1
            continue

        cars = {
            label: sum(ars[low - window[0] : high - window[0] + 1]) for label, low, high in fitted
        }
        used.append(
            _UsedEvent(
                event=event,
                symbol=symbol,
                event_day=event_day,
                alpha=alpha,
                beta=beta,
                estimation_days=estimation_days,
                cars=cars,
                ars=ars,
            )
        )

    if not used:
        return empty()

    return EventStudyResult(
        per_event=_per_event_frame(used),
        car_by_day=_car_by_day_frame(used, window),
        summary=_summary(
            n_events=n_events,
            n_used=len(used),
            skipped=skipped,
            windows=_window_stats(used, fitted, bootstrap, seed),
            model=model,
            benchmark=benchmark,
            window=window,
            estimation=estimation,
            bootstrap=bootstrap,
            seed=seed,
        ),
    )


def write_outputs(result: EventStudyResult, out_dir: Path) -> list[Path]:
    """Write ``summary.json``, ``car_by_day.csv`` and ``per_event.internal.csv``.

    ``out_dir`` is created if needed. The summary is written with sorted keys so
    repeated runs over the same input produce identical bytes. Only
    ``per_event.internal.csv`` contains fund or security identifiers; the other
    two files are the public-safe aggregate outputs. The returned paths are
    sorted.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "summary.json"
    summary_path.write_text(
        json.dumps(result.to_public_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    car_path = out_dir / "car_by_day.csv"
    result.car_by_day.write_csv(car_path)
    per_event_path = out_dir / "per_event.internal.csv"
    result.per_event.write_csv(per_event_path)
    return sorted([summary_path, car_path, per_event_path])


# --------------------------------------------------------------------------- #
# Input handling
# --------------------------------------------------------------------------- #


def _validate_arguments(
    window: tuple[int, int],
    estimation: tuple[int, int],
    model: str,
    bootstrap: int,
) -> None:
    if window[0] > window[1]:
        raise ValueError(f"window must be ordered low..high, got {window!r}")
    if estimation[0] > estimation[1]:
        raise ValueError(f"estimation must be ordered low..high, got {estimation!r}")
    if estimation[1] >= window[0]:
        raise ValueError(
            f"estimation must end before the event window starts, "
            f"got estimation={estimation!r} and window={window!r}"
        )
    if model not in ("market_adjusted", "market_model"):
        raise ValueError(f"unknown model {model!r}; expected market_adjusted or market_model")
    if bootstrap < 1:
        raise ValueError(f"bootstrap must be at least 1 resample, got {bootstrap}")


def _ordered_events(events: pl.DataFrame) -> tuple[list[_Event], int]:
    """Return the dated events in a deterministic order, plus the undated count."""
    missing = [column for column in EVENT_COLUMNS if column not in events.columns]
    if missing:
        raise ValueError(f"events frame is missing columns: {', '.join(missing)}")
    for column in ("portfolio_date", "known_date"):
        if events.schema[column] != pl.Date:
            raise ValueError(f"events frame column {column!r} must be Date")

    ordered = events.sort(["known_date", "fund_ticker", "security_id"], nulls_last=True).select(
        *EVENT_COLUMNS
    )
    rows: list[_Event] = []
    undated = 0
    for row in ordered.iter_rows(named=True):
        known_date = row["known_date"]
        if not isinstance(known_date, date):
            undated += 1
            continue
        portfolio_date = row["portfolio_date"]
        if not isinstance(portfolio_date, date):
            undated += 1
            continue
        rows.append(
            _Event(
                fund_ticker=str(row["fund_ticker"]),
                security_id=str(row["security_id"]),
                security_name=str(row["security_name"]),
                security_ticker=_optional_str(row["security_ticker"]),
                exchange=_optional_str(row["exchange"]),
                event_type=str(row["event_type"]),
                portfolio_date=portfolio_date,
                known_date=known_date,
            )
        )
    return rows, undated


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _empty_result(
    *,
    n_events: int,
    skipped: dict[str, int],
    model: Model,
    benchmark: str,
    window: tuple[int, int],
    estimation: tuple[int, int],
    bootstrap: int,
    seed: int,
    fitted: list[tuple[str, int, int]],
) -> EventStudyResult:
    windows = {
        label: {"mean_car": None, "t_stat": None, "ci_low": None, "ci_high": None, "n": 0}
        for label, _, _ in fitted
    }
    return EventStudyResult(
        per_event=pl.DataFrame(schema=_PER_EVENT_SCHEMA),
        car_by_day=pl.DataFrame(schema=_CAR_BY_DAY_SCHEMA),
        summary=_summary(
            n_events=n_events,
            n_used=0,
            skipped=skipped,
            windows=windows,
            model=model,
            benchmark=benchmark,
            window=window,
            estimation=estimation,
            bootstrap=bootstrap,
            seed=seed,
        ),
    )


# --------------------------------------------------------------------------- #
# Prices and the trading-day grid
# --------------------------------------------------------------------------- #


def _price_rows(frame: pl.DataFrame) -> pl.DataFrame:
    """Normalize ``symbol, date, close`` to string, date and float (null if unparsable)."""
    for column in ("symbol", "date", "close"):
        if column not in frame.columns:
            raise ValueError(f"prices frame is missing column {column!r}")
    return frame.select(
        pl.col("symbol").cast(pl.String, strict=False).alias("symbol"),
        pl.col("date").cast(pl.Date, strict=False).alias("date"),
        pl.col("close").cast(pl.Float64, strict=False).alias("close"),
    )


def _build_grid(frame: pl.DataFrame, benchmark: str) -> _Grid:
    """Build the trading-day grid from the benchmark, skipping unusable closes."""
    benchmark_rows = (
        frame.filter(pl.col("symbol") == benchmark)
        .select("date", "close")
        .unique(subset=["date"], keep="last")
        .sort("date")
    )
    dates: list[date] = []
    closes: list[float | None] = []
    for row in benchmark_rows.iter_rows(named=True):
        day = row["date"]
        if not isinstance(day, date):
            continue
        dates.append(day)
        closes.append(_usable_close(row["close"]))
    if len(dates) < 2 or sum(1 for close in closes if close is not None) < 2:
        raise ValueError(
            f"benchmark {benchmark!r} needs at least two usable daily closes for the study"
        )
    returns: list[float | None] = [None]
    for position in range(1, len(closes)):
        previous = closes[position - 1]
        current = closes[position]
        if previous is None or current is None:
            returns.append(None)
        else:
            returns.append(current / previous - 1.0)
    return _Grid(
        dates=dates,
        index={day: position for position, day in enumerate(dates)},
        returns=returns,
    )


def _closes_by_symbol(frame: pl.DataFrame, symbols: Sequence[str]) -> dict[str, dict[date, float]]:
    wanted = set(symbols)
    closes: dict[str, dict[date, float]] = {}
    for row in frame.iter_rows(named=True):
        symbol = row["symbol"]
        day = row["date"]
        if symbol not in wanted or not isinstance(day, date):
            continue
        value = _usable_close(row["close"])
        if value is None:
            continue
        closes.setdefault(symbol, {})[day] = value
    return closes


def _usable_close(value: object) -> float | None:
    """A close is usable only when it is a finite number greater than zero."""
    if not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        return None
    return number


def _series(closes: dict[date, float] | None, grid: _Grid) -> _Series | None:
    """Align one symbol's closes to the grid; ``None`` when nothing is usable."""
    if not closes:
        return None
    aligned: list[float | None] = [closes.get(day) for day in grid.dates]
    returns: list[float | None] = [None]
    for position in range(1, len(aligned)):
        previous = aligned[position - 1]
        current = aligned[position]
        if previous is None or current is None:
            returns.append(None)
        else:
            returns.append(current / previous - 1.0)
    return _Series(closes=aligned, returns=returns)


def _estimation_pairs(
    series: _Series, grid: _Grid, day0: int, estimation: tuple[int, int]
) -> list[tuple[float, float]]:
    first = max(1, day0 + estimation[0])
    last = min(len(grid.dates) - 1, day0 + estimation[1])
    pairs: list[tuple[float, float]] = []
    for position in range(first, last + 1):
        benchmark_return = grid.returns[position]
        security_return = series.returns[position]
        if benchmark_return is None or security_return is None:
            continue
        pairs.append((benchmark_return, security_return))
    return pairs


def _ols(pairs: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Return ``(alpha, beta)`` from ``security = alpha + beta * benchmark``."""
    count = len(pairs)
    if count < 2:
        return None
    mean_benchmark = sum(pair[0] for pair in pairs) / count
    mean_security = sum(pair[1] for pair in pairs) / count
    covariance = sum(
        (benchmark - mean_benchmark) * (security - mean_security) for benchmark, security in pairs
    )
    variance = sum((benchmark - mean_benchmark) ** 2 for benchmark, _ in pairs)
    if variance <= 0.0:
        return None
    beta = covariance / variance
    alpha = mean_security - beta * mean_benchmark
    return alpha, beta


def _require(value: float | None) -> float:
    """Narrow a value that is non-null on this path, for the type checker."""
    if value is None:  # pragma: no cover - the market-model branch sets both
        raise AssertionError("market model coefficient missing")
    return value


# --------------------------------------------------------------------------- #
# Output frames and statistics
# --------------------------------------------------------------------------- #


def _per_event_frame(used: list[_UsedEvent]) -> pl.DataFrame:
    records: list[dict[str, Any]] = []
    for entry in used:
        record: dict[str, Any] = {
            "fund_ticker": entry.event.fund_ticker,
            "security_id": entry.event.security_id,
            "security_name": entry.event.security_name,
            "security_ticker": entry.event.security_ticker,
            "exchange": entry.event.exchange,
            "event_type": entry.event.event_type,
            "portfolio_date": entry.event.portfolio_date,
            "known_date": entry.event.known_date,
            "event_day": entry.event_day,
            "symbol": entry.symbol,
            "estimation_days": entry.estimation_days,
            "alpha": entry.alpha,
            "beta": entry.beta,
        }
        for label, column in _CAR_COLUMNS.items():
            record[column] = entry.cars.get(label)
        records.append(record)
    return pl.DataFrame(records, schema=_PER_EVENT_SCHEMA)


def _car_by_day_frame(used: list[_UsedEvent], window: tuple[int, int]) -> pl.DataFrame:
    """One row per relative day: mean AR, mean CAR and the (balanced) n."""
    days = list(range(window[0], window[1] + 1))
    count = len(used)
    mean_ar: list[float] = []
    mean_car: list[float] = []
    daily_totals = [0.0] * len(days)
    for entry in used:
        for position, value in enumerate(entry.ars):
            daily_totals[position] += value
    running = 0.0
    for total in daily_totals:
        running += total
        mean_ar.append(total / count)
        mean_car.append(running / count)
    return pl.DataFrame(
        {
            "relative_day": days,
            "mean_ar": mean_ar,
            "mean_car": mean_car,
            "n": [count] * len(days),
        },
        schema=_CAR_BY_DAY_SCHEMA,
    )


def _window_stats(
    used: list[_UsedEvent],
    fitted: list[tuple[str, int, int]],
    bootstrap: int,
    seed: int,
) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for label, _, _ in fitted:
        values = [entry.cars[label] for entry in used]
        mean = sum(values) / len(values)
        low, high = _bootstrap_interval(values, bootstrap, seed)
        stats[label] = {
            "mean_car": _round(mean),
            "t_stat": _round(_t_stat(values)),
            "ci_low": _round(low),
            "ci_high": _round(high),
            "n": len(values),
        }
    return stats


def _t_stat(values: list[float]) -> float | None:
    count = len(values)
    if count < 2:
        return None
    mean = sum(values) / count
    variance = sum((value - mean) ** 2 for value in values) / (count - 1)
    if variance <= 0.0:
        return None
    return mean / math.sqrt(variance / count)


def _bootstrap_interval(
    values: list[float], resamples: int, seed: int
) -> tuple[float | None, float | None]:
    """Percentile bootstrap 95 % interval of the mean, or ``(None, None)``."""
    if not values:
        return None, None
    rng = random.Random(seed)
    count = len(values)
    means: list[float] = []
    for _ in range(resamples):
        total = 0.0
        for _ in range(count):
            total += values[rng.randrange(count)]
        means.append(total / count)
    means.sort()
    return _percentile(means, 0.025), _percentile(means, 0.975)


def _percentile(sorted_values: list[float], quantile: float) -> float:
    """Linear-interpolation percentile of an already sorted list."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = quantile * (len(sorted_values) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return sorted_values[low]
    weight = position - low
    return sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    rounded = round(value, 6)
    return 0.0 if rounded == 0 else rounded


def _summary(
    *,
    n_events: int,
    n_used: int,
    skipped: dict[str, int],
    windows: dict[str, dict[str, Any]],
    model: Model,
    benchmark: str,
    window: tuple[int, int],
    estimation: tuple[int, int],
    bootstrap: int,
    seed: int,
) -> dict[str, Any]:
    return {
        "label": "exploratory",
        "model": model,
        "benchmark": benchmark,
        "window": [window[0], window[1]],
        "estimation": [estimation[0], estimation[1]],
        "min_estimation_days": MIN_ESTIMATION_DAYS,
        "n_events": n_events,
        "n_used": n_used,
        "skipped": dict(skipped),
        "windows": windows,
        "bootstrap": {
            "resamples": bootstrap,
            "seed": seed,
            "level": 0.95,
            "method": "percentile",
        },
        "method": _METHOD_TEXT,
        "notes": list(_NOTES),
        "disclaimer": _DISCLAIMER,
    }
