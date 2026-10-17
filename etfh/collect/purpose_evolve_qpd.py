"""Purpose and Evolve quarterly disclosure collectors.

Purpose publishes a per-fund JSON document index (the ``documents`` field
carries the quarterly portfolio disclosure URL) and hosts the PDFs on
``documents.purposeinvest.com``. Evolve publishes the PDF link on each product
page. Both PDF hosts return a non-404 4xx for ``/robots.txt``, which D37
treats as disallow-all, so :class:`PoliteClient` refuses the download and the
collectors report a :class:`~etfh.collect.registry.CollectError`.
"""

from __future__ import annotations

import html as html_module
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path

from etfh.collect.registry import CollectError
from etfh.http import HTTPRetryError, PoliteClient, RobotsDisallowedError
from etfh.registry import Fund, Source, load_sources, require_collectable
from etfh.types import RawFile, RawStore

__all__ = [
    "COLLECTORS",
    "EvolveQpdCollector",
    "PurposeQpdCollector",
    "evolve_disclosure_url",
    "purpose_disclosure_url",
]

_PURPOSE_SOURCE_ID = "purpose_qpd"
_EVOLVE_SOURCE_ID = "evolve_qpd"
_PURPOSE_API = "https://www.purposeinvest.com/api/funds/{slug}"
_EVOLVE_PRODUCT = "https://evolveetfs.com/product/{ticker}/"
_ANCHOR = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def _configured_source(source_id: str) -> Source:
    config_dir = Path(os.environ.get("ETFH_CONFIG_DIR", "config"))
    return load_sources(config_dir / "sources.yaml")[source_id]


def _anchor_text(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html_module.unescape(text)).strip()


def purpose_disclosure_url(payload: object) -> str | None:
    """Return the quarterly portfolio disclosure URL from a fund JSON payload."""
    if not isinstance(payload, dict):
        return None
    documents = payload.get("documents")
    if not isinstance(documents, dict):
        return None
    for group in documents.values():
        entries = group if isinstance(group, list) else [group]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            document = entry.get("quarterly_portfolio_disclosure")
            if isinstance(document, dict):
                url = document.get("url")
                if isinstance(url, str) and url:
                    return url
    return None


def _is_french(href: str, text: str) -> bool:
    lowered = href.lower()
    if re.search(r"(?:^|[-_/])fr(?:[-_/.]|$)", lowered):
        return True
    return bool(re.search(r"trimestrielle|divulgation|fran[çc]ais|r[ée]sum[ée]", text, re.I))


def _qpd_score(href: str, text: str) -> tuple[int, int] | None:
    for candidate in (href, text):
        match = re.search(r"qpd\s*([a-z]+)\s*(20\d{2})", candidate, re.IGNORECASE)
        if match:
            month = _MONTHS.get(match.group(1).lower(), 0)
            return int(match.group(2)), month
        match = re.search(r"(20\d{2})\s*[-_ ]?\s*qpd\s*([a-z]+)", candidate, re.IGNORECASE)
        if match:
            month = _MONTHS.get(match.group(2).lower(), 0)
            return int(match.group(1)), month
    return None


def evolve_disclosure_url(page: str) -> str | None:
    """Return the newest English quarterly portfolio disclosure link."""
    scored: list[tuple[tuple[int, int], str]] = []
    fallback: list[str] = []
    for match in _ANCHOR.finditer(page):
        href = html_module.unescape(match.group(1)).strip()
        text = _anchor_text(match.group(2))
        if not re.search(r"\.pdf(?:$|\?)", href, re.IGNORECASE):
            continue
        is_disclosure = "quarterly portfolio disclosure" in text.lower() or "qpd" in href.lower()
        if not is_disclosure or _is_french(href, text):
            continue
        score = _qpd_score(href, text)
        if score is None:
            fallback.append(href)
        else:
            scored.append((score, href))
    if scored:
        return max(scored, key=lambda item: item[0])[1]
    return fallback[0] if fallback else None


def _download_pdf(
    *,
    source_id: str,
    fund: Fund,
    client: PoliteClient,
    store: RawStore,
    source: Source,
    url: str,
) -> RawFile | None:
    try:
        response = client.get(url, source=source)
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
        http_status=response.status_code,
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
    )


class PurposeQpdCollector:
    """Read a fund's JSON index and download its disclosure PDF."""

    source_id: str = _PURPOSE_SOURCE_ID

    def __init__(self, source: Source | None = None) -> None:
        self._source = source

    def source(self) -> Source:
        if self._source is None:
            self._source = _configured_source(self.source_id)
        return self._source

    def api_url(self, slug: str) -> str:
        return _PURPOSE_API.format(slug=slug)

    def fetch(
        self,
        fund: Fund,
        day: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        source = self.source()
        require_collectable(source)
        slug = fund.params.get("purpose_slug", "").strip()
        if not slug:
            raise CollectError(self.source_id, fund, "fund has no purpose_slug parameter")
        api_url = self.api_url(slug)
        try:
            response = client.get(api_url, source=source)
        except RobotsDisallowedError as exc:
            raise CollectError(self.source_id, fund, f"robots.txt disallows {api_url}") from exc
        except HTTPRetryError as exc:
            raise CollectError(
                self.source_id, fund, f"HTTP {exc.status_code} for {api_url}"
            ) from exc
        if response.status_code != 200:
            raise CollectError(self.source_id, fund, f"HTTP {response.status_code} for {api_url}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CollectError(self.source_id, fund, f"invalid JSON from {api_url}") from exc
        pdf_url = purpose_disclosure_url(payload)
        if pdf_url is None:
            raise CollectError(
                self.source_id, fund, "no quarterly portfolio disclosure in the fund JSON"
            )
        raw = _download_pdf(
            source_id=self.source_id,
            fund=fund,
            client=client,
            store=store,
            source=source,
            url=pdf_url,
        )
        return [] if raw is None else [raw]


class EvolveQpdCollector:
    """Read a product page and download its disclosure PDF."""

    source_id: str = _EVOLVE_SOURCE_ID

    def __init__(self, source: Source | None = None) -> None:
        self._source = source

    def source(self) -> Source:
        if self._source is None:
            self._source = _configured_source(self.source_id)
        return self._source

    def product_url(self, ticker: str) -> str:
        return _EVOLVE_PRODUCT.format(ticker=ticker.strip().upper())

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
            response = client.get(product_url, source=source)
        except RobotsDisallowedError as exc:
            raise CollectError(self.source_id, fund, f"robots.txt disallows {product_url}") from exc
        except HTTPRetryError as exc:
            raise CollectError(
                self.source_id, fund, f"HTTP {exc.status_code} for {product_url}"
            ) from exc
        if response.status_code != 200:
            raise CollectError(
                self.source_id, fund, f"HTTP {response.status_code} for {product_url}"
            )
        pdf_url = evolve_disclosure_url(response.text)
        if pdf_url is None:
            raise CollectError(
                self.source_id, fund, "no quarterly portfolio disclosure link on the product page"
            )
        raw = _download_pdf(
            source_id=self.source_id,
            fund=fund,
            client=client,
            store=store,
            source=source,
            url=pdf_url,
        )
        return [] if raw is None else [raw]


COLLECTORS: list[PurposeQpdCollector | EvolveQpdCollector] = [
    PurposeQpdCollector(),
    EvolveQpdCollector(),
]
