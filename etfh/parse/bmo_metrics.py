"""Parser for BMO ETF factsheet PDFs into fund metrics.

The factsheet states ``DATA AS OF <MON DD, YYYY>`` and a ``Fund
Characteristics`` block with ``Net Assets (MM)``, ``Number of Securities`` and
``Shares Outstanding``. Net assets are printed in millions, so the parser
multiplies by one million before writing ``aum``: the metrics contract stores
currency units, never millions (D66). The factsheet does not print a NAV per
unit; when both net assets and shares outstanding are present the parser
derives it as ``aum / shares`` and records ``nav_derived=aum/shares`` in its
notes. A missing figure stays null, never zero (D67).

BMO refreshes a factsheet monthly and some funds lag far behind that cadence, so
every parsed row carries ``stale_days=<n>``: whole days between the factsheet's
stated date and the date it was fetched in America/Toronto. A negative value
means the document states a date after the fetch (already a validation error
under ``future_as_of``). The note records how old the figure is; it never
changes a value.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pdfplumber
import polars as pl

from etfh.types import FileFormat, ParsedFile, RawFile

__all__ = [
    "METRICS_PARSERS",
    "BmoMetricsParser",
    "date_from_text",
    "metrics_from_text",
    "stale_days",
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

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_DATE = re.compile(r"(?:DATA AS OF|As of)\s+([A-Z]{3})[A-Za-z]*\s+(\d{1,2}),\s*(\d{4})")
_NET_ASSETS = re.compile(r"Net Assets \(MM\)\s*\$?\s*([\d,]+(?:\.\d+)?)")
_SHARES = re.compile(r"Shares Outstanding\s*([\d,]+)")
_CURRENCY = re.compile(r"Base Currency\s+([A-Z]{3})")
_SOURCE_ID = "bmo_metrics"
_TORONTO = ZoneInfo("America/Toronto")


def stale_days(as_of_date: date, fetched_at: datetime) -> int:
    """Whole days between ``as_of_date`` and the fetch date in America/Toronto.

    The fetch timestamp is stored in UTC; the comparison uses the calendar date
    the collector ran on locally, so a fetch after midnight UTC is not counted a
    day late (D30 uses the same timezone for as-of cutoffs).
    """
    return (fetched_at.astimezone(_TORONTO).date() - as_of_date).days


def _number(text: str) -> float | None:
    """Parse a number with thousands separators; null when it is not numeric."""
    cleaned = text.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def date_from_text(text: str) -> date | None:
    """The factsheet's stated as-of date, if the document prints one."""
    match = _DATE.search(text)
    if match is None:
        return None
    month, day, year = match.groups()
    month_number = _MONTHS.get(month.upper())
    if month_number is None:
        return None
    try:
        return date(int(year), month_number, int(day))
    except ValueError:
        return None


def metrics_from_text(
    text: str,
) -> tuple[date, float | None, float | None, float | None, str | None]:
    """Extract ``(as_of_date, nav, shares, aum, currency)`` from factsheet text.

    ``nav`` is derived from net assets and shares because the factsheet prints
    no NAV per unit. A missing net-assets or shares line leaves that figure null
    (never zero); raising is reserved for a document with no date or with
    neither figure, which is not a factsheet this parser can trust.
    """
    as_of = date_from_text(text)
    if as_of is None:
        raise ValueError("BMO factsheet has no 'DATA AS OF' date")
    assets_match = _NET_ASSETS.search(text)
    shares_match = _SHARES.search(text)
    if assets_match is None and shares_match is None:
        raise ValueError("BMO factsheet has neither 'Net Assets (MM)' nor 'Shares Outstanding'")
    aum = None
    if assets_match is not None:
        millions = _number(assets_match.group(1))
        if millions is not None:
            aum = millions * 1_000_000.0
    shares = _number(shares_match.group(1)) if shares_match is not None else None
    nav = aum / shares if aum is not None and shares is not None and shares > 0 else None
    currency_match = _CURRENCY.search(text)
    currency = currency_match.group(1) if currency_match is not None else "CAD"
    return as_of, nav, shares, aum, currency


def _text(path: Path) -> str:
    with pdfplumber.open(path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


class BmoMetricsParser:
    """Parse a BMO factsheet PDF into one fund-metrics row."""

    source_id = _SOURCE_ID
    formats = frozenset({FileFormat.pdf})

    def can_parse(self, raw: RawFile) -> bool:
        return raw.source_id == self.source_id and raw.ext.lower() == "pdf"

    def parse(self, raw: RawFile) -> ParsedFile:
        text = _text(Path(raw.path))
        as_of, nav, shares, aum, currency = metrics_from_text(text)
        notes = [
            "aum_units=millions->currency_units",
            "shares_source=factsheet",
            f"stale_days={stale_days(as_of, raw.fetched_at)}",
        ]
        if nav is not None:
            notes.append("nav_derived=aum/shares")
        frame = pl.DataFrame(
            {
                "fund_ticker": pl.Series([raw.fund_ticker], dtype=pl.String),
                "as_of_date": pl.Series([as_of], dtype=pl.Date),
                "nav_per_unit": pl.Series([nav], dtype=pl.Float64),
                "shares_outstanding": pl.Series([shares], dtype=pl.Float64),
                "aum": pl.Series([aum], dtype=pl.Float64),
                "currency": pl.Series([currency], dtype=pl.String),
                "source_id": pl.Series([self.source_id], dtype=pl.String),
                "raw_file_sha256": pl.Series([raw.sha256], dtype=pl.String),
            }
        ).select(_COLUMNS)
        return ParsedFile(
            raw=raw,
            as_of_date=as_of,
            published_at=None,
            frame=frame,
            notes=notes,
        )


METRICS_PARSERS = [BmoMetricsParser()]
