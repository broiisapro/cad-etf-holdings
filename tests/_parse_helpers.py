"""Helpers shared by parser tests.

Only test-owned helpers live here; ``tests/conftest.py`` stays untouched this
wave.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from etfh.storage import RawStore
from etfh.types import ParsedFile, RawFile

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RAW_SYNTHETIC = FIXTURES / "raw_synthetic"
MANIFEST_PATH = FIXTURES / "manifest.json"


def load_manifest() -> dict[str, Any]:
    """Return the committed synthetic-fixture manifest."""
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest.json must be a JSON object")
    return data


def raw_synthetic_files() -> list[RawFile]:
    """Every raw file in the committed synthetic fixture store."""
    return RawStore(RAW_SYNTHETIC).iter_raw()


def find_raw(
    ticker: str,
    *,
    source_id: str | None = None,
    as_of: date | None = None,
    fetched_at: datetime | None = None,
) -> RawFile:
    """Return the first fixture file matching the given keys."""
    for raw in raw_synthetic_files():
        if raw.fund_ticker != ticker:
            continue
        if source_id is not None and raw.source_id != source_id:
            continue
        if as_of is not None and raw.as_of_hint != as_of:
            continue
        if fetched_at is not None and raw.fetched_at != fetched_at:
            continue
        return raw
    raise LookupError(f"no fixture for {ticker!r} matching the given keys")


def make_raw(
    path: Path,
    *,
    source_id: str,
    fund_ticker: str,
    as_of_hint: date | None = None,
    fetched_at: datetime = datetime(2026, 10, 29, 22, 0, tzinfo=UTC),
    url: str | None = None,
) -> RawFile:
    """Build a :class:`RawFile` for a test-created file."""
    source = Path(path)
    content = source.read_bytes()
    return RawFile(
        source_id=source_id,
        issuer="synthetic",
        fund_ticker=fund_ticker,
        as_of_hint=as_of_hint,
        fetched_at=fetched_at,
        url=url or f"synthetic://fixtures/{fund_ticker}{source.suffix}",
        path=source,
        sha256=hashlib.sha256(content).hexdigest(),
        content_type="text/plain",
        ext=source.suffix.lstrip("."),
    )


def weight_sum(parsed: ParsedFile) -> float:
    """Sum of the parsed fraction weights."""
    return float(parsed.frame["weight"].sum())


def weight_for(parsed: ParsedFile, *, isin: str) -> float:
    """The single weight of the row carrying ``isin``."""
    rows = parsed.frame.filter(pl.col("isin") == isin)
    if rows.height != 1:
        raise AssertionError(f"expected exactly one row for {isin}, found {rows.height}")
    return float(rows["weight"][0])
