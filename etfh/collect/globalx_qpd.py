"""Global X Canada quarterly disclosure collector.

The product page lists a "YYYY Qn Summary" link (the Summary of Investment
Portfolio). Link names are not stable, so the collector always reads the page
and picks the newest English quarterly link instead of guessing a file name.
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
from etfh.types import RawFile, RawStore

__all__ = ["COLLECTORS", "GlobalXQpdCollector"]

_SOURCE_ID = "globalx_ca_qpd"
_PRODUCT_URL = "https://www.globalx.ca/product/{ticker}"
_ANCHOR = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_YEAR_QUARTER = re.compile(r"\b(20\d{2})\s*[-_ ]?\s*Q([1-4])\b", re.IGNORECASE)
_QUARTER_YEAR = re.compile(r"\bQ([1-4])\s*[-_ ]?\s*(20\d{2})\b", re.IGNORECASE)


def _configured_source(source_id: str) -> Source:
    config_dir = Path(os.environ.get("ETFH_CONFIG_DIR", "config"))
    return load_sources(config_dir / "sources.yaml")[source_id]


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


class GlobalXQpdCollector:
    """Read a product page and download its newest quarterly summary PDF."""

    source_id: str = _SOURCE_ID

    def __init__(self, source: Source | None = None) -> None:
        self._source = source

    def source(self) -> Source:
        if self._source is None:
            self._source = _configured_source(self.source_id)
        return self._source

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

    def fetch(
        self,
        fund: Fund,
        day: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        source = self.source()
        require_collectable(source)
        product_url = self.product_url(fund.ticker)
        try:
            page_response = client.get(product_url, source=source)
        except RobotsDisallowedError as exc:
            raise CollectError(self.source_id, fund, f"robots.txt disallows {product_url}") from exc
        except HTTPRetryError as exc:
            raise CollectError(
                self.source_id, fund, f"HTTP {exc.status_code} for {product_url}"
            ) from exc
        if page_response.status_code != 200:
            raise CollectError(
                self.source_id,
                fund,
                f"HTTP {page_response.status_code} for {product_url}",
            )
        pdf_url = self.disclosure_url(page_response.text, fund.ticker)
        try:
            pdf_response = client.get(pdf_url, source=source)
        except RobotsDisallowedError as exc:
            raise CollectError(self.source_id, fund, f"robots.txt disallows {pdf_url}") from exc
        except HTTPRetryError as exc:
            raise CollectError(
                self.source_id, fund, f"HTTP {exc.status_code} for {pdf_url}"
            ) from exc
        if pdf_response.status_code == 304:
            return []
        if pdf_response.status_code != 200:
            raise CollectError(
                self.source_id, fund, f"HTTP {pdf_response.status_code} for {pdf_url}"
            )
        if not pdf_response.content.startswith(b"%PDF-"):
            raise CollectError(
                self.source_id,
                fund,
                f"response is not a PDF ({pdf_response.headers.get('content-type', 'unknown')})",
            )
        raw = store.write(
            pdf_response.content,
            source_id=self.source_id,
            issuer=source.issuer,
            fund_ticker=fund.ticker,
            url=str(pdf_response.url),
            fetched_at=datetime.now(UTC),
            ext="pdf",
            content_type=pdf_response.headers.get("content-type", "application/pdf"),
            http_status=pdf_response.status_code,
            etag=pdf_response.headers.get("etag"),
            last_modified=pdf_response.headers.get("last-modified"),
        )
        return [raw]


COLLECTORS: list[GlobalXQpdCollector] = [GlobalXQpdCollector()]
