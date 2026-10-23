"""Fund metrics from the Purpose daily fund JSON (no extra fetch; D65).

The document already collected as ``purpose_daily`` carries:

- ``nav_table``: ``{series: {YYYY-MM-DD: {"nav": <float>, ...}}}`` — a NAV
  history for every series, including the ETF series;
- ``series``: series metadata (``code``, ``currency``, ``nav_updated``,
  ``default``);
- ``details``: ``{series: [{"name": "aum", "val": "$481.2M"}, ...]}`` — the
  fund's total net assets, stated once for the fund and repeated for each
  series.

The parser reads the ETF series (the one named ``ETF``, else the default
series), emits one row per NAV date, and attaches the AUM to the series'
``nav_updated`` date only. Shares outstanding are not published by Purpose;
when AUM and NAV are present for the same date the parser derives
``shares_outstanding = aum / nav`` and records both ``aum_scope=fund`` and
``shares_derived=fund_aum/etf_nav`` in its notes. A missing figure stays null,
never zero (D67), and a date appears at most once in the frame.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from etfh.types import FileFormat, ParsedFile, RawFile

__all__ = [
    "METRICS_PARSERS",
    "NoMetricsError",
    "PurposeMetricsParser",
    "metrics_from_payload",
]

_COLUMNS = [
    "fund_ticker",
    "as_of_date",
    "nav_per_unit",
    "shares_outstanding",
    "aum",
    "currency",
    "source_id",
    "raw_file_sha256",
]

_SOURCE_ID = "purpose_metrics"
_RAW_SOURCE_ID = "purpose_daily"
_METRIC_NAMES = frozenset({"nav", "aum"})
_TORONTO = ZoneInfo("America/Toronto")


class NoMetricsError(ValueError):
    """The document is a Purpose daily file without a metrics section."""


_MONEY = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)\s*([KMB])?", re.IGNORECASE)
_SCALES = {"": 1.0, "K": 1_000.0, "M": 1_000_000.0, "B": 1_000_000_000.0}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    """A numeric JSON value or a numeric string; null for booleans and junk."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = value.strip().replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _money(value: Any) -> float | None:
    """Parse ``$481.2M``/``$4.4B``/``$0`` into currency units; null if absent."""
    text = _text(value)
    if text is None:
        return None
    match = _MONEY.search(text)
    if match is None:
        return None
    try:
        return float(match.group(1).replace(",", "")) * _SCALES[(match.group(2) or "").upper()]
    except (ValueError, KeyError):
        return None


def _series_key(payload: dict[str, Any]) -> str | None:
    """Select the ETF series: ``ETF`` first, else the default series."""
    series = payload.get("series")
    if not isinstance(series, dict) or not series:
        return None
    if isinstance(series.get("ETF"), dict):
        return "ETF"
    for key in sorted(series):
        entry = series[key]
        if isinstance(entry, dict) and entry.get("default") is True:
            return str(key)
    return str(sorted(series)[0])


def _series_entry(payload: dict[str, Any], key: str | None) -> dict[str, Any]:
    series = payload.get("series")
    if not isinstance(series, dict) or key is None:
        return {}
    entry = series.get(key)
    return entry if isinstance(entry, dict) else {}


def _aum_for(payload: dict[str, Any], key: str | None) -> float | None:
    details = payload.get("details")
    if not isinstance(details, dict) or key is None:
        return None
    entries = details.get(key)
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if _text(entry.get("name")) == "aum":
            return _money(entry.get("val"))
    return None


def _nav_details(payload: dict[str, Any], key: str | None) -> tuple[date, float] | None:
    """Fallback NAV from ``details`` when ``nav_table`` has no row for the series."""
    details = payload.get("details")
    if not isinstance(details, dict) or key is None:
        return None
    entries = details.get(key)
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or _text(entry.get("name")) != "nav":
            continue
        nav = _money(entry.get("val"))
        if nav is None:
            return None
        series = _series_entry(payload, key)
        nav_date = _date(series.get("nav_updated")) or _date(payload.get("updated"))
        if nav_date is None:
            return None
        return nav_date, nav
    return None


def _has_metrics_section(payload: dict[str, Any]) -> bool:
    """Whether the document carries a NAV table or a NAV/AUM detail entry."""
    nav_table = payload.get("nav_table")
    if isinstance(nav_table, dict):
        for table in nav_table.values():
            if isinstance(table, dict) and table:
                return True
    details = payload.get("details")
    if isinstance(details, dict):
        for entries in details.values():
            if not isinstance(entries, list):
                continue
            if any(
                isinstance(entry, dict) and _text(entry.get("name")) in _METRIC_NAMES
                for entry in entries
            ):
                return True
    return False


def metrics_from_payload(
    payload: dict[str, Any],
) -> tuple[date, dict[date, tuple[float | None, float | None, float | None]], str | None, str]:
    """Extract ``(as_of_date, rows, currency, series_key)`` from a Purpose document.

    ``rows`` maps each date to ``(nav, shares, aum)`` and contains every date at
    most once. The AUM is attached only to the series' NAV-updated date. Raises
    ``ValueError`` when the document has no usable NAV rows.
    """
    if not _has_metrics_section(payload):
        raise NoMetricsError("Purpose document has no nav_table or details nav/aum entries")
    key = _series_key(payload)
    series = _series_entry(payload, key)
    currency = _text(series.get("currency")) or "CAD"

    values: dict[date, dict[str, float | None]] = {}
    nav_table = payload.get("nav_table")
    if isinstance(nav_table, dict) and key is not None:
        table = nav_table.get(key)
        if isinstance(table, dict):
            for date_text, entry in table.items():
                row_date = _date(date_text)
                if row_date is None or not isinstance(entry, dict):
                    continue
                values.setdefault(row_date, {})["nav"] = _number(entry.get("nav"))

    if not values:
        fallback = _nav_details(payload, key)
        if fallback is None:
            raise ValueError("Purpose document has no nav_table rows and no details.nav")
        values[fallback[0]] = {"nav": fallback[1]}

    aum = _aum_for(payload, key)
    if aum is not None:
        aum_date = _date(series.get("nav_updated")) or _date(payload.get("updated"))
        if aum_date is None:
            aum_date = max(values)
        values.setdefault(aum_date, {})["aum"] = aum

    rows: dict[date, tuple[float | None, float | None, float | None]] = {}
    for row_date in sorted(values):
        entry = values[row_date]
        nav = entry.get("nav")
        row_aum = entry.get("aum")
        shares = row_aum / nav if row_aum is not None and nav is not None and nav > 0 else None
        rows[row_date] = (nav, shares, row_aum)
    if not rows:
        raise ValueError("Purpose document produced no metrics rows")
    return max(rows), rows, currency, key or ""


def _empty_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "fund_ticker": pl.Series([], dtype=pl.String),
            "as_of_date": pl.Series([], dtype=pl.Date),
            "nav_per_unit": pl.Series([], dtype=pl.Float64),
            "shares_outstanding": pl.Series([], dtype=pl.Float64),
            "aum": pl.Series([], dtype=pl.Float64),
            "currency": pl.Series([], dtype=pl.String),
            "source_id": pl.Series([], dtype=pl.String),
            "raw_file_sha256": pl.Series([], dtype=pl.String),
        }
    ).select(_COLUMNS)


class PurposeMetricsParser:
    """Parse a stored Purpose daily JSON into fund-metrics rows."""

    source_id = _SOURCE_ID
    formats = frozenset({FileFormat.json})

    def can_parse(self, raw: RawFile) -> bool:
        return raw.source_id == _RAW_SOURCE_ID and raw.ext.lower() == "json"

    def parse(self, raw: RawFile) -> ParsedFile:
        payload = json.loads(Path(raw.path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{raw.path}: fund document must be a JSON object")
        try:
            as_of, rows, currency, series_key = metrics_from_payload(payload)
        except NoMetricsError:
            portfolio = payload.get("portfolio")
            portfolio_date = _date(portfolio.get("dt")) if isinstance(portfolio, dict) else None
            as_of = (
                _date(payload.get("updated"))
                or portfolio_date
                or raw.as_of_hint
                or raw.fetched_at.astimezone(_TORONTO).date()
            )
            return ParsedFile(
                raw=raw,
                as_of_date=as_of,
                published_at=None,
                frame=_empty_frame(),
                notes=["metrics_present=false"],
            )
        notes = [
            f"metrics_source={self.source_id}",
            f"nav_table_series={series_key}",
            "aum_scope=fund",
        ]
        if any(shares is not None for _, shares, _ in rows.values()):
            notes.append("shares_derived=fund_aum/etf_nav")
        frame = pl.DataFrame(
            {
                "fund_ticker": pl.Series([raw.fund_ticker] * len(rows), dtype=pl.String),
                "as_of_date": pl.Series(list(rows), dtype=pl.Date),
                "nav_per_unit": pl.Series([nav for nav, _, _ in rows.values()], dtype=pl.Float64),
                "shares_outstanding": pl.Series(
                    [shares for _, shares, _ in rows.values()], dtype=pl.Float64
                ),
                "aum": pl.Series([aum for _, _, aum in rows.values()], dtype=pl.Float64),
                "currency": pl.Series([currency] * len(rows), dtype=pl.String),
                "source_id": pl.Series([self.source_id] * len(rows), dtype=pl.String),
                "raw_file_sha256": pl.Series([raw.sha256] * len(rows), dtype=pl.String),
            }
        ).select(_COLUMNS)
        return ParsedFile(
            raw=raw,
            as_of_date=as_of,
            published_at=None,
            frame=frame,
            notes=notes,
        )


METRICS_PARSERS = [PurposeMetricsParser()]
