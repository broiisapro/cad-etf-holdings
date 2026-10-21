"""Parser for Purpose Investments daily fund JSON.

The public fund endpoint returns a large fund document. The portfolio date is
``portfolio.dt`` and the listed holdings are under ``portfolio.Level4.holdings``
(``name``, optional ``geo``/``sector`` and a percent ``weight`` such as
``"5.63%"``). The list may end with a ``Total`` row, which is not a holding.
The format carries no tickers, identifiers, exchanges, currencies or share
counts, so those columns stay null.

The holdings list is a securities list: option lines ("... Call 94 11/06/26"),
warrants, futures, forwards and swaps are classified as ``derivative``, FX
forwards as ``fx``, and cash accounts as ``cash``. Several funds never reach
100% because the source publishes the cash/other-asset share only in the
``Level1``–``Level3`` exposure breakdowns, not as a holding row (some funds list
a top-N subset). When the source itself reports a complete 100% exposure
breakdown but its holdings list totals less, the file is marked
``partial_list=true`` (D52) so validation does not force a sum the source never
published. A shortfall without that evidence stays unmarked: a parsing gap must
never hide behind the note.

Funds that publish a section list instead of ``Level4.holdings`` (for example a
fund-of-funds "Holding Breakdown" under ``portfolio.holdings_data``) are read
from that list; the sections are concatenated in document order.

The note ``weight_decimals=<n>`` records the finest precision printed for a
weight in the document (percentages are printed to two decimals today), so
validation can derive a rounding-aware weight-sum tolerance (D56).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from etfh.secmaster.identifiers import normalize_exchange
from etfh.types import FileFormat, ParsedFile, RawFile
from etfh.units import WeightUnit, check_unit, normalize_weights, parse_number

__all__ = ["PARSERS", "PurposeParser"]

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

_ASSET_CLASSES = {
    "equity": "equity",
    "fixed income": "fixed_income",
    "fixed_income": "fixed_income",
    "cash": "cash",
    "commodity": "commodity",
    "balanced": "balanced",
    "currency": "fx",
    "foreign currency": "fx",
    "fx": "fx",
    "derivative": "derivative",
    "derivatives": "derivative",
    "other": "other",
}
_TOTAL_NAMES = {"total", "totals", "total holdings", "total of holdings"}

# Option lines end with "Call <strike> <expiry>", e.g. "Apple Inc Put 320
# 10/23/26". The anchored pattern keeps ordinary names that merely contain
# "Call"/"Put" (company names) out of the derivative class.
_OPTION_LINE = re.compile(r"\b(?:call|put)\s+[\d.]+\s+\d{1,2}/\d{1,2}/\d{2,4}\s*$", re.IGNORECASE)
_DERIVATIVE_WORD = re.compile(
    r"\b(?:option|futures?|forwards?|swaps?|warrants?|rights?)\b", re.IGNORECASE
)
_FX_WORD = re.compile(r"\b(?:fx|foreign exchange)\b", re.IGNORECASE)
# Percentages are printed to two decimals, so a complete list can round to
# within a few hundredths of 100. A shortfall beyond this is real.
_COMPLETE_TOLERANCE = 0.05
# A printed number as the document writes weights: optional sign, digits with
# an optional fraction, and an optional exponent.
_PRINTED_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.(\d*))?|\.(\d+))(?:[eE]([+-]?\d+))?$")


def _printed_decimals(value: object) -> int | None:
    """Decimal places printed for one weight value, or ``None`` if not numeric."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text.endswith("%"):
        text = text[:-1].strip()
    match = _PRINTED_NUMBER.fullmatch(text)
    if match is None:
        return None
    fraction = match.group(1) or match.group(2) or ""
    exponent = int(match.group(3) or 0)
    return max(len(fraction) - exponent, 0)


def _weight_decimals(values: Iterable[object]) -> int:
    """Finest number of decimals printed for the weights in one file (D56)."""
    printed = [decimals for decimals in map(_printed_decimals, values) if decimals is not None]
    return max(printed) if printed else 0


def _date_value(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _label_asset_class(label: str) -> str:
    """Map a source asset-class label to the schema's asset classes."""
    lowered = label.strip().lower()
    mapped = _ASSET_CLASSES.get(lowered)
    if mapped is not None:
        return mapped
    if "fixed income" in lowered or "bond" in lowered:
        return "fixed_income"
    if "equit" in lowered:
        return "equity"
    if "cash" in lowered:
        return "cash"
    if "commodit" in lowered:
        return "commodity"
    if "balanced" in lowered:
        return "balanced"
    return "other"


def _fund_asset_class(payload: dict[str, Any]) -> str:
    classes = payload.get("assetClass")
    if not isinstance(classes, list):
        return "other"
    for entry in classes:
        if not isinstance(entry, dict):
            continue
        fields = entry.get("fields")
        if not isinstance(fields, dict):
            continue
        label = _text(fields.get("className")) or _text(fields.get("displayName"))
        if label is not None:
            return _label_asset_class(label)
    return "other"


def _row_asset_class(name: str, fund_class: str) -> str:
    """Classify one holding line: derivative, fx or cash, else the fund class."""
    lowered = name.lower()
    if "cash" in lowered:
        return "cash"
    if _FX_WORD.search(name):
        return "fx"
    if _OPTION_LINE.search(name) or _DERIVATIVE_WORD.search(name):
        return "derivative"
    return fund_class


def _has_full_exposure_breakdown(payload: dict[str, Any]) -> bool:
    """True when the document reports an exposure breakdown that totals 100%.

    Purpose publishes ``Level1``–``Level3`` breakdowns (by asset class, credit
    profile, geography, sector) whose values sum to ~100%. One such complete
    breakdown proves the source knows the whole portfolio while its holdings
    list totals less: the missing share is cash/other assets it does not list
    as a holding row.
    """
    portfolio = payload.get("portfolio")
    if not isinstance(portfolio, dict):
        return False
    for level in ("Level1", "Level2", "Level3"):
        node = portfolio.get(level)
        if not isinstance(node, dict):
            continue
        for breakdown in node.values():
            if not isinstance(breakdown, dict) or len(breakdown) < 2:
                continue
            values = list(breakdown.values())
            numeric = all(
                isinstance(value, (int, float)) and not isinstance(value, bool) for value in values
            )
            if not numeric:
                continue
            if abs(sum(values) - 100.0) <= 1.0:
                return True
    return False


def _holding_rows(payload: dict[str, Any]) -> list[Any]:
    portfolio = payload.get("portfolio")
    if not isinstance(portfolio, dict):
        raise ValueError("missing portfolio section")
    level4 = portfolio.get("Level4")
    if isinstance(level4, dict) and isinstance(level4.get("holdings"), list):
        return list(level4["holdings"])
    if isinstance(portfolio.get("holdings"), list):
        return list(portfolio["holdings"])
    data = portfolio.get("holdings_data")
    if isinstance(data, list):
        return list(data)
    if isinstance(data, dict):
        # Some documents (for example the fund-of-funds breakdown) carry the
        # holdings as a dict of named sections; concatenate the section lists
        # in document order.
        sections = [value for value in data.values() if isinstance(value, list)]
        if sections:
            return [row for section in sections for row in section]
    raise ValueError("missing portfolio holdings")


class PurposeParser:
    """Parse the Purpose daily fund JSON into a fraction-weighted frame."""

    source_id = "purpose_daily"
    formats = frozenset({FileFormat.json})

    def can_parse(self, raw: RawFile) -> bool:
        return raw.source_id == self.source_id and raw.ext.lower() == "json"

    def parse(self, raw: RawFile) -> ParsedFile:
        payload = json.loads(Path(raw.path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{raw.path}: fund document must be a JSON object")
        portfolio = payload.get("portfolio")
        if not isinstance(portfolio, dict):
            raise ValueError(f"{raw.path}: missing portfolio section")
        as_of_date = _date_value(portfolio.get("dt"))
        if as_of_date is None:
            raise ValueError(f"{raw.path}: no portfolio date found")
        fund_class = _fund_asset_class(payload)
        holdings = _holding_rows(payload)
        rows: list[dict[str, Any]] = []
        unparsed_weights = 0
        weight_cells: list[object] = []
        for holding in holdings:
            if not isinstance(holding, dict):
                continue
            name = _text(holding.get("name")) or _text(holding.get("security_name"))
            if name is None or name.lower() in _TOTAL_NAMES:
                continue
            weight_cell = holding.get("weight")
            weight = parse_number(_text(weight_cell))
            if weight is None:
                # A named row without a parseable weight may be a parsing gap;
                # it must not be silently hidden behind partial_list (D52).
                unparsed_weights += 1
                continue
            weight_cells.append(weight_cell)
            sector = _text(holding.get("sector"))
            country = _text(holding.get("geo")) or _text(holding.get("country"))
            asset_class = "cash" if fund_class == "cash" else _row_asset_class(name, fund_class)
            rows.append(
                {
                    "security_name": name,
                    "security_ticker": _text(holding.get("ticker")),
                    "exchange": normalize_exchange(_text(holding.get("exchange"))),
                    "isin": _text(holding.get("isin")),
                    "cusip": _text(holding.get("cusip")),
                    "asset_class": asset_class,
                    "sector": sector or "Unknown",
                    "country": country or "Unknown",
                    "currency": _text(holding.get("currency")),
                    "weight": weight,
                    "shares": parse_number(_text(holding.get("shares"))),
                    "market_value": parse_number(_text(holding.get("market_value"))),
                }
            )
        if not rows:
            raise ValueError(f"{raw.path}: no holdings rows found")
        declared = WeightUnit.percent
        weights = [row["weight"] for row in rows]
        unit_check = check_unit(pl.Series("weight", weights, dtype=pl.Float64), declared)
        notes = [
            f"declared_unit={declared.value}",
            f"weight_decimals={_weight_decimals(weight_cells)}",
        ]
        if unit_check.suggested is not None:
            # The total matches the other unit: a real unit contradiction.
            notes.append(f"unit_check={unit_check.detail()}")
        elif (
            unparsed_weights == 0
            and unit_check.total < 100.0 - _COMPLETE_TOLERANCE
            and _has_full_exposure_breakdown(payload)
        ):
            # The source reports a complete 100% breakdown but lists only part
            # of the portfolio as holdings (cash/other assets or a top-N list).
            notes.append("partial_list=true")
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


PARSERS = [PurposeParser()]
