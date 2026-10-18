"""Optional OpenFIGI composite-FIGI lookup with an on-disk JSON cache (D24).

The v3 mapping API (``POST https://api.openfigi.com/v3/mapping``) takes an
array of jobs (``idType``, ``idValue``, optional ``micCode`` or ``exchCode``
and ``marketSecDes``) and returns one result object per job. Without an API
key the limits are 25 requests/minute and 10 jobs/request; with
``X-OPENFIGI-APIKEY`` they are 250/minute and 100 jobs/request. A 429 carries
``retry-after`` and ``ratelimit-reset`` headers.

Ticker jobs always carry ``marketSecDes=Equity`` (D47): the security master
only needs equity instruments, and the filter keeps options, warrants and
other instruments with the same ticker out of the answer. A micro-venue code
is sent as ``micCode``; a Bloomberg country-composite code (``US``, ``CN``,
...) is sent as ``exchCode``.

An ISIN identifies one security across all its listings, and OpenFIGI answers
with one row per venue; several country composites are therefore normal, not
ambiguous. The lookup picks the composite of the listing whose ``exchCode``
is the ISIN country's verified home-market code (``CA`` → ``CN``, ``US`` →
``US`` ...), falling back to the first returned listing (OpenFIGI's primary
listing) when the home venue is not among them. For a venue-filtered ticker
job, several *distinct* composite FIGIs are genuinely ambiguous: the lookup
returns ``None``, records the key in :attr:`~FigiLookup.ambiguous_keys` and
caches the ambiguity, so the caller can count it instead of guessing (D47,
issue #74).

The lookup is optional enrichment, off by default: nothing constructs a
:class:`FigiLookup` unless a caller passes one to
:class:`~etfh.secmaster.resolve.SecurityMaster`. Lookups are cached on disk
keyed by ``(idType, value, micCode, exchCode)``, and every network call goes
through an ``httpx`` transport that tests replace with ``httpx.MockTransport``.
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

CacheKey = tuple[str, str, str | None, str | None]

# ISO 3166 country of an ISIN -> the exchange/composite code of that country's
# home market, used to pick the home composite out of an ISIN's cross-listing
# rows. Each pair was verified on 2026-10-18 against the mapping API with a
# well-known ISIN and ``exchCode=<code>`` (AT/AV, AU/AU, BE/BB, CA/CN, CH/SW,
# DE/GR, DK/DC, ES/SM, FR/FP, GB/LN, HK/HK, IT/IM, JP/JP, KR/KS, NL/NA, NO/NO,
# SE/SS, US/US each returned a listing on that home venue). Codes live in
# ``etfh.secmaster.identifiers`` where they are shared with the exchange table.
_HOME_MARKET_CODES: dict[str, str] = {
    "AT": "AV",
    "AU": "AU",
    "BE": "BB",
    "CA": "CN",
    "CH": "SW",
    "DE": "GR",
    "DK": "DC",
    "ES": "SM",
    "FR": "FP",
    "GB": "LN",
    "HK": "HK",
    "IT": "IM",
    "JP": "JP",
    "KR": "KS",
    "NL": "NA",
    "NO": "NO",
    "SE": "SS",
    "US": "US",
}


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
        self.ambiguous_count = 0
        self.ambiguous_keys: set[CacheKey] = set()
        self._transport = transport
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._max_attempts = max(1, max_attempts)
        self._jobs_per_request = 100 if api_key else 10
        self._requests_per_minute = 250 if api_key else 25
        self._request_times: list[float] = []

    def composite_figi(
        self,
        id_type: str,
        value: str,
        mic: str | None = None,
        exch_code: str | None = None,
    ) -> str | None:
        """Return the composite FIGI for one identifier, or ``None``.

        ``id_type`` is an OpenFIGI identifier type (``ID_ISIN``, ``ID_CUSIP``,
        ``TICKER``, ...). Ticker lookups can be narrowed to one venue with
        ``mic`` (``micCode``) or a Bloomberg composite code (``exchCode``).
        ``None`` means "no mapping, several distinct mappings, or the lookup
        failed"; all are safe for an optional enrichment.
        """
        key = _key(id_type, value, mic, exch_code)
        return self.lookup_many([key])[key]

    def lookup_many(self, jobs: Sequence[CacheKey]) -> dict[CacheKey, str | None]:
        """Resolve many keys, batching cache misses into API requests.

        Cached hits (including cached "not found") make no request. Misses are
        grouped into requests of at most ``jobs_per_request`` jobs. A failed
        request returns ``None`` for its keys without caching them, so a later
        call retries. Ambiguous keys are recorded in :attr:`ambiguous_keys`
        with a ``None`` value.
        """
        keys: list[CacheKey] = []
        seen: set[CacheKey] = set()
        for id_type, value, mic, exch_code in jobs:
            key = _key(id_type, value, mic, exch_code)
            if key not in seen:
                seen.add(key)
                keys.append(key)
        results: dict[CacheKey, str | None] = {}
        pending: list[CacheKey] = []
        for key in keys:
            found, cached, cached_ambiguous = self._read_cache(key)
            if not found:
                pending.append(key)
            else:
                results[key] = cached
                self.cache_hits += 1
                if cached_ambiguous:
                    self._record_ambiguous(key)
        for start in range(0, len(pending), self._jobs_per_request):
            batch = pending[start : start + self._jobs_per_request]
            values, ambiguous, failed = self._fetch_batch(batch)
            for key in batch:
                if key in failed:
                    results[key] = None
                    continue
                key_ambiguous = key in ambiguous
                composite = values.get(key)
                self._write_cache(key, composite, ambiguous=key_ambiguous)
                if key_ambiguous:
                    self._record_ambiguous(key)
                results[key] = composite
        return results

    def _record_ambiguous(self, key: CacheKey) -> None:
        self.ambiguous_count += 1
        self.ambiguous_keys.add(key)

    def _fetch_batch(
        self, batch: Sequence[CacheKey]
    ) -> tuple[dict[CacheKey, str | None], set[CacheKey], set[CacheKey]]:
        payload = []
        for id_type, value, mic, exch_code in batch:
            job: dict[str, str] = {"idType": id_type, "idValue": value}
            if id_type == "TICKER":
                job["marketSecDes"] = "Equity"
            if mic is not None:
                job["micCode"] = mic
            if exch_code is not None:
                job["exchCode"] = exch_code
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
                return {}, set(), set(batch)
            self.requests_made += 1
            if response.status_code == 429:
                if attempt < self._max_attempts:
                    self._sleep(_retry_after(response))
                    continue
                return {}, set(), set(batch)
            if response.status_code >= 500:
                if attempt < self._max_attempts:
                    self._sleep(_backoff(attempt))
                    continue
                return {}, set(), set(batch)
            if response.status_code != 200:
                return {}, set(), set(batch)
            parsed = _parse_response(response, batch)
            if parsed is None:
                return {}, set(), set(batch)
            values, ambiguous = parsed
            return values, ambiguous, set()
        return {}, set(), set(batch)

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
        id_type, value, mic, exch_code = key
        safe = re.sub(
            r"[^A-Za-z0-9._-]+",
            "_",
            f"{id_type}_{value}_{mic or 'none'}_{exch_code or 'none'}",
        )
        digest = hashlib.sha256(
            "|".join((id_type, value, mic or "", exch_code or "")).encode()
        ).hexdigest()[:12]
        return self.cache_dir / f"{safe}-{digest}.json"

    def _read_cache(self, key: CacheKey) -> tuple[bool, str | None, bool]:
        """Return ``(found, composite, ambiguous)`` for a cache key."""
        path = self._cache_path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False, None, False
        if not isinstance(payload, dict):
            return False, None, False
        stored_key = (
            payload.get("id_type"),
            payload.get("value"),
            payload.get("mic"),
            payload.get("exch_code"),
        )
        if stored_key != key:
            return False, None, False
        composite = payload.get("composite_figi")
        if composite is not None and not isinstance(composite, str):
            return False, None, False
        ambiguous = payload.get("ambiguous") is True
        if ambiguous and composite is not None:
            return False, None, False
        return True, composite, ambiguous

    def _write_cache(self, key: CacheKey, composite: str | None, *, ambiguous: bool) -> None:
        path = self._cache_path(key)
        payload = {
            "id_type": key[0],
            "value": key[1],
            "mic": key[2],
            "exch_code": key[3],
            "composite_figi": composite,
            "ambiguous": ambiguous,
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(path)


def _key(id_type: str, value: str, mic: str | None, exch_code: str | None) -> CacheKey:
    cleaned_mic = mic.strip().upper() if mic is not None else None
    cleaned_exch = exch_code.strip().upper() if exch_code is not None else None
    return (
        id_type.strip().upper(),
        value.strip().upper(),
        cleaned_mic or None,
        cleaned_exch or None,
    )


def _parse_response(
    response: httpx.Response,
    batch: Sequence[CacheKey],
) -> tuple[dict[CacheKey, str | None], set[CacheKey]] | None:
    try:
        payload: Any = response.json()
    except ValueError:
        return None
    if not isinstance(payload, list) or len(payload) != len(batch):
        return None
    values: dict[CacheKey, str | None] = {}
    ambiguous: set[CacheKey] = set()
    for key, entry in zip(batch, payload, strict=True):
        composite, key_ambiguous = _composite_from_entry(
            entry,
            home_code=_HOME_MARKET_CODES.get(key[1][:2]) if key[0] == "ID_ISIN" else None,
            ambiguous_ok=key[0] != "ID_ISIN",
        )
        values[key] = composite
        if key_ambiguous:
            ambiguous.add(key)
    return values, ambiguous


def _composite_from_entry(
    entry: Any,
    *,
    home_code: str | None = None,
    ambiguous_ok: bool = False,
) -> tuple[str | None, bool]:
    """Return ``(composite, ambiguous)`` for one mapping result.

    Instrument rows are read in response order. An ISIN answer's rows are the
    same security on different venues, so ``home_code`` (the ISIN country's
    verified home market) picks the home composite and several distinct
    composites are not ambiguity; without a home match the first listing's
    composite is used. A venue-filtered ticker answer with several distinct
    composites is ambiguous and resolves to ``None`` (D47, issue #74).
    """
    if not isinstance(entry, dict):
        return None, False
    data = entry.get("data")
    if not isinstance(data, list):
        return None, False
    rows = [
        (instrument.get("exchCode"), instrument["compositeFIGI"])
        for instrument in data
        if isinstance(instrument, dict)
        and isinstance(instrument.get("compositeFIGI"), str)
        and instrument["compositeFIGI"]
    ]
    if not rows:
        return None, False
    composites = {composite for _, composite in rows}
    if ambiguous_ok and len(composites) > 1:
        return None, True
    if home_code is not None:
        for exch_code, composite in rows:
            if exch_code == home_code:
                return composite, False
    return rows[0][1], False


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
