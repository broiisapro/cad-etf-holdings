"""Helpers shared by security-master tests.

Frames come from the committed synthetic raw fixtures through the merged
synthetic daily parsers (``etfh.parse.synthetic_daily``), so the tests see the
same shapes the build sees. Only test-owned helpers live here;
``tests/conftest.py`` stays untouched this wave.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from etfh.parse.synthetic_daily import PARSERS
from etfh.storage import RawStore
from etfh.types import ParsedFile, RawFile

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RAW_SYNTHETIC = FIXTURES / "raw_synthetic"
EXPECTED_OVERLAP = FIXTURES / "expected_overlap.json"


def fixture_raw_files() -> list[RawFile]:
    """Every raw file in the committed synthetic fixture store."""
    return RawStore(RAW_SYNTHETIC).iter_raw()


def find_fixture(ticker: str, *, as_of: date | None = None) -> RawFile:
    """The first fixture file for ``ticker``, optionally on ``as_of``.

    ``RawStore.iter_raw`` is ordered by issuer, ticker, as-of date and
    fetched_at, so when a fund/date has several versions (a republication)
    the first is the earliest-known one.
    """
    for raw in fixture_raw_files():
        if raw.fund_ticker != ticker:
            continue
        if as_of is not None and raw.as_of_hint != as_of:
            continue
        return raw
    raise LookupError(f"no fixture for {ticker!r} on {as_of}")


def parse_fixture(ticker: str, *, as_of: date | None = None) -> ParsedFile:
    """Parse one committed fixture with the synthetic daily parsers."""
    raw = find_fixture(ticker, as_of=as_of)
    for parser in PARSERS:
        if parser.can_parse(raw):
            return parser.parse(raw)
    raise LookupError(f"no parser handles {raw.path}")


def fixture_frame(ticker: str, *, as_of: date | None = None) -> pl.DataFrame:
    """Parsed holdings frame for one fixture file (weights as fractions)."""
    return parse_fixture(ticker, as_of=as_of).frame


def holdings_frame(
    rows: list[dict[str, object]],
    *,
    as_of: date = date(2026, 11, 2),
    source_id: str = "syn_csv",
    raw_file_sha256: str = "0" * 64,
) -> pl.DataFrame:
    """Build a holdings frame from partial row dictionaries.

    Columns not named in a row are null, so a test only spells out what the
    rule under test needs. ``as_of_date``, ``source_id`` and
    ``raw_file_sha256`` can be overridden per row or for the whole frame;
    they are needed by the unmatched report.
    """
    fields = (
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
    frame = pl.DataFrame(
        {
            field: pl.Series(field, [row.get(field) for row in rows], dtype=pl.String)
            for field in fields
        }
    )
    return frame.with_columns(
        pl.Series("as_of_date", [row.get("as_of_date", as_of) for row in rows], dtype=pl.Date),
        pl.Series(
            "source_id",
            [row.get("source_id", source_id) for row in rows],
            dtype=pl.String,
        ),
        pl.Series(
            "raw_file_sha256",
            [row.get("raw_file_sha256", raw_file_sha256) for row in rows],
            dtype=pl.String,
        ),
    )
