"""Assign a stable ``security_id`` to every holding row (D23, D33, D41).

Resolution uses only time-invariant evidence: the row's own provided ISIN or
CUSIP, its exchange-qualified ticker, the human-reviewed overrides file and
(optionally, off by default) a cached OpenFIGI mapping. Links are never
learned from other holdings files, so a file known later cannot change how an
earlier file's rows resolve.

The same ticker on two exchanges never merges: a row without a known exchange
cannot form the ``TKR:<MIC>:<TICKER>`` key and stays unmatched instead.

Non-security rows get explicit ids (D33): cash rows ``CASH:<CCY>`` (or
``CASH:XXX``, ISO 4217's "no currency", when the file states no currency) and
fx/derivative rows ``DERIV:<fund>:<key>``. Derivative keys are unique per
instrument within a file: the normalized ``security_ticker`` when the row has
one, otherwise the normalized full name (which carries option strike/expiry
tokens); rows that still collide in one file get a stable ordinal suffix
``#2``, ``#3`` ... in file order (interfaces §3).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

import polars as pl

from etfh.secmaster.identifiers import (
    composite_code,
    cusip_is_valid,
    cusip_to_isin,
    isin_is_valid,
    normalize_exchange,
    normalize_ticker,
)
from etfh.secmaster.openfigi import CacheKey, FigiLookup
from etfh.secmaster.overrides import Override

__all__ = ["SecurityMaster"]

_REQUIRED_COLUMNS = (
    "fund_ticker",
    "security_name",
    "security_ticker",
    "exchange",
    "isin",
    "cusip",
    "asset_class",
    "country",
    "currency",
)

_MIC_PATTERN = re.compile(r"^[A-Z0-9]{4}$")
_CASH_CLASSES = frozenset({"cash"})
_DERIVATIVE_CLASSES = frozenset({"fx", "derivative"})
_NO_CURRENCY = "XXX"


class SecurityMaster:
    """Resolve holding rows to ``security_id`` values.

    Resolution order per row:

    1. reviewed overrides, most specific first (``isin``, ``cusip``,
       ``ticker``, ``name``);
    2. cash rows → ``CASH:<CCY>`` (``CASH:XXX`` without a stated currency);
    3. fx and derivative rows → ``DERIV:<fund_ticker>:<normalized ticker>``,
       else ``DERIV:<fund_ticker>:<normalized name>``; colliding rows in the
       same frame get ``#2``, ``#3`` ... suffixes in file order;
    4. provided valid ISIN → ``ISIN:<isin>``;
    5. provided valid CUSIP → ``ISIN:<derived>`` for CA/US, else
       ``CUSIP:<cusip>``;
    6. normalized MIC + ticker → ``TKR:<MIC>:<TICKER>``;
    7. otherwise null; the row appears in the unmatched report.

    Invalid provided identifiers are ignored and counted in
    :attr:`invalid_identifier_count` for the most recent :meth:`assign` call,
    never used.

    When a :class:`~etfh.secmaster.openfigi.FigiLookup` is passed (D47),
    :meth:`prefetch` can batch all lookups of a build, and :meth:`assign`
    replaces the D23 id of every non-cash, non-derivative row whose ISIN,
    MIC+ticker or composite code+ticker maps to exactly one composite FIGI with
    ``FIGI:<composite>``. Ambiguous mappings keep the D23 id and are counted;
    reviewed overrides are never replaced. With ``figi=None`` the result is
    identical to the no-lookup behaviour.
    """

    def __init__(
        self,
        overrides: Sequence[Override] = (),
        figi: FigiLookup | None = None,
    ) -> None:
        self.overrides = list(overrides)
        self.figi = figi
        self.invalid_identifier_count = 0
        self.figi_assigned_count = 0
        self.figi_ambiguous_count = 0
        self.figi_unmapped_count = 0
        self._by_isin: dict[str, str] = {}
        self._by_cusip: dict[str, str] = {}
        self._by_ticker: dict[str, str] = {}
        self._by_name: dict[tuple[str, str], str] = {}
        for override in self.overrides:
            if override.match_type == "isin":
                self._by_isin[override.match_value] = override.security_id
            elif override.match_type == "cusip":
                self._by_cusip[override.match_value] = override.security_id
            elif override.match_type == "ticker":
                self._by_ticker[override.match_value] = override.security_id
            else:
                fund_ticker, security_name = override.match_value.split("|", 1)
                self._by_name[(fund_ticker, security_name)] = override.security_id

    def assign(self, frame: pl.DataFrame) -> pl.DataFrame:
        """Return ``frame`` with a nullable Utf8 ``security_id`` column added.

        The row count and row order are preserved exactly. With a ``figi``
        lookup, every non-cash, non-derivative row whose ISIN (provided or
        structurally derived from a CA/US CUSIP), MIC+ticker or composite
        code+ticker maps to exactly one composite FIGI gets ``FIGI:<composite>``
        instead of its D23 id; ambiguous mappings keep the D23 id and are
        counted (D47). Reviewed overrides always win. With ``figi=None`` the
        result depends only on the frame and the overrides, as before.
        """
        missing = [column for column in _REQUIRED_COLUMNS if column not in frame.columns]
        if missing:
            raise ValueError(f"holdings frame is missing columns: {', '.join(missing)}")
        security_ids: list[str | None] = []
        invalid = 0
        derivative_counts: dict[str, int] = {}
        rows = zip(
            frame["fund_ticker"].to_list(),
            frame["security_name"].to_list(),
            frame["security_ticker"].to_list(),
            frame["exchange"].to_list(),
            frame["isin"].to_list(),
            frame["cusip"].to_list(),
            frame["asset_class"].to_list(),
            frame["country"].to_list(),
            frame["currency"].to_list(),
            strict=True,
        )
        for (
            fund_ticker,
            security_name,
            security_ticker,
            exchange,
            isin,
            cusip,
            asset_class,
            country,
            currency,
        ) in rows:
            security_id, row_invalid = self._resolve(
                fund_ticker=fund_ticker,
                security_name=security_name,
                security_ticker=security_ticker,
                exchange=exchange,
                isin=isin,
                cusip=cusip,
                asset_class=asset_class,
                country=country,
                currency=currency,
            )
            if security_id is not None and security_id.startswith("DERIV:"):
                seen = derivative_counts.get(security_id, 0) + 1
                derivative_counts[security_id] = seen
                if seen > 1:
                    security_id = f"{security_id}#{seen}"
            security_ids.append(security_id)
            invalid += row_invalid
        self.invalid_identifier_count = invalid
        if self.figi is not None:
            security_ids = self._apply_figi(frame, security_ids)
        return frame.with_columns(pl.Series("security_id", security_ids, dtype=pl.String))

    def prefetch(self, frames: Iterable[pl.DataFrame]) -> None:
        """Warm the FIGI cache for all keys the given frames will need.

        Batching all lookups of a build into one pass keeps the request count
        (and the OpenFIGI rate limit) under control. A no-op when no
        :class:`~etfh.secmaster.openfigi.FigiLookup` was passed. Callers may
        still call :meth:`assign` without prefetching; missing keys are then
        looked up lazily.
        """
        if self.figi is None:
            return
        keys: list[CacheKey] = []
        for frame in frames:
            missing = [column for column in _REQUIRED_COLUMNS if column not in frame.columns]
            if missing:
                raise ValueError(f"holdings frame is missing columns: {', '.join(missing)}")
            keys.extend(key for _, key in self._figi_candidates(frame))
        if keys:
            self.figi.lookup_many(keys)

    def _apply_figi(self, frame: pl.DataFrame, security_ids: list[str | None]) -> list[str | None]:
        """Replace D23 ids with ``FIGI:<composite>`` where the evidence maps."""
        assert self.figi is not None
        candidates = self._figi_candidates(frame)
        assigned = 0
        ambiguous = 0
        unmapped = 0
        if candidates:
            values = self.figi.lookup_many([key for _, key in candidates])
            for index, key in candidates:
                composite = values.get(key)
                if composite is not None:
                    security_ids[index] = f"FIGI:{composite}"
                    assigned += 1
                elif key in self.figi.ambiguous_keys:
                    ambiguous += 1
                else:
                    unmapped += 1
        self.figi_assigned_count = assigned
        self.figi_ambiguous_count = ambiguous
        self.figi_unmapped_count = unmapped
        return security_ids

    def _figi_candidates(self, frame: pl.DataFrame) -> list[tuple[int, CacheKey]]:
        """Rows eligible for a FIGI id, with their lookup key, in frame order.

        Eligible rows are not cash, fx or derivative rows and are not matched
        by a reviewed override (overrides win, D23). The key priority is the
        row's ISIN (provided or derived from a valid CA/US CUSIP), then
        MIC+ticker, then Bloomberg composite code+ticker (D47).
        """
        assert self.figi is not None
        candidates: list[tuple[int, CacheKey]] = []
        tickers = frame["security_ticker"].to_list()
        exchanges = frame["exchange"].to_list()
        isins = frame["isin"].to_list()
        cusips = frame["cusip"].to_list()
        asset_classes = frame["asset_class"].to_list()
        countries = frame["country"].to_list()
        fund_tickers = frame["fund_ticker"].to_list()
        names = frame["security_name"].to_list()
        for index in range(frame.height):
            normalized_class = (asset_classes[index] or "").strip().lower()
            if normalized_class in _CASH_CLASSES or normalized_class in _DERIVATIVE_CLASSES:
                continue
            provided_isin = _clean(isins[index])
            provided_cusip = _clean(cusips[index])
            ticker = normalize_ticker(tickers[index])
            mic = _mic(exchanges[index])
            if (
                self._match_override(
                    fund_ticker=fund_tickers[index],
                    security_name=names[index],
                    isin=provided_isin,
                    cusip=provided_cusip,
                    mic=mic,
                    ticker=ticker,
                )
                is not None
            ):
                continue
            key = _figi_key(
                isin=provided_isin,
                cusip=provided_cusip,
                country=countries[index],
                ticker=ticker,
                exchange=exchanges[index],
                mic=mic,
            )
            if key is not None:
                candidates.append((index, key))
        return candidates

    def _resolve(
        self,
        *,
        fund_ticker: str,
        security_name: str,
        security_ticker: str | None,
        exchange: str | None,
        isin: str | None,
        cusip: str | None,
        asset_class: str | None,
        country: str | None,
        currency: str | None,
    ) -> tuple[str | None, int]:
        provided_isin = _clean(isin)
        provided_cusip = _clean(cusip)
        mic = _mic(exchange)
        ticker = normalize_ticker(security_ticker)

        override = self._match_override(
            fund_ticker=fund_ticker,
            security_name=security_name,
            isin=provided_isin,
            cusip=provided_cusip,
            mic=mic,
            ticker=ticker,
        )
        if override is not None:
            return override, 0

        normalized_class = (asset_class or "").strip().lower()
        if normalized_class in _CASH_CLASSES:
            ccy = _clean(currency) or _NO_CURRENCY
            return f"CASH:{ccy}", 0
        if normalized_class in _DERIVATIVE_CLASSES:
            return _derivative_key(fund_ticker, ticker, security_name), 0

        invalid = 0
        if provided_isin is not None:
            if isin_is_valid(provided_isin):
                return f"ISIN:{provided_isin}", invalid
            invalid += 1
        if provided_cusip is not None:
            if cusip_is_valid(provided_cusip):
                derived = cusip_to_isin(provided_cusip, country or "")
                if derived is not None:
                    return f"ISIN:{derived}", invalid
                return f"CUSIP:{provided_cusip}", invalid
            invalid += 1
        if mic is not None and ticker is not None:
            return f"TKR:{mic}:{ticker}", invalid
        return None, invalid

    def _match_override(
        self,
        *,
        fund_ticker: str,
        security_name: str,
        isin: str | None,
        cusip: str | None,
        mic: str | None,
        ticker: str | None,
    ) -> str | None:
        if isin is not None and isin in self._by_isin:
            return self._by_isin[isin]
        if cusip is not None and cusip in self._by_cusip:
            return self._by_cusip[cusip]
        if mic is not None and ticker is not None:
            key = f"{mic}:{ticker}"
            if key in self._by_ticker:
                return self._by_ticker[key]
        name_key = (fund_ticker.strip(), security_name.strip())
        return self._by_name.get(name_key)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().upper()
    return cleaned or None


def _mic(exchange: str | None) -> str | None:
    """Normalize an exchange label or MIC to a MIC, or ``None``."""
    if exchange is None:
        return None
    mapped = normalize_exchange(exchange)
    if mapped is not None:
        return mapped
    cleaned = exchange.strip().upper()
    return cleaned if _MIC_PATTERN.match(cleaned) else None


def _derivative_key(fund_ticker: str, ticker: str | None, security_name: str) -> str:
    """Base ``DERIV:`` id for one fx/derivative row.

    The normalized instrument ticker wins when the row has one (it is the
    issuer's own instrument identifier); otherwise the normalized full name is
    used, which keeps option strike/expiry tokens. Rows whose base key still
    collides with an earlier row of the same file are resolved by
    :meth:`SecurityMaster.assign` with ordinal suffixes in file order.
    """
    if ticker is not None:
        return f"DERIV:{fund_ticker}:{ticker}"
    normalized = re.sub(r"[^0-9A-Z]+", " ", security_name.upper()).strip()
    return f"DERIV:{fund_ticker}:{normalized}"


def _figi_key(
    *,
    isin: str | None,
    cusip: str | None,
    country: str | None,
    ticker: str | None,
    exchange: str | None,
    mic: str | None,
) -> CacheKey | None:
    """The single highest-priority FIGI evidence for one row (D47).

    An ISIN wins (provided, or structurally derived from a valid CA/US CUSIP,
    which is the same evidence the D23 id uses); then MIC+ticker; then a
    Bloomberg country-composite code+ticker. Rows without any usable evidence
    return ``None`` and keep their D23 id.
    """
    if isin is not None and isin_is_valid(isin):
        return ("ID_ISIN", isin, None, None)
    if cusip is not None and cusip_is_valid(cusip):
        derived = cusip_to_isin(cusip, country or "")
        if derived is not None:
            return ("ID_ISIN", derived, None, None)
    if ticker is not None:
        if mic is not None:
            return ("TICKER", ticker, mic, None)
        composite = composite_code(exchange)
        if composite is not None:
            return ("TICKER", ticker, None, composite)
    return None
