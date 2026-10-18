"""Purpose and Evolve quarterly disclosure collectors.

Purpose publishes a per-fund JSON document index (the ``documents`` field
carries the quarterly portfolio disclosure URL) and hosts the PDFs on
``documents.purposeinvest.com``. Evolve publishes the PDF link on each product
page and hosts the PDFs on its DigitalOcean Spaces CDN. Both PDF hosts answer
a missing ``/robots.txt`` with 403, which RFC 9309 (D46) treats as "no
restrictions"; every request still goes through :class:`PoliteClient`.
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
from etfh.storage import RawStore
from etfh.types import RawFile

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
        http_status=response.status_code,
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
    )


class PurposeQpdCollector:
    """Read a fund's JSON index and download its disclosure PDF."""

    source_id: str = _PURPOSE_SOURCE_ID
    source: Source | None = None

    def __init__(self, source: Source | None = None) -> None:
        self.source = source

    def _resolve_source(self, fund: Fund) -> Source:
        if self.source is not None:
            return self.source
        source = _configured_source(self.source_id)
        self.source = source
        return source

    def api_url(self, slug: str) -> str:
        return _PURPOSE_API.format(slug=slug)

    def fetch(
        self,
        fund: Fund,
        day: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        source = self._resolve_source(fund)
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
        return _EVOLVE_PRODUCT.format(ticker=ticker.strip().upper())

    def fetch(
        self,
        fund: Fund,
        day: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        source = self._resolve_source(fund)
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
