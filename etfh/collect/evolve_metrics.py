"""Collector for Evolve ETF product pages (fund metrics).

The product page states the NAV per unit and the fund's total net assets in
static HTML, so no JavaScript execution is needed. The page is fetched
conditionally; an unchanged page produces a 304 and no new raw file. The NAV
card's "As at" date is stored as the raw file's as-of hint so the raw store is
dated by the figure, not by the fetch.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from etfh.collect.registry import CollectError
from etfh.http import HTTPRetryError, PoliteClient
from etfh.registry import Fund, Source, load_sources
from etfh.storage import RawStore
from etfh.types import RawFile

__all__ = ["COLLECTORS", "EvolveMetricsCollector", "metrics_page_url", "nav_date_from_html"]

_SOURCE_ID = "evolve_metrics"
_PAGE = "https://evolveetfs.com/product/{ticker}"
_CONFIG_PATH = Path("config/sources.yaml")
_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y")
_DATE = re.compile(r"As at\s+([A-Za-z]+ \d{1,2},\s*\d{4})", re.IGNORECASE)
_NAV = re.compile(r"data-key=\"nav\"", re.IGNORECASE)
_AUM = re.compile(r"data-key=\"aum\"", re.IGNORECASE)


def metrics_page_url(fund: Fund) -> str:
    """The product page URL for ``fund``."""
    return _PAGE.format(ticker=fund.ticker.lower())


def nav_date_from_html(html: str) -> date | None:
    """The first "As at <date>" on an Evolve product page, if present."""
    for match in _DATE.finditer(html):
        candidate = re.sub(r"\s+", " ", match.group(1)).strip()
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()  # noqa: DTZ007
            except ValueError:
                continue
    return None


class EvolveMetricsCollector:
    """Fetch the product page that carries NAV per unit and total net assets."""

    source_id = _SOURCE_ID
    source: Source | None = None

    def fetch(
        self,
        fund: Fund,
        day: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        """Fetch the current product page for ``fund`` into ``store``."""
        source = self._resolve_source(fund)
        url = metrics_page_url(fund)
        etag, last_modified = _conditional_headers(store, self.source_id, fund.ticker, url)
        try:
            response = client.get(url, source=source, etag=etag, last_modified=last_modified)
        except (HTTPRetryError, httpx.HTTPError) as exc:
            raise CollectError(self.source_id, fund, str(exc)) from exc
        if response.status_code == 304:
            return []
        if response.status_code != 200:
            raise CollectError(
                self.source_id,
                fund,
                f"unexpected HTTP status {response.status_code} for {url}",
            )
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type.lower():
            raise CollectError(
                self.source_id,
                fund,
                f"expected HTML from {url}, got content-type {content_type!r}",
            )
        body = response.text
        if _NAV.search(body) is None or _AUM.search(body) is None:
            raise CollectError(
                self.source_id,
                fund,
                f"no NAV/AUM cards found on {url}; the page may be JavaScript-only",
            )
        raw = store.write(
            response.content,
            source_id=source.id,
            issuer=source.issuer,
            fund_ticker=fund.ticker,
            url=str(response.url),
            fetched_at=datetime.now(UTC),
            ext="html",
            content_type=content_type,
            as_of_hint=nav_date_from_html(body),
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


COLLECTORS = [EvolveMetricsCollector()]
