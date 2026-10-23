"""Parser for Evolve ETF product pages into fund metrics.

The product page states, in its ``product-series-box`` cards, a NAV per unit
("Nav ... As at <date> $<value>") and the fund's total net assets ("Fund Total
Net Assets ... $<value> <K|M|B>"). The page does not publish units outstanding,
so the parser derives ``shares_outstanding = aum / nav`` and records
``shares_derived=aum/nav`` in its notes; a missing NAV or AUM stays null rather
than being filled from another date (D67). Currency is CAD for the approved
Canadian funds; the page does not state a separate fund currency in the card.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import polars as pl

from etfh.types import FileFormat, ParsedFile, RawFile

__all__ = ["METRICS_PARSERS", "EvolveMetricsParser", "metrics_from_html"]

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

_SOURCE_ID = "evolve_metrics"
_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y")
_AS_AT = re.compile(r"As at\s+([A-Za-z]+ \d{1,2},\s*\d{4})", re.IGNORECASE)
_NAV_VALUE = re.compile(r"<h2[^>]*data-key=\"nav\"[^>]*>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
_AUM_VALUE = re.compile(r"<h2[^>]*data-key=\"aum\"[^>]*>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")
_MONEY = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)\s*([KMB])?", re.IGNORECASE)
_SCALES = {"": 1.0, "K": 1_000.0, "M": 1_000_000.0, "B": 1_000_000_000.0}


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub(" ", fragment)).strip()


def _money(fragment: str) -> float | None:
    """Parse ``$25.69`` or ``$371.33 M`` into currency units; null if absent."""
    match = _MONEY.search(_text(fragment))
    if match is None:
        return None
    value = match.group(1).replace(",", "")
    suffix = (match.group(2) or "").upper()
    try:
        return float(value) * _SCALES[suffix]
    except (ValueError, KeyError):
        return None


def _date(fragment: str) -> date | None:
    match = _AS_AT.search(_text(fragment))
    if match is None:
        return None
    candidate = re.sub(r"\s+", " ", match.group(1)).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date()  # noqa: DTZ007
        except ValueError:
            continue
    return None


def metrics_from_html(
    html: str, *, fallback_date: date | None = None
) -> tuple[date, float | None, float | None, float | None]:
    """Extract ``(as_of_date, nav, shares, aum)`` from an Evolve product page.

    The NAV card carries the date; the AUM card does not, so the date is read
    from the text before the NAV value. Raises ``ValueError`` when there is no
    NAV card at all.
    """
    nav_match = _NAV_VALUE.search(html)
    if nav_match is None:
        raise ValueError("Evolve product page has no NAV card")
    nav = _money(nav_match.group(1))
    if nav is None:
        raise ValueError("Evolve NAV card has no numeric value")
    as_of = _date(html[max(0, nav_match.start() - 2_000) : nav_match.start()])
    if as_of is None:
        as_of = fallback_date
    if as_of is None:
        raise ValueError("Evolve product page has no NAV as-at date")
    aum_match = _AUM_VALUE.search(html)
    aum = _money(aum_match.group(1)) if aum_match is not None else None
    shares = aum / nav if aum is not None and nav > 0 else None
    return as_of, nav, shares, aum


class EvolveMetricsParser:
    """Parse an Evolve product page into one fund-metrics row."""

    source_id = _SOURCE_ID
    formats = frozenset({FileFormat.html})

    def can_parse(self, raw: RawFile) -> bool:
        return raw.source_id == self.source_id and raw.ext.lower() == "html"

    def parse(self, raw: RawFile) -> ParsedFile:
        html = Path(raw.path).read_text(encoding="utf-8")
        as_of, nav, shares, aum = metrics_from_html(html, fallback_date=raw.as_of_hint)
        notes = ["shares_source=derived_from_aum_and_nav"]
        if shares is not None:
            notes.append("shares_derived=aum/nav")
        frame = pl.DataFrame(
            {
                "fund_ticker": pl.Series([raw.fund_ticker], dtype=pl.String),
                "as_of_date": pl.Series([as_of], dtype=pl.Date),
                "nav_per_unit": pl.Series([nav], dtype=pl.Float64),
                "shares_outstanding": pl.Series([shares], dtype=pl.Float64),
                "aum": pl.Series([aum], dtype=pl.Float64),
                "currency": pl.Series(["CAD"], dtype=pl.String),
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


METRICS_PARSERS = [EvolveMetricsParser()]
