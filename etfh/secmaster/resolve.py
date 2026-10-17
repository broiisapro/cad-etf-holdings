"""Assign a stable ``security_id`` to every holding row (D23, D33, D41).

Resolution uses only time-invariant evidence: the row's own provided ISIN or
CUSIP, its exchange-qualified ticker, the human-reviewed overrides file and
(optionally, off by default) a cached OpenFIGI mapping. Links are never
learned from other holdings files, so a file known later cannot change how an
earlier file's rows resolve.

The same ticker on two exchanges never merges: a row without an exchange
cannot form the ``TKR:<MIC>:<TICKER>`` key and stays unmatched instead.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import polars as pl

from etfh.secmaster.identifiers import (
    cusip_is_valid,
    cusip_to_isin,
    isin_is_valid,
    normalize_exchange,
    normalize_ticker,
)
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


class SecurityMaster:
    """Resolve holding rows to ``security_id`` values.

    Resolution order per row:

    1. reviewed overrides, most specific first (``isin``, ``cusip``,
       ``ticker``, ``name``);
    2. cash rows → ``CASH:<CCY>``;
    3. fx and derivative rows → ``DERIV:<fund_ticker>:<normalized name>``;
    4. provided valid ISIN → ``ISIN:<isin>``;
    5. provided valid CUSIP → ``ISIN:<derived>`` for CA/US, else
       ``CUSIP:<cusip>``;
    6. normalized MIC + ticker → ``TKR:<MIC>:<TICKER>``;
    7. otherwise null; the row appears in the unmatched report.

    Invalid provided identifiers are ignored and counted in
    :attr:`invalid_identifier_count` for the most recent :meth:`assign` call,
    never used.
    """

    def __init__(self, overrides: Sequence[Override] = ()) -> None:
        self.overrides = list(overrides)
        self.invalid_identifier_count = 0
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

        The row count and row order are preserved exactly. ``figi`` lookups
        are a later addition (issue #24); with no lookup the result depends
        only on the frame and the overrides.
        """
        missing = [column for column in _REQUIRED_COLUMNS if column not in frame.columns]
        if missing:
            raise ValueError(f"holdings frame is missing columns: {', '.join(missing)}")
        security_ids: list[str | None] = []
        invalid = 0
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
            security_ids.append(security_id)
            invalid += row_invalid
        self.invalid_identifier_count = invalid
        return frame.with_columns(pl.Series("security_id", security_ids, dtype=pl.String))

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
            ccy = _clean(currency)
            return (f"CASH:{ccy}" if ccy is not None else None), 0
        if normalized_class in _DERIVATIVE_CLASSES:
            return _derivative_key(fund_ticker, security_name), 0

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


def _derivative_key(fund_ticker: str, security_name: str) -> str:
    normalized = re.sub(r"[^0-9A-Z]+", " ", security_name.upper()).strip()
    return f"DERIV:{fund_ticker}:{normalized}"
