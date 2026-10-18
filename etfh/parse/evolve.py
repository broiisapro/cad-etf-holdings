"""Parser for Evolve ETFs daily holdings CSV.

The static download (``/wp-content/uploads/holdings/<TICKER>.csv``) has a
single header row and no preamble:

``TICKER,PORTFOLIO_MWEIGHT,POSITION,SECURITY_NAME,...``

``PORTFOLIO_MWEIGHT`` is a fraction (sometimes rendered in scientific
notation, e.g. ``3.19E-02``). ``TICKER`` is a Bloomberg-style identifier:
``<symbol> <market> EQUITY`` plus, for derivatives, an expiry and option code
(``GOOG US 10/16/26 C360 EQUITY``). Derivative rows keep that whole descriptor
in ``security_ticker`` so two options on the same underlying (e.g. GOOG and
GOOGL calls both named "Alphabet Inc - CALL Option") stay distinguishable for
the security master's ``DERIV:`` ids. The raw market codes are passed through
to ``normalize_exchange`` unchanged (never guessed here) and listed in the
``exchange_codes=`` note so the mapping table can be extended from real
evidence.

The file states no portfolio date; the fund page states "Holdings As at
<date>", which the collector records as the raw file's as-of hint. The parser
therefore uses ``raw.as_of_hint`` and refuses to guess a date.

Files mix encodings: the HTTP header says UTF-8, but French accents in the
real files are latin-1. The parser tries UTF-8 first and falls back to
latin-1.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

import polars as pl

from etfh.secmaster.identifiers import normalize_exchange
from etfh.types import FileFormat, ParsedFile, RawFile
from etfh.units import WeightUnit, check_unit, normalize_weights, parse_number

__all__ = ["PARSERS", "EvolveParser"]

_SOURCE_ID = "evolve_daily"
_COLUMNS = [
    "fund_ticker",
    "as_of_date",
    "security_name",
    "security_ticker",
    "exchange",
    "isin",
    "cusip",
    "asset_class",
    "sector",
    "country",
    "currency",
    "weight",
    "shares",
    "market_value",
    "source_id",
    "raw_file_sha256",
]

# Whole words only: "Warranty Corp" and "Brightstone" are companies, not
# derivatives.
_DERIVATIVE_WORD = re.compile(r"\b(?:option|futures?|forwards?|swaps?|warrants?)\b", re.IGNORECASE)
# Bloomberg derivative tickers carry "<expiry> C|P<strike>" after the symbol,
# e.g. "GOOG US 10/16/26 C360". Some files name the row without the word
# "Option", so the ticker shape is checked as well.
_OPTION_TICKER = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\s+[CP]\d", re.IGNORECASE)
_CURRENCIES = {
    "CAD",
    "USD",
    "EUR",
    "GBP",
    "JPY",
    "CHF",
    "AUD",
    "HKD",
    "KRW",
    "DKK",
    "SEK",
    "NOK",
    "ILS",
}


def _read_text(path: Path) -> str:
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _split_ticker(value: str) -> tuple[str | None, str | None, str | None]:
    """Split ``<symbol> <market> [expiry option] EQUITY``.

    Returns the symbol, the market code and the full descriptor (symbol plus
    any expiry/strike tokens, without the trailing ``EQUITY``).
    """
    parts = value.split()
    if parts and parts[-1].upper() == "EQUITY":
        parts = parts[:-1]
    if not parts:
        return None, None, None
    symbol = parts[0].upper() or None
    market = parts[1].upper() if len(parts) >= 2 else None
    descriptor = " ".join(parts).upper() or None
    return symbol, market, descriptor


def _asset_class(name: str, ticker: str) -> str:
    lowered = name.lower()
    if "cash" in lowered or ticker.lower().startswith("cash"):
        return "cash"
    if _DERIVATIVE_WORD.search(name) or _OPTION_TICKER.search(ticker):
        return "derivative"
    return "equity"


def _cash_currency(name: str, raw_ticker: str) -> str | None:
    tokens = [str(token) for token in re.findall(r"\b[A-Z]{3}\b", f"{name} {raw_ticker}".upper())]
    for token in tokens:
        if token in _CURRENCIES:
            return token
    return None


class EvolveParser:
    """Parse an Evolve daily holdings CSV into fraction weights."""

    source_id = _SOURCE_ID
    formats = frozenset({FileFormat.csv})

    def can_parse(self, raw: RawFile) -> bool:
        return raw.source_id == self.source_id and raw.ext.lower() == "csv"

    def parse(self, raw: RawFile) -> ParsedFile:
        as_of_date = raw.as_of_hint
        if as_of_date is None:
            raise ValueError(
                f"{raw.path}: no as-of date; the collector records it from the fund page"
            )
        text = _read_text(Path(raw.path))
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None or "TICKER" not in reader.fieldnames:
            raise ValueError(f"{raw.path}: unexpected Evolve header {reader.fieldnames!r}")
        rows: list[dict[str, Any]] = []
        exchange_codes: set[str] = set()
        for record in reader:
            raw_ticker = _text(record.get("TICKER")) or ""
            name = _text(record.get("SECURITY_NAME"))
            if name is None:
                continue
            weight = parse_number(_text(record.get("PORTFOLIO_MWEIGHT")))
            if weight is None:
                continue
            symbol, market, descriptor = _split_ticker(raw_ticker)
            asset_class = _asset_class(name, raw_ticker)
            derivative = asset_class == "derivative"
            if market is not None and asset_class != "cash":
                exchange_codes.add(market)
            rows.append(
                {
                    "security_name": name,
                    "security_ticker": descriptor if derivative else symbol,
                    # Cash rows carry a currency where equities carry a market.
                    "exchange": None if asset_class == "cash" else normalize_exchange(market),
                    "isin": _text(record.get("ISIN")),
                    "cusip": _text(record.get("CUSIP")),
                    "asset_class": asset_class,
                    "sector": _text(record.get("GICS_SECTOR_NAME")) or "Unknown",
                    "country": _text(record.get("COUNTRY")) or "Unknown",
                    "currency": (
                        _cash_currency(name, raw_ticker) if asset_class == "cash" else None
                    ),
                    "weight": weight,
                    "shares": parse_number(_text(record.get("POSITION"))),
                    "market_value": parse_number(_text(record.get("VALUE"))),
                }
            )
        if not rows:
            raise ValueError(f"{raw.path}: no holdings rows found")
        declared = WeightUnit.fraction
        weights = [row["weight"] for row in rows]
        unit_check = check_unit(pl.Series("weight", weights, dtype=pl.Float64), declared)
        notes = [f"declared_unit={declared.value}"]
        if unit_check.suggested is not None:
            notes.append(f"unit_check={unit_check.detail()}")
        if exchange_codes:
            notes.append(f"exchange_codes={','.join(sorted(exchange_codes))}")
        count = len(rows)
        frame = pl.DataFrame(
            {
                "fund_ticker": pl.Series([raw.fund_ticker] * count, dtype=pl.String),
                "as_of_date": pl.Series([as_of_date] * count, dtype=pl.Date),
                "security_name": pl.Series([row["security_name"] for row in rows], dtype=pl.String),
                "security_ticker": pl.Series(
                    [row["security_ticker"] for row in rows], dtype=pl.String
                ),
                "exchange": pl.Series([row["exchange"] for row in rows], dtype=pl.String),
                "isin": pl.Series([row["isin"] for row in rows], dtype=pl.String),
                "cusip": pl.Series([row["cusip"] for row in rows], dtype=pl.String),
                "asset_class": pl.Series([row["asset_class"] for row in rows], dtype=pl.String),
                "sector": pl.Series([row["sector"] for row in rows], dtype=pl.String),
                "country": pl.Series([row["country"] for row in rows], dtype=pl.String),
                "currency": pl.Series([row["currency"] for row in rows], dtype=pl.String),
                "weight": normalize_weights(weights, declared),
                "shares": pl.Series([row["shares"] for row in rows], dtype=pl.Float64),
                "market_value": pl.Series([row["market_value"] for row in rows], dtype=pl.Float64),
                "source_id": pl.Series([raw.source_id] * count, dtype=pl.String),
                "raw_file_sha256": pl.Series([raw.sha256] * count, dtype=pl.String),
            }
        )
        return ParsedFile(
            raw=raw,
            as_of_date=as_of_date,
            published_at=None,
            frame=frame.select(_COLUMNS),
            notes=notes,
        )


PARSERS = [EvolveParser()]
