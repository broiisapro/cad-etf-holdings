"""Tiingo End-of-Day daily closes behind the terms gate (D78/D84).

What this module relies on from Tiingo's official documentation, read on
2026-10-28:

* Endpoint: ``GET https://api.tiingo.com/tiingo/daily/<ticker>/prices`` with
  ``startDate``/``endDate`` query parameters and ``format=json``
  (https://www.tiingo.com/documentation/end-of-day §2.1.2). The historical
  price endpoint returns one JSON object per trading day.
* Authentication: the token is sent in the ``Authorization`` header as
  ``Token <token>`` (https://www.tiingo.com/documentation/general/connecting
  §1.2.1). The documented alternative, a ``token`` query parameter, is never
  used here so the key cannot end up in a URL, a sidecar or a log line.
* Fields: ``date`` is the trading date as an ISO-8601 UTC-midnight timestamp
  (``2026-10-27T00:00:00.000Z``); ``close`` is the raw close and ``adjClose``
  the close adjusted for splits and dividends with the CRSP method. The
  ``close`` column of this module's frames is ``adjClose`` (falling back to
  ``close`` when a row has none), because an event study needs a series that
  is comparable across corporate actions.
* Plan limits: the free "Starter" plan is internal use only, 50 requests per
  hour, 1,000 per day and 500 unique symbols per month
  (https://www.tiingo.com/about/pricing); those are the
  :class:`~etfh.prices.budget.PriceBudget` defaults. Terms of service:
  https://www.tiingo.com/tos. The human's decision (D84) records that raw
  prices are never committed, published or redistributed.
* Error behaviour: the docs do not enumerate error bodies. An unknown ticker
  answers ``404`` with a JSON ``detail`` message (the meta endpoint documents
  ``startDate: null`` for an asset without price data). This module treats
  ``404`` and an empty array as "symbol absent, never an error" and any other
  non-2xx status as :class:`PriceFetchError`. Error bodies are stored in the
  raw store like any other response; they cannot contain the key because it is
  only ever sent as a header.

Every request goes through :class:`~etfh.http.PoliteClient`, which re-checks
the terms gate (D3), honours robots.txt (D46) and a one-second host interval
(D4). Budget accounting counts one request per symbol fetch; retries inside
the client are not counted again, so the meter is a guard against the plan
limits rather than an audit trail of HTTP attempts.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import polars as pl

from etfh.http import PoliteClient
from etfh.prices.budget import PriceBudget
from etfh.prices.frames import closes_frame
from etfh.registry import Source, require_collectable
from etfh.storage import RawStore, read_meta

__all__ = [
    "ApiKeyMissingError",
    "PriceFetchError",
    "PriceSourceError",
    "TiingoPrices",
]

_LOGGER = logging.getLogger(__name__)

SOURCE_ID = "tiingo_prices"
_API_KEY_ENV = "TIINGO_API_KEY"
_META_SUFFIX = ".meta.json"


class ApiKeyMissingError(RuntimeError):
    """Raised when the environment has no ``TIINGO_API_KEY``."""


class PriceSourceError(RuntimeError):
    """Raised when the config has no usable ``tiingo_prices`` source."""


class PriceFetchError(RuntimeError):
    """Raised when Tiingo answers with a status the client cannot use."""


class TiingoPrices:
    """Daily adjusted closes from Tiingo, gated, budgeted and stored raw.

    Construct through :meth:`from_env` for the CLI/event-study path or
    directly in tests with an explicit ``client``. Raw responses are written to
    the raw store under ``<raw_root>/<issuer>/<symbol>/<end>/`` and reused when
    an already stored response covers the requested range, so a repeated call
    makes no request.
    """

    def __init__(
        self,
        key: str,
        raw_root: str | Path,
        *,
        sources: Mapping[str, Source],
        budget: PriceBudget,
        client: PoliteClient | None = None,
        clock: Callable[[], datetime] | None = None,
        store: RawStore | None = None,
    ) -> None:
        if not key or not key.strip():
            raise ApiKeyMissingError(
                f"{_API_KEY_ENV} is empty; the Tiingo price source needs a key"
            )
        source = sources.get(SOURCE_ID)
        if source is None:
            raise PriceSourceError(f"config has no {SOURCE_ID!r} source")
        self._key = key.strip()
        self._source = source
        self._budget = budget
        self.raw_root = Path(raw_root)
        self._client = client
        self._owns_client = client is None
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._store = store if store is not None else RawStore(self.raw_root)

    @classmethod
    def from_env(
        cls,
        raw_root: str | Path,
        *,
        sources: Mapping[str, Source],
        budget_path: str | Path,
        client: PoliteClient | None = None,
        budget: PriceBudget | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> TiingoPrices:
        """Build the source with the key from ``TIINGO_API_KEY`` and a persisted budget."""
        key = os.environ.get(_API_KEY_ENV, "")
        if not key.strip():
            raise ApiKeyMissingError(f"{_API_KEY_ENV} is not set; the Tiingo price source needs it")
        return cls(
            key,
            raw_root,
            sources=sources,
            budget=budget if budget is not None else PriceBudget(budget_path),
            client=client,
            clock=clock,
        )

    @property
    def source(self) -> Source:
        """The ``tiingo_prices`` config entry this instance is bound to."""
        return self._source

    def close(self) -> None:
        """Close the owned HTTP client, if this instance created one."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> TiingoPrices:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def daily_closes(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
    ) -> pl.DataFrame:
        """Return adjusted closes for ``symbols`` in ``[start, end]``.

        Unknown symbols are absent from the result, never an error. The terms
        gate (D3) is checked before the cache is read and again by the client
        for every request, so a source reverted to ``pending_review`` cannot
        serve a fetch even from stored data.
        """
        if start > end:
            raise ValueError(f"start {start.isoformat()} is after end {end.isoformat()}")
        require_collectable(self._source)
        wanted = sorted({symbol.strip().upper() for symbol in symbols if symbol and symbol.strip()})
        rows: list[tuple[str, date, float]] = []
        for symbol in wanted:
            payload = self._cached(symbol, start, end)
            if payload is None:
                payload = self._fetch(symbol, start, end)
            rows.extend(self._rows(symbol, payload, start, end))
        return closes_frame(rows)

    def _fetch(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        url = self._url(symbol, start, end)
        self._budget.consume([symbol], now=self._now())
        response = self._polite().get(
            url,
            source=self._source,
            headers={
                "Authorization": f"Token {self._key}",
                "Content-Type": "application/json",
            },
        )
        stored = self._store.write(
            response.content,
            source_id=SOURCE_ID,
            issuer=self._source.issuer,
            fund_ticker=symbol,
            url=url,
            fetched_at=self._now(),
            ext="json",
            content_type=response.headers.get("content-type", "application/json"),
            as_of_hint=end,
            http_status=response.status_code,
        )
        if response.status_code == 404:
            _LOGGER.info("tiingo has no price data for %s (HTTP 404)", symbol)
            return []
        if response.status_code >= 400:
            raise PriceFetchError(f"tiingo {symbol}: HTTP {response.status_code} from {url}")
        return self._items(stored.path)

    def _items(self, path: Path) -> list[dict[str, Any]]:
        try:
            payload = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError) as error:
            raise PriceFetchError(f"tiingo response {path.name} is not readable JSON") from error
        return self._dict_items(payload, path.name)

    @staticmethod
    def _dict_items(payload: object, label: str) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise PriceFetchError(f"tiingo response {label} is not a JSON array of daily rows")
        return [item for item in payload if isinstance(item, dict)]

    def _cached(self, symbol: str, start: date, end: date) -> list[dict[str, Any]] | None:
        """Return a stored response whose requested range covers ``[start, end]``."""
        directory = self.raw_root / self._source.issuer / symbol
        if not directory.is_dir():
            return None
        for meta_path in sorted(directory.glob(f"*/*{_META_SUFFIX}"), reverse=True):
            try:
                meta = read_meta(meta_path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if meta.get("source_id") != SOURCE_ID:
                continue
            if meta.get("http_status") not in (None, 200):
                continue
            covered = _covered_range(str(meta.get("url", "")))
            if covered is None or covered[0] > start or covered[1] < end:
                continue
            data_path = meta_path.with_name(meta_path.name[: -len(_META_SUFFIX)] + ".json")
            if not data_path.is_file():
                continue
            try:
                return self._items(data_path)
            except PriceFetchError:
                continue
        return None

    def _rows(
        self,
        symbol: str,
        payload: Sequence[Mapping[str, Any]],
        start: date,
        end: date,
    ) -> list[tuple[str, date, float]]:
        rows: list[tuple[str, date, float]] = []
        for item in payload:
            day = _row_date(item.get("date"))
            if day is None or not start <= day <= end:
                continue
            raw_close = item.get("adjClose")
            if raw_close is None:
                raw_close = item.get("close")
            close = _finite_float(raw_close)
            if close is None:
                continue
            rows.append((symbol, day, close))
        return rows

    def _url(self, symbol: str, start: date, end: date) -> str:
        pattern = self._source.url_patterns[0]
        base = pattern.split("?", 1)[0].replace("<TICKER>", quote(symbol, safe=""))
        return f"{base}?startDate={start.isoformat()}&endDate={end.isoformat()}&format=json"

    def _polite(self) -> PoliteClient:
        if self._client is None:
            self._client = PoliteClient()
            self._owns_client = True
        return self._client

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None:
            raise ValueError("the price clock must return a timezone-aware datetime")
        return moment.astimezone(UTC)


def _row_date(value: object) -> date | None:
    """Parse a Tiingo row date (``2026-10-27T00:00:00.000Z`` or ``2026-10-27``)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _covered_range(url: str) -> tuple[date, date] | None:
    """The ``startDate``/``endDate`` a stored request asked for, if recorded."""
    query = parse_qs(urlsplit(url).query)
    starts = query.get("startDate") or query.get("startdate")
    ends = query.get("endDate") or query.get("enddate")
    if not starts or not ends:
        return None
    start = _row_date(starts[0])
    end = _row_date(ends[0])
    if start is None or end is None:
        return None
    return start, end
