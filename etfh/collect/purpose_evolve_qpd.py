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
from collections.abc import Sequence
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
    "evolve_disclosure_links",
    "evolve_disclosure_url",
    "purpose_disclosure_entries",
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


def _iso_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _date_in_url(url: object) -> date | None:
    if not isinstance(url, str):
        return None
    match = re.search(r"(20\d{2}-\d{2}-\d{2})", url)
    return _iso_date(match.group(1)) if match else None


def purpose_disclosure_entries(payload: object) -> list[tuple[date, str]]:
    """Return every dated quarterly disclosure in a fund JSON payload.

    Purpose keeps a fund's documents in a ``documents`` object whose groups can
    be dicts (the current disclosure per document type) or lists (a history).
    The walk accepts both shapes and dates an entry by its ``dt`` field, falling
    back to the ISO date the issuer puts in the document URL.
    """
    entries: set[tuple[date, str]] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            document = node.get("quarterly_portfolio_disclosure")
            if isinstance(document, dict):
                url = document.get("url")
                stated = _iso_date(document.get("dt")) or _date_in_url(url)
                if isinstance(url, str) and url and stated is not None:
                    entries.add((stated, url))
            for key, value in node.items():
                if key != "quarterly_portfolio_disclosure":
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return sorted(entries)


def _entry_for_quarter(
    entries: Sequence[tuple[date, str]], quarter_end: date, tolerance_days: int = 7
) -> tuple[date, str] | None:
    matches = [entry for entry in entries if abs((entry[0] - quarter_end).days) <= tolerance_days]
    if not matches:
        return None
    return min(matches, key=lambda entry: (abs((entry[0] - quarter_end).days), entry[0], entry[1]))


def _is_french(href: str, text: str) -> bool:
    lowered = href.lower()
    if re.search(r"(?:^|[-_/])fr(?:[-_/.]|$)", lowered):
        return True
    return bool(re.search(r"trimestrielle|divulgation|fran[çc]ais|r[ée]sum[ée]", text, re.I))


def _qpd_score(href: str, text: str) -> tuple[int, int] | None:
    """Return a (year, quarter-end month) score for a disclosure link.

    Evolve names its files by the disclosure month (``QPDMarch2026``). The
    quarter forms are accepted too so a renamed or older file is still tied to
    the right quarter instead of being ignored.
    """
    for candidate in (href, text):
        match = re.search(r"qpd\s*([a-z]+)\s*(20\d{2})", candidate, re.IGNORECASE)
        if match:
            month = _MONTHS.get(match.group(1).lower(), 0)
            return int(match.group(2)), month
        match = re.search(r"(20\d{2})\s*[-_ ]?\s*qpd\s*([a-z]+)", candidate, re.IGNORECASE)
        if match:
            month = _MONTHS.get(match.group(2).lower(), 0)
            return int(match.group(1)), month
        match = re.search(r"qpd\s*(20\d{2})\s*[-_ ]?\s*q([1-4])", candidate, re.IGNORECASE)
        if match:
            return int(match.group(1)), int(match.group(2)) * 3
        match = re.search(r"qpd\s*q([1-4])\s*[-_ ]?\s*(20\d{2})", candidate, re.IGNORECASE)
        if match:
            return int(match.group(2)), int(match.group(1)) * 3
        match = re.search(r"(20\d{2})\s*[-_ ]?\s*q([1-4])\s*[-_ ]?\s*qpd", candidate, re.IGNORECASE)
        if match:
            return int(match.group(1)), int(match.group(2)) * 3
    return None


def _evolve_links(page: str) -> tuple[list[tuple[tuple[int, int], str]], list[str]]:
    """Split English disclosure links into quarter-scored and unscored."""
    scored: list[tuple[tuple[int, int], str]] = []
    unscored: list[str] = []
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
            unscored.append(href)
        else:
            scored.append((score, href))
    return scored, unscored


def evolve_disclosure_links(page: str) -> list[tuple[tuple[int, int], str]]:
    """Return English quarterly portfolio disclosure links with their quarter."""
    return _evolve_links(page)[0]


def evolve_disclosure_url(page: str) -> str | None:
    """Return the newest English quarterly portfolio disclosure link."""
    scored, unscored = _evolve_links(page)
    if scored:
        return max(scored, key=lambda item: item[0])[1]
    return unscored[0] if unscored else None


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

    def _fund_payload(self, fund: Fund, client: PoliteClient, source: Source) -> object:
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
            return response.json()
        except ValueError as exc:
            raise CollectError(self.source_id, fund, f"invalid JSON from {api_url}") from exc

    def _historical_payload(self, fund: Fund, client: PoliteClient, source: Source) -> object:
        """Fetch a fund's document index once per fund during a backfill run."""
        cache = getattr(self, "_historical_payloads", None)
        if cache is None:
            cache = {}
            self._historical_payloads: dict[str, object | CollectError] = cache
        cached = cache.get(fund.ticker)
        if isinstance(cached, CollectError):
            raise cached
        if cached is not None:
            return cached
        try:
            payload = self._fund_payload(fund, client, source)
        except CollectError as exc:
            cache[fund.ticker] = exc
            raise
        cache[fund.ticker] = payload
        return payload

    def fetch(
        self,
        fund: Fund,
        day: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        source = self._resolve_source(fund)
        require_collectable(source)
        payload = self._fund_payload(fund, client, source)
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

    def fetch_historical(
        self,
        fund: Fund,
        quarter_end: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        """Fetch the disclosure dated at ``quarter_end`` from the fund's JSON.

        Purpose's fund JSON is the document index: each disclosure carries the
        portfolio date (``dt``) and the URL on ``documents.purposeinvest.com``.
        Only an entry the issuer publishes is downloaded; a quarter that is not
        in the index is reported missing with the dates the index does carry.
        """
        source = self._resolve_source(fund)
        require_collectable(source)
        payload = self._historical_payload(fund, client, source)
        entries = purpose_disclosure_entries(payload)
        entry = _entry_for_quarter(entries, quarter_end)
        if entry is None:
            listed = ", ".join(stated.isoformat() for stated, _ in entries)
            reason = (
                "not published: the fund JSON lists no quarterly portfolio disclosure"
                f" for {quarter_end.isoformat()} (documents dated: {listed or 'none'})"
            )
            raise CollectError(self.source_id, fund, reason)
        _, url = entry
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
                self.source_id, fund, f"HTTP {response.status_code} for {product_url}"
            )
        return response.text

    def _historical_page(self, fund: Fund, client: PoliteClient, source: Source) -> str:
        """Fetch a product page once per fund during a backfill run."""
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
        pdf_url = evolve_disclosure_url(page)
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

    def fetch_historical(
        self,
        fund: Fund,
        quarter_end: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        """Fetch the disclosure that the product page links for ``quarter_end``.

        The Evolve page lists every disclosure it still publishes. A file whose
        name carries the disclosure month is matched to the quarter-end month;
        a quarter that is not listed is reported missing, never requested at a
        guessed URL.
        """
        source = self._resolve_source(fund)
        require_collectable(source)
        page = self._historical_page(fund, client, source)
        target = (quarter_end.year, quarter_end.month)
        links = evolve_disclosure_links(page)
        matches = sorted(href for score, href in links if score == target)
        if not matches:
            listed = ", ".join(
                f"{year}-{month:02d}" for year, month in sorted({score for score, _ in links})
            )
            reason = (
                "not published: the product page lists no quarterly portfolio disclosure"
                f" dated {target[0]}-{target[1]:02d} (listed: {listed or 'none'})"
            )
            raise CollectError(self.source_id, fund, reason)
        raw = _download_pdf(
            source_id=self.source_id,
            fund=fund,
            client=client,
            store=store,
            source=source,
            url=matches[0],
            as_of_hint=quarter_end,
            conditional=False,
        )
        return [] if raw is None else [raw]


COLLECTORS: list[PurposeQpdCollector | EvolveQpdCollector] = [
    PurposeQpdCollector(),
    EvolveQpdCollector(),
]
