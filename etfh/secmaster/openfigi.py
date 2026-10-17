"""Optional OpenFIGI composite-FIGI lookup with an on-disk JSON cache (D24).

The v3 mapping API (``POST https://api.openfigi.com/v3/mapping``) takes an
array of jobs (``idType``, ``idValue``, optional ``micCode``) and returns one
result object per job. Without an API key the limits are 25 requests/minute
and 10 jobs/request; with ``X-OPENFIGI-APIKEY`` they are 250/minute and 100
jobs/request. A 429 carries ``retry-after`` and ``ratelimit-reset`` headers.

The lookup is optional enrichment, off by default: nothing constructs a
:class:`FigiLookup` unless a caller passes one to
:class:`~etfh.secmaster.resolve.SecurityMaster`. Lookups are cached on disk
keyed by ``(idType, value, mic)``, and every network call goes through an
``httpx`` transport that tests replace with ``httpx.MockTransport``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx

__all__ = ["FigiLookup"]

_URL = "https://api.openfigi.com/v3/mapping"
_WINDOW_SECONDS = 60.0
_MAX_WAIT_SECONDS = 60.0
_TIMEOUT_SECONDS = 30.0

CacheKey = tuple[str, str, str | None]


class FigiLookup:
    """Cached client for OpenFIGI composite FIGIs.

    ``transport`` is an injection point for tests; the real network is only
    used when it is ``None``. ``clock`` and ``sleep`` are injectable so the
    rate limiter never blocks in tests.
    """

    def __init__(
        self,
        cache_dir: Path,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        max_attempts: int = 3,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key
        self.requests_made = 0
        self.cache_hits = 0
        self._transport = transport
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._max_attempts = max(1, max_attempts)
        self._jobs_per_request = 100 if api_key else 10
        self._requests_per_minute = 250 if api_key else 25
        self._request_times: list[float] = []

    def composite_figi(self, id_type: str, value: str, mic: str | None = None) -> str | None:
        """Return the composite FIGI for one identifier, or ``None``.

        ``id_type`` is an OpenFIGI identifier type (``ID_ISIN``, ``ID_CUSIP``,
        ``TICKER``, ...). ``mic`` filters ticker lookups to one venue and must
        match the row's MIC exactly. ``None`` means "no mapping, or the lookup
        failed"; both are safe for an optional enrichment.
        """
        key = _key(id_type, value, mic)
        return self.lookup_many([key])[key]

    def lookup_many(self, jobs: Sequence[CacheKey]) -> dict[CacheKey, str | None]:
        """Resolve many keys, batching cache misses into API requests.

        Cached hits (including cached "not found") make no request. Misses are
        grouped into requests of at most ``jobs_per_request`` jobs. A failed
        request returns ``None`` for its keys without caching them, so a later
        call retries.
        """
        keys: list[CacheKey] = []
        seen: set[CacheKey] = set()
        for id_type, value, mic in jobs:
            key = _key(id_type, value, mic)
            if key not in seen:
                seen.add(key)
                keys.append(key)
        results: dict[CacheKey, str | None] = {}
        pending: list[CacheKey] = []
        for key in keys:
            found, cached = self._read_cache(key)
            if not found:
                pending.append(key)
            else:
                results[key] = cached
                self.cache_hits += 1
        for start in range(0, len(pending), self._jobs_per_request):
            batch = pending[start : start + self._jobs_per_request]
            values, failed = self._fetch_batch(batch)
            for key in batch:
                if key in failed:
                    results[key] = None
                    continue
                composite = values.get(key)
                self._write_cache(key, composite)
                results[key] = composite
        return results

    def _fetch_batch(
        self, batch: Sequence[CacheKey]
    ) -> tuple[dict[CacheKey, str | None], set[CacheKey]]:
        payload = []
        for id_type, value, mic in batch:
            job: dict[str, str] = {"idType": id_type, "idValue": value}
            if mic is not None:
                job["micCode"] = mic
            payload.append(job)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-OPENFIGI-APIKEY"] = self.api_key
        user_agent = os.environ.get("ETFH_USER_AGENT")
        if user_agent:
            headers["User-Agent"] = user_agent
        for attempt in range(1, self._max_attempts + 1):
            self._throttle()
            try:
                with httpx.Client(transport=self._transport, timeout=_TIMEOUT_SECONDS) as client:
                    response = client.post(_URL, json=payload, headers=headers)
            except httpx.HTTPError:
                if attempt < self._max_attempts:
                    self._sleep(_backoff(attempt))
                    continue
                return {}, set(batch)
            self.requests_made += 1
            if response.status_code == 429:
                if attempt < self._max_attempts:
                    self._sleep(_retry_after(response))
                    continue
                return {}, set(batch)
            if response.status_code >= 500:
                if attempt < self._max_attempts:
                    self._sleep(_backoff(attempt))
                    continue
                return {}, set(batch)
            if response.status_code != 200:
                return {}, set(batch)
            parsed = _parse_response(response, batch)
            if parsed is None:
                return {}, set(batch)
            return parsed, set()
        return {}, set(batch)

    def _throttle(self) -> None:
        now = self._clock()
        self._request_times = [
            time_ for time_ in self._request_times if now - time_ < _WINDOW_SECONDS
        ]
        if len(self._request_times) >= self._requests_per_minute:
            wait = _WINDOW_SECONDS - (now - self._request_times[0])
            if wait > 0:
                self._sleep(wait)
            now = self._clock()
            self._request_times = [
                time_ for time_ in self._request_times if now - time_ < _WINDOW_SECONDS
            ]
        self._request_times.append(now)

    def _cache_path(self, key: CacheKey) -> Path:
        id_type, value, mic = key
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{id_type}_{value}_{mic or 'none'}")
        digest = hashlib.sha256("|".join((id_type, value, mic or "")).encode()).hexdigest()[:12]
        return self.cache_dir / f"{safe}-{digest}.json"

    def _read_cache(self, key: CacheKey) -> tuple[bool, str | None]:
        """Return ``(found, composite)`` for a cache key."""
        path = self._cache_path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False, None
        if not isinstance(payload, dict):
            return False, None
        if (payload.get("id_type"), payload.get("value"), payload.get("mic")) != key:
            return False, None
        composite = payload.get("composite_figi")
        if composite is not None and not isinstance(composite, str):
            return False, None
        return True, composite

    def _write_cache(self, key: CacheKey, composite: str | None) -> None:
        path = self._cache_path(key)
        payload = {
            "id_type": key[0],
            "value": key[1],
            "mic": key[2],
            "composite_figi": composite,
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(path)


def _key(id_type: str, value: str, mic: str | None) -> CacheKey:
    cleaned_mic = mic.strip().upper() if mic is not None else None
    return (id_type.strip().upper(), value.strip().upper(), cleaned_mic or None)


def _parse_response(
    response: httpx.Response,
    batch: Sequence[CacheKey],
) -> dict[CacheKey, str | None] | None:
    try:
        payload: Any = response.json()
    except ValueError:
        return None
    if not isinstance(payload, list) or len(payload) != len(batch):
        return None
    values: dict[CacheKey, str | None] = {}
    for key, entry in zip(batch, payload, strict=True):
        values[key] = _composite_from_entry(entry)
    return values


def _composite_from_entry(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    data = entry.get("data")
    if not isinstance(data, list):
        return None
    for instrument in data:
        if isinstance(instrument, dict):
            composite = instrument.get("compositeFIGI")
            if isinstance(composite, str) and composite:
                return composite
    return None


def _retry_after(response: httpx.Response) -> float:
    for header in ("retry-after", "ratelimit-reset"):
        raw = response.headers.get(header)
        if raw is None:
            continue
        try:
            wait = float(raw)
        except ValueError:
            continue
        if wait > 0:
            return min(wait, _MAX_WAIT_SECONDS)
    return _WINDOW_SECONDS


def _backoff(attempt: int) -> float:
    return min(2.0**attempt, _MAX_WAIT_SECONDS)
