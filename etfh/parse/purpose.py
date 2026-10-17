"""Parser for Purpose Investments daily fund JSON.

The public fund endpoint returns a large fund document. The portfolio date is
``portfolio.dt`` and the listed holdings are under ``portfolio.Level4.holdings``
(``name``, optional ``geo``/``sector`` and a percent ``weight`` such as
``"5.63%"``). The list may end with a ``Total`` row, which is not a holding.
The format carries no tickers, identifiers, exchanges, currencies or share
counts, so those columns stay null.
"""

from __future__ import annotations

import json
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
            return _ASSET_CLASSES.get(label.lower(), "other")
    return "other"


def _holding_rows(payload: dict[str, Any]) -> list[Any]:
    portfolio = payload.get("portfolio")
    if not isinstance(portfolio, dict):
        raise ValueError("missing portfolio section")
    level4 = portfolio.get("Level4")
    if isinstance(level4, dict) and isinstance(level4.get("holdings"), list):
        return list(level4["holdings"])
    if isinstance(portfolio.get("holdings"), list):
        return list(portfolio["holdings"])
    if isinstance(portfolio.get("holdings_data"), list):
        return list(portfolio["holdings_data"])
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
        for holding in holdings:
            if not isinstance(holding, dict):
                continue
            name = _text(holding.get("name")) or _text(holding.get("security_name"))
            if name is None or name.lower() in _TOTAL_NAMES:
                continue
            weight = parse_number(_text(holding.get("weight")))
            if weight is None:
                continue
            sector = _text(holding.get("sector"))
            country = _text(holding.get("geo")) or _text(holding.get("country"))
            asset_class = fund_class
            if fund_class == "cash" or "cash" in name.lower():
                asset_class = "cash"
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
        notes = [f"declared_unit={declared.value}"]
        if not unit_check.consistent:
            notes.append(f"unit_check={unit_check.detail()}")
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
