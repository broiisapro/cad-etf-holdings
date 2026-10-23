"""Global X Canada quarterly disclosure collector.

The product page lists a "YYYY Qn Summary" link (the Summary of Investment
Portfolio). Link names are not stable, so the collector always reads the page
and picks a link by the quarter printed in its text instead of guessing a file
name. The product pages currently list Q1 and Q3 summaries only; a quarter that
is not linked is reported missing rather than requested at a guessed URL.

The product pages and ``/wp-content/uploads/`` are allowed by robots.txt; the
recorded source condition sets a 30-second minimum interval.
"""

from __future__ import annotations

import html as html_module
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urljoin

from etfh.collect.registry import CollectError
from etfh.http import HTTPRetryError, PoliteClient, RobotsDisallowedError
from etfh.registry import Fund, Source, load_sources, require_collectable
from etfh.storage import RawStore
from etfh.types import RawFile

__all__ = ["COLLECTORS", "GlobalXQpdCollector", "quarterly_links"]

_SOURCE_ID = "globalx_ca_qpd"
_PRODUCT_URL = "https://www.globalx.ca/product/{ticker}"
_ANCHOR = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_YEAR_QUARTER = re.compile(r"\b(20\d{2})\s*[-_ ]?\s*Q([1-4])\b", re.IGNORECASE)
_QUARTER_YEAR = re.compile(r"\bQ([1-4])\s*[-_ ]?\s*(20\d{2})\b", re.IGNORECASE)


def _configured_source(source_id: str) -> Source:
    config_dir = Path(os.environ.get("ETFH_CONFIG_DIR", "config"))
    return load_sources(config_dir / "sources.yaml")[source_id]


def _conditional_for_url(
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


def _anchor_text(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html_module.unescape(text)).strip()


def _quarter_score(text: str, href: str) -> tuple[int, int] | None:
    for candidate in (text, href):
        match = _YEAR_QUARTER.search(candidate)
        if match:
            return int(match.group(1)), int(match.group(2))
        match = _QUARTER_YEAR.search(candidate)
        if match:
            return int(match.group(2)), int(match.group(1))
    return None


def _is_english(url: str, text: str) -> bool:
    lowered_url = url.lower()
    if re.search(r"(?:^|[-_/])fr(?:[-_/.]|$)", lowered_url):
        return False
    return not re.search(r"r[ée]sum[ée]|fran[çc]ais|french", text, re.IGNORECASE)


def quarterly_links(page: str) -> list[tuple[tuple[int, int], str]]:
    """Return English quarterly disclosure links scored by (year, quarter)."""
    scored: list[tuple[tuple[int, int], str]] = []
    for match in _ANCHOR.finditer(page):
        href = html_module.unescape(match.group(1)).strip()
        text = _anchor_text(match.group(2))
        if not re.search(r"\.pdf(?:$|\?)", href, re.IGNORECASE):
            continue
        if "proxy" in text.lower() or "proxy" in href.lower():
            continue
        score = _quarter_score(text, href)
        if score is None:
            continue
        if "summary" not in text.lower() and "summary" not in href.lower():
            continue
        if not _is_english(href, text):
            continue
        scored.append((score, href))
    return scored


def _download_pdf(
    *,
    source_id: str,
    fund: Fund,
    client: PoliteClient,
    store: RawStore,
    source: Source,
    url: str,
    as_of_hint: date | None = None,
    conditional: bool = True,
) -> RawFile | None:
    etag: str | None = None
    last_modified: str | None = None
    if conditional:
        etag, last_modified = _conditional_for_url(store, source_id, fund.ticker, url)
    try:
        response = client.get(url, source=source, etag=etag, last_modified=last_modified)
    except RobotsDisallowedError as exc:
        raise CollectError(source_id, fund, f"robots.txt disallows {url}") from exc
    except HTTPRetryError as exc:
        raise CollectError(source_id, fund, f"HTTP {exc.status_code} for {url}") from exc
    if response.status_code == 304:
        return None
    if response.status_code != 200:
        raise CollectError(source_id, fund, f"HTTP {response.status_code} for {url}")
    if not response.content.startswith(b"%PDF-"):
        raise CollectError(
            source_id,
            fund,
            f"response is not a PDF ({response.headers.get('content-type', 'unknown')})",
        )
    return store.write(
        response.content,
        source_id=source_id,
        issuer=source.issuer,
        fund_ticker=fund.ticker,
        url=str(response.url),
        fetched_at=datetime.now(UTC),
        ext="pdf",
        content_type=response.headers.get("content-type", "application/pdf"),
        as_of_hint=as_of_hint,
        http_status=response.status_code,
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
    )


class GlobalXQpdCollector:
    """Read a product page and download a quarterly summary PDF."""

    source_id: str = _SOURCE_ID
    source: Source | None = None

    def __init__(self, source: Source | None = None) -> None:
        self.source = source

    def _resolve_source(self, fund: Fund) -> Source:
        if self.source is not None:
            return self.source
        source = _configured_source(self.source_id)
        self.source = source
        return source

    def product_url(self, ticker: str) -> str:
        return _PRODUCT_URL.format(ticker=ticker.strip().upper())

    def disclosure_url(self, page: str, ticker: str) -> str:
        scored = quarterly_links(page)
        if not scored:
            raise CollectError(
                self.source_id,
                ticker,
                "no quarterly Summary of Investment Portfolio link on the product page",
            )
        _, best_href = max(scored, key=lambda item: item[0])
        return urljoin(self.product_url(ticker), best_href)

    def _product_page(self, fund: Fund, client: PoliteClient, source: Source) -> str:
        product_url = self.product_url(fund.ticker)
        try:
            response = client.get(product_url, source=source)
        except RobotsDisallowedError as exc:
            raise CollectError(self.source_id, fund, f"robots.txt disallows {product_url}") from exc
        except HTTPRetryError as exc:
            raise CollectError(
                self.source_id, fund, f"HTTP {exc.status_code} for {product_url}"
            ) from exc
        if response.status_code != 200:
            raise CollectError(
                self.source_id,
                fund,
                f"HTTP {response.status_code} for {product_url}",
            )
        return response.text

    def _historical_page(self, fund: Fund, client: PoliteClient, source: Source) -> str:
        """Fetch a product page once per fund during a backfill run.

        A backfill asks for several quarters; re-reading the same page for each
        one would multiply the politeness delay by the quarter count. Failures
        are cached too, so a broken page costs one request, not one per quarter.
        """
        cache = getattr(self, "_historical_pages", None)
        if cache is None:
            cache = {}
            self._historical_pages: dict[str, str | CollectError] = cache
        cached = cache.get(fund.ticker)
        if isinstance(cached, CollectError):
            raise cached
        if isinstance(cached, str):
            return cached
        try:
            page = self._product_page(fund, client, source)
        except CollectError as exc:
            cache[fund.ticker] = exc
            raise
        cache[fund.ticker] = page
        return page

    def fetch(
        self,
        fund: Fund,
        day: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        source = self._resolve_source(fund)
        require_collectable(source)
        page = self._product_page(fund, client, source)
        pdf_url = self.disclosure_url(page, fund.ticker)
        raw = _download_pdf(
            source_id=self.source_id,
            fund=fund,
            client=client,
            store=store,
            source=source,
            url=pdf_url,
        )
        return [] if raw is None else [raw]

    def fetch_historical(
        self,
        fund: Fund,
        quarter_end: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        """Fetch the summary that the product page links for ``quarter_end``.

        Only links printed on the page are used: the upload directory in a
        Global X URL is not derivable from the quarter, so a URL for an older
        quarter cannot be constructed. When the page does not list the quarter,
        the caller reports it missing with the listed quarters as the reason.
        """
        source = self._resolve_source(fund)
        require_collectable(source)
        page = self._historical_page(fund, client, source)
        quarter = (quarter_end.month - 1) // 3 + 1
        target = (quarter_end.year, quarter)
        links = quarterly_links(page)
        matches = sorted(href for score, href in links if score == target)
        if not matches:
            listed = ", ".join(
                f"{year} Q{number}" for year, number in sorted({score for score, _ in links})
            )
            reason = (
                f"not published: the product page lists no {target[0]} Q{target[1]} summary"
                f" (listed: {listed or 'none'})"
            )
            raise CollectError(self.source_id, fund, reason)
        url = urljoin(self.product_url(fund.ticker), matches[0])
        raw = _download_pdf(
            source_id=self.source_id,
            fund=fund,
            client=client,
            store=store,
            source=source,
            url=url,
            as_of_hint=quarter_end,
            conditional=False,
        )
        return [] if raw is None else [raw]


COLLECTORS: list[GlobalXQpdCollector] = [GlobalXQpdCollector()]
