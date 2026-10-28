"""Map dataset security rows to US ticker symbols for the price source.

Only US-listed venues are in scope (D78: Tiingo's free plan covers US-listed
securities), so the function is deliberately total and conservative: anything
that is not a US venue, or that has no ticker, returns ``None`` and the row is
simply left out of a price request.

Issuers that identify holdings by ISIN or composite FIGI (BMO, Purpose) carry
no ticker at all, so :func:`symbol_for` alone cannot price them. The optional
:func:`cached_figi_resolver` adds the offline fallback D90a allows: it reads the
OpenFIGI cache the build already wrote (D47/D55) and resolves the ticker of a US
listing of the same security. It never contacts the network and never writes to
the cache — a missing or unreadable cache simply leaves the row unresolved.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from etfh.secmaster.identifiers import normalize_exchange, normalize_ticker

__all__ = ["cached_figi_resolver", "symbol_for"]

#: MICs of the US venues the free price plan covers: the five venues named in
#: the wave-08 interface (``XNYS``, ``XNAS``, ``XASE``, ``ARCX``, ``BATS``).
#: ``etfh.secmaster`` maps the Bloomberg labels for most of them onto these
#: codes (D22); ``XASE`` is accepted as a raw MIC because the alias table does
#: not list it yet.
_US_MICS = frozenset({"XNYS", "XNAS", "XASE", "ARCX", "BATS"})

_ID_PREFIX = "TKR:"
_FIGI_PREFIX = "FIGI:"

#: Security-id prefixes that name an OpenFIGI identifier, with the cache's
#: ``id_type`` for each. A ``CUSIP:`` id is only resolved when the cache holds a
#: CUSIP lookup (D23/D55 store CUSIP-derived ids as ``ISIN:``), so nothing is
#: derived here: the resolver never guesses a country.
_CACHE_ID_TYPES = (("ISIN:", "ID_ISIN"), ("CUSIP:", "ID_CUSIP"))

#: OpenFIGI exchange code of the US country composite (``exchCode`` "US").
_US_COMPOSITE = "US"

_TICKER_SEPARATORS = {ord("/"): "-", ord("."): "-"}


def _mic(label: str | None) -> str | None:
    """Normalize an exchange label or raw MIC to a MIC, or ``None``."""
    if label is None:
        return None
    normalized = normalize_exchange(label)
    if normalized is not None:
        return normalized
    cleaned = label.strip().upper()
    return cleaned if cleaned in _US_MICS else None


def symbol_for(
    security_id: str | None,
    security_ticker: str | None,
    exchange: str | None,
) -> str | None:
    """Return the Tiingo symbol for a security row, or ``None``.

    ``exchange`` may be a MIC or any label ``etfh.secmaster`` understands
    (``NYSE``, ``NASDAQ GS``, ...). When the row has no exchange label but its
    D23 security id is exchange-qualified (``TKR:<MIC>:<TICKER>``), the id
    supplies both. Class shares use ``-`` for Tiingo (``BRK.B`` → ``BRK-B``).

    ISIN-only, CUSIP-only, name-only, non-US and id-less rows return ``None``:
    the event study then reports them as skipped instead of guessing a symbol.
    """
    mic = _mic(exchange)
    ticker = normalize_ticker(security_ticker)

    if security_id is not None and security_id.startswith(_ID_PREFIX):
        parts = security_id.split(":", 2)
        if len(parts) == 3:
            if mic is None:
                mic = _mic(parts[1])
            if ticker is None:
                ticker = normalize_ticker(parts[2])

    if mic is None or mic not in _US_MICS:
        return None
    if ticker is None:
        return None
    return ticker.replace(".", "-")


def cached_figi_resolver(
    cache_dir: Path,
) -> Callable[[str | None, str | None, str | None], str | None]:
    """Build a symbol resolver that falls back to the cached OpenFIGI mappings.

    The returned callable first tries :func:`symbol_for`; when that returns
    ``None`` and the security id is ``ISIN:``, ``CUSIP:`` or ``FIGI:``, it reads
    the JSON cache written by :class:`etfh.secmaster.openfigi.FigiLookup`
    (``id_type``/``value``/``mic``/``exch_code``/``composite_figi``/``ambiguous``
    files, read-only) and returns the ticker of the security's US listing.

    The cache is keyed by identifier, so the fallback is a two-step lookup: the
    security id's cached composite FIGI, then a cached ``TICKER`` entry for that
    same composite that names a US venue (``exchCode`` ``US`` or a venue that
    maps to a US MIC). A composite with more than one US ticker is ambiguous and
    resolves to ``None``. Tickers come back in Tiingo form (``BRK/B`` and
    ``BRK.B`` → ``BRK-B``).

    The cache index and every answer are kept in memory for the lifetime of the
    resolver, so a run reads the cache once. A missing directory, an unreadable
    file or a cache entry that does not match the documented shape is ignored:
    without usable evidence the resolver behaves exactly like :func:`symbol_for`.
    """

    cache = _FigiCache(Path(cache_dir))
    answers: dict[tuple[str | None, str | None, str | None], str | None] = {}

    def resolve(
        security_id: str | None,
        security_ticker: str | None,
        exchange: str | None,
    ) -> str | None:
        key = (security_id, security_ticker, exchange)
        if key not in answers:
            symbol = symbol_for(security_id, security_ticker, exchange)
            if symbol is None:
                symbol = cache.symbol_for_security_id(security_id)
            answers[key] = symbol
        return answers[key]

    return resolve


class _FigiCache:
    """Read-only, lazily loaded index over a ``FigiLookup`` cache directory."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self._loaded = False
        #: ``(id_type, value)`` → composite FIGI, for the non-ticker lookups.
        self._composites: dict[tuple[str, str], str] = {}
        #: composite FIGI → the US tickers cached for it.
        self._tickers: dict[str, set[str]] = {}

    def symbol_for_security_id(self, security_id: str | None) -> str | None:
        """The US ticker cached for one security id, or ``None``."""
        if security_id is None:
            return None
        composite: str | None
        if security_id.startswith(_FIGI_PREFIX):
            composite = security_id[len(_FIGI_PREFIX) :].strip().upper() or None
        else:
            composite = None
            for prefix, id_type in _CACHE_ID_TYPES:
                if security_id.startswith(prefix):
                    value = security_id[len(prefix) :].strip().upper()
                    composite = self._composite(id_type, value) if value else None
                    break
        if composite is None:
            return None
        return self._ticker(composite)

    def _composite(self, id_type: str, value: str) -> str | None:
        self._load()
        return self._composites.get((id_type, value))

    def _ticker(self, composite: str) -> str | None:
        self._load()
        tickers = self._tickers.get(composite)
        if not tickers or len(tickers) > 1:
            return None
        return next(iter(tickers))

    def _load(self) -> None:
        """Index every cache file once; unreadable or odd files are skipped."""
        if self._loaded:
            return
        self._loaded = True
        for path in sorted(self.cache_dir.glob("*.json")):
            entry = _read_entry(path)
            if entry is None:
                continue
            id_type, value, mic, exch_code, composite = entry
            if id_type == "TICKER":
                if composite is None or not _is_us_listing(mic, exch_code):
                    continue
                ticker = _tiingo_symbol(value)
                if ticker is not None:
                    self._tickers.setdefault(composite, set()).add(ticker)
            elif composite is not None and value:
                self._composites[(id_type, value)] = composite


def _read_entry(path: Path) -> tuple[str, str, str | None, str | None, str | None] | None:
    """One cache file as ``(id_type, value, mic, exch_code, composite)``.

    Mirrors :meth:`etfh.secmaster.openfigi.FigiLookup._read_cache`: a file that
    is not JSON, not an object, or that says ``ambiguous`` with a composite is
    not evidence. The cached key is upper-cased the way ``FigiLookup`` writes
    it, so lookups match without case folding at every call.
    """
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    id_type = payload.get("id_type")
    value = payload.get("value")
    if not isinstance(id_type, str) or not isinstance(value, str) or not value:
        return None
    composite = payload.get("composite_figi")
    if composite is not None and not isinstance(composite, str):
        return None
    if payload.get("ambiguous") is True and composite is not None:
        return None
    mic = payload.get("mic")
    exch_code = payload.get("exch_code")
    cleaned_mic = mic.strip().upper() if isinstance(mic, str) and mic.strip() else None
    cleaned_exch = (
        exch_code.strip().upper() if isinstance(exch_code, str) and exch_code.strip() else None
    )
    return (
        id_type.strip().upper(),
        value.strip().upper(),
        cleaned_mic,
        cleaned_exch,
        composite.strip().upper() if isinstance(composite, str) and composite.strip() else None,
    )


def _is_us_listing(mic: str | None, exch_code: str | None) -> bool:
    """True when a cached ticker entry names a US listing.

    ``exchCode`` ``US`` is OpenFIGI's US country composite; a single-venue entry
    carries either the venue code (``UN``, ``UW``, ``UP``) or the MIC itself, and
    both go through the same alias table as :func:`symbol_for`.
    """
    if exch_code == _US_COMPOSITE:
        return True
    return any(label is not None and _mic(label) in _US_MICS for label in (mic, exch_code))


def _tiingo_symbol(ticker: str) -> str | None:
    """A cached ticker in Tiingo form: ``BRK/B`` and ``BRK.B`` → ``BRK-B``."""
    cleaned = ticker.strip().upper().translate(_TICKER_SEPARATORS)
    return cleaned or None
