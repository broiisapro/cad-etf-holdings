"""Core enums, records and protocols shared across the package."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import polars as pl

if TYPE_CHECKING:
    from etfh.http import PoliteClient
    from etfh.registry import Fund


class Decision(StrEnum):
    """Terms-of-use decision recorded for a source."""

    allowed = "allowed"
    allowed_with_conditions = "allowed_with_conditions"
    not_allowed = "not_allowed"
    pending_review = "pending_review"


class SourceKind(StrEnum):
    """What a source publishes."""

    daily_holdings = "daily_holdings"
    quarterly_disclosure = "quarterly_disclosure"
    synthetic = "synthetic"


class FileFormat(StrEnum):
    """File format of the files a source publishes."""

    csv = "csv"
    xlsx = "xlsx"
    json = "json"
    pdf = "pdf"


class AssetClass(StrEnum):
    """Asset class of a fund."""

    equity = "equity"
    fixed_income = "fixed_income"
    balanced = "balanced"
    commodity = "commodity"
    other = "other"


@dataclass(frozen=True, slots=True)
class RawFile:
    """A file stored unmodified in the raw store.

    ``fetched_at`` is UTC-aware. ``path`` points at the stored bytes inside the
    raw store; ``sha256`` is the hash of exactly those bytes.
    """

    source_id: str
    issuer: str
    fund_ticker: str
    as_of_hint: date | None
    fetched_at: datetime
    url: str
    path: Path
    sha256: str
    content_type: str
    ext: str


@dataclass(slots=True)
class ParsedFile:
    """A parsed holdings file.

    Weights in ``frame`` are fractions; the parser declares its source unit
    (percent or fraction) and ``etfh.units.normalize_weights`` converts to
    fractions. ``known_at`` rules are applied later, at build time.
    """

    raw: RawFile
    as_of_date: date
    published_at: datetime | None
    frame: pl.DataFrame
    notes: list[str] = field(default_factory=list)


class RawStore(Protocol):
    """Structural interface a raw store must satisfy for collectors.

    The concrete implementation lives in ``etfh.storage``.
    """

    def write(
        self,
        content: bytes,
        *,
        source_id: str,
        issuer: str,
        fund_ticker: str,
        url: str,
        fetched_at: datetime,
        ext: str,
        content_type: str = "application/octet-stream",
        as_of_hint: date | None = None,
        http_status: int | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> RawFile:
        """Store bytes unmodified and return the resulting :class:`RawFile`."""
        ...


class Collector(Protocol):
    """Fetches files for one source.

    A collector never bypasses :class:`~etfh.http.PoliteClient`; every request
    goes through the terms gate, the per-host limiter and robots.txt.
    """

    source_id: str

    def fetch(
        self,
        fund: Fund,
        day: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        """Fetch the file(s) for ``fund`` covering ``day`` into ``store``."""
        ...


class Parser(Protocol):
    """Parses raw files of one or more formats into :class:`ParsedFile`."""

    formats: frozenset[FileFormat]

    def can_parse(self, raw: RawFile) -> bool:
        """Return whether this parser handles ``raw``'s format and issuer."""
        ...

    def parse(self, raw: RawFile) -> ParsedFile:
        """Parse ``raw``. Weights in the returned frame are fractions."""
        ...
