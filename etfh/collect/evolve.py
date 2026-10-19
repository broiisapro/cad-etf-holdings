"""Collector for Evolve ETFs daily holdings CSV.

The static download URL never changes, and the CSV itself has no date. The
fund page states the holdings date ("Holdings As at October 07, 2026"), so the
collector fetches the page first and records that date as the raw file's
as-of hint. The CSV request is conditional so an unchanged file produces a 304
and no new version. The source's recorded minimum interval is 10 seconds;
``PoliteClient`` enforces it and tests use the injectable clock.
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

__all__ = ["COLLECTORS", "EvolveCollector"]

_SOURCE_ID = "evolve_daily"
_FILE_URL = "https://evolveetfs.com/wp-content/uploads/holdings/{ticker}.csv"
_PAGE_URL = "https://evolveetfs.com/product/{slug}"
_CONFIG_PATH = Path("config/sources.yaml")
_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y")
_TAGS = re.compile(r"<[^>]+>")
_HOLDINGS_DATE = re.compile(
    r"Holdings\s+As at\s+([A-Za-z]+ \d{1,2},\s*\d{4})",
    re.IGNORECASE,
)


def holdings_date_from_page(html: str) -> date | None:
    """Extract the holdings as-of date stated on an Evolve fund page."""
    text = _TAGS.sub(" ", html)
    match = _HOLDINGS_DATE.search(text)
    if match is None:
        return None
    candidate = re.sub(r"\s+", " ", match.group(1)).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date()  # noqa: DTZ007
        except ValueError:
            continue
    return None


class EvolveCollector:
    """Fetch the holdings CSV and the fund page that dates it.

    The registry binds the source the CLI passes (``--config DIR``) by setting
    ``source`` on a fresh copy; only when it is ``None`` does the collector
    fall back to the default config. Every request is gated with that
    source, so the terms gate always checks the config the CLI selected.
    """

    source_id = _SOURCE_ID
    source: Source | None = None

    def fetch(
        self,
        fund: Fund,
        day: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        """Fetch the page's holdings date, then the CSV it links to."""
        source = self._resolve_source(fund)
        page_url = _PAGE_URL.format(slug=fund.ticker.lower())
        file_url = _FILE_URL.format(ticker=fund.ticker.upper())
        page = self._get(client, source, page_url, fund)
        as_of_hint = holdings_date_from_page(page.text)
        if as_of_hint is None:
            raise CollectError(
                self.source_id,
                fund,
                f"no 'Holdings As at' date found on {page_url}",
            )
        etag, last_modified = _conditional_headers(store, self.source_id, fund.ticker)
        file_response = self._get(
            client,
            source,
            file_url,
            fund,
            etag=etag,
            last_modified=last_modified,
        )
        if file_response.status_code == 304:
            return []
        content_type = file_response.headers.get("content-type", "")
        if "csv" not in content_type.lower():
            raise CollectError(
                self.source_id,
                fund,
                f"expected CSV from {file_url}, got content-type {content_type!r}",
            )
        raw = store.write(
            file_response.content,
            source_id=source.id,
            issuer=source.issuer,
            fund_ticker=fund.ticker,
            url=str(file_response.url),
            fetched_at=datetime.now(UTC),
            ext="csv",
            content_type=content_type or "text/csv",
            as_of_hint=as_of_hint,
            http_status=file_response.status_code,
            etag=file_response.headers.get("etag"),
            last_modified=file_response.headers.get("last-modified"),
        )
        return [raw]

    def _get(
        self,
        client: PoliteClient,
        source: Source,
        url: str,
        fund: Fund,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> httpx.Response:
        try:
            response = client.get(
                url,
                source=source,
                etag=etag,
                last_modified=last_modified,
            )
        except (HTTPRetryError, httpx.HTTPError) as exc:
            raise CollectError(self.source_id, fund, str(exc)) from exc
        if response.status_code not in (200, 304):
            raise CollectError(
                self.source_id,
                fund,
                f"unexpected HTTP status {response.status_code} for {url}",
            )
        return response

    def _resolve_source(self, fund: Fund) -> Source:
        if self.source is not None:
            return self.source
        sources = load_sources(_CONFIG_PATH)
        if self.source_id not in sources:
            raise CollectError(self.source_id, fund, f"source {self.source_id!r} not in config")
        return sources[self.source_id]


def _conditional_headers(
    store: RawStore, source_id: str, fund_ticker: str
) -> tuple[str | None, str | None]:
    """ETag and Last-Modified of the newest stored version, if any."""
    raws = [
        raw
        for raw in store.iter_raw()
        if raw.source_id == source_id and raw.fund_ticker == fund_ticker
    ]
    if not raws:
        return None, None
    latest = max(raws, key=lambda raw: raw.fetched_at)
    meta = store.read_meta(latest.path.parent / f"{latest.path.stem}.meta.json")
    etag = meta.get("etag")
    last_modified = meta.get("last_modified")
    return (
        str(etag) if etag is not None else None,
        str(last_modified) if last_modified is not None else None,
    )


COLLECTORS = [EvolveCollector()]
