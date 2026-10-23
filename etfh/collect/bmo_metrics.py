"""Collector for BMO ETF factsheet PDFs (fund metrics).

The factsheet URL is stable and built from the fund name and ticker:
``https://df.bmogam.com/assets/static/etf-profiles-pdfs/<Fund Name>_Factsheet_EN_<TICKER>.pdf``.
The profile endpoint named in the issue serves a JavaScript shell without fund
values (verified 2026-10-23), so the static factsheet is used instead. The page
is fetched conditionally; an unchanged factsheet produces a 304 and no new raw
file. The factsheet's stated date is extracted from its text and stored as the
raw file's as-of hint.
"""

from __future__ import annotations

import io
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
import pdfplumber

from etfh.collect.registry import CollectError
from etfh.http import HTTPRetryError, PoliteClient
from etfh.parse.bmo_metrics import date_from_text
from etfh.registry import Fund, Source, load_sources
from etfh.storage import RawStore
from etfh.types import RawFile

__all__ = ["COLLECTORS", "BmoMetricsCollector", "factsheet_url"]

_SOURCE_ID = "bmo_metrics"
_BASE = "https://df.bmogam.com/assets/static/etf-profiles-pdfs/"
_CONFIG_PATH = Path("config/sources.yaml")


def factsheet_url(fund: Fund) -> str:
    """The static factsheet URL for ``fund`` with its name and ticker encoded."""
    name = quote(fund.name, safe="")
    ticker = quote(fund.ticker.upper(), safe="")
    return f"{_BASE}{name}_Factsheet_EN_{ticker}.pdf"


class BmoMetricsCollector:
    """Fetch the static factsheet that carries net assets and shares outstanding."""

    source_id = _SOURCE_ID
    source: Source | None = None

    def fetch(
        self,
        fund: Fund,
        day: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        """Fetch the current factsheet for ``fund`` into ``store``."""
        source = self._resolve_source(fund)
        url = factsheet_url(fund)
        etag, last_modified = _conditional_headers(store, self.source_id, fund.ticker, url)
        try:
            response = client.get(url, source=source, etag=etag, last_modified=last_modified)
        except (HTTPRetryError, httpx.HTTPError) as exc:
            raise CollectError(self.source_id, fund, str(exc)) from exc
        if response.status_code == 304:
            return []
        if response.status_code == 404:
            raise CollectError(
                self.source_id,
                fund,
                f"no factsheet at {url}; check the fund name in funds.csv",
            )
        if response.status_code != 200:
            raise CollectError(
                self.source_id,
                fund,
                f"unexpected HTTP status {response.status_code} for {url}",
            )
        content_type = response.headers.get("content-type", "")
        if "pdf" not in content_type.lower() or not response.content.startswith(b"%PDF"):
            raise CollectError(
                self.source_id,
                fund,
                f"expected a PDF from {url}, got content-type {content_type!r}",
            )
        as_of_hint = _pdf_date(response.content)
        raw = store.write(
            response.content,
            source_id=source.id,
            issuer=source.issuer,
            fund_ticker=fund.ticker,
            url=str(response.url),
            fetched_at=datetime.now(UTC),
            ext="pdf",
            content_type=content_type or "application/pdf",
            as_of_hint=as_of_hint,
            http_status=response.status_code,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )
        return [raw]

    def _resolve_source(self, fund: Fund) -> Source:
        if self.source is not None:
            return self.source
        sources = load_sources(_CONFIG_PATH)
        if self.source_id not in sources:
            raise CollectError(self.source_id, fund, f"source {self.source_id!r} not in config")
        return sources[self.source_id]


def _pdf_date(content: bytes) -> date | None:
    """The factsheet's stated date, or ``None`` when the PDF cannot be read."""
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages[:1])
    except Exception:
        return None
    return date_from_text(text)


def _conditional_headers(
    store: RawStore, source_id: str, fund_ticker: str, url: str
) -> tuple[str | None, str | None]:
    """ETag/Last-Modified only when the newest stored version has this URL."""
    raws = [
        raw
        for raw in store.iter_raw()
        if raw.source_id == source_id and raw.fund_ticker == fund_ticker
    ]
    if not raws:
        return None, None
    latest = max(raws, key=lambda raw: raw.fetched_at)
    if latest.url != url:
        return None, None
    meta = store.read_meta(latest.path.parent / f"{latest.path.stem}.meta.json")
    etag = meta.get("etag")
    last_modified = meta.get("last_modified")
    return (
        str(etag) if etag is not None else None,
        str(last_modified) if last_modified is not None else None,
    )


COLLECTORS = [BmoMetricsCollector()]
