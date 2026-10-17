"""BMO Global Asset Management quarterly disclosure collector.

The legal-and-regulatory listing links to disclosure PDFs on
``bmo.bynder.com`` under opaque hashes. The collector reads the listing once
per run and matches the exact English file name (``bmo<TICKER>_qpd-Eng.pdf``);
it never constructs or guesses a hash. The listing host was unreachable in
this environment, which under D37 means disallow-all for robots.txt, so
:class:`PoliteClient` refuses the request and the collector reports it.
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

__all__ = ["BmoQpdCollector", "COLLECTORS", "listing_links"]

_SOURCE_ID = "bmo_qpd"
_LISTING_URL = "https://bmogam.com/ca-en/investing-resources/legal-and-regulatory-documents/"
_ANCHOR = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>', re.IGNORECASE)


def _configured_source(source_id: str) -> Source:
    config_dir = Path(os.environ.get("ETFH_CONFIG_DIR", "config"))
    return load_sources(config_dir / "sources.yaml")[source_id]


def listing_links(page: str) -> list[str]:
    """Return absolute PDF links found on the listing page."""
    links: list[str] = []
    for match in _ANCHOR.finditer(page):
        href = html_module.unescape(match.group(1)).strip()
        if not re.search(r"\.pdf(?:$|\?)", href, re.IGNORECASE):
            continue
        links.append(urljoin(_LISTING_URL, href))
    return links


def _ticker_pattern(ticker: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?:^|/)bmo{re.escape(ticker)}_qpd-eng\.pdf(?:$|\?)",
        re.IGNORECASE,
    )


class BmoQpdCollector:
    """Read the BMO document listing once and download a fund's English QPD."""

    source_id: str = _SOURCE_ID

    def __init__(self, source: Source | None = None) -> None:
        self._source = source
        self._listing: list[str] | None = None

    def source(self) -> Source:
        if self._source is None:
            self._source = _configured_source(self.source_id)
        return self._source

    def disclosure_url(self, ticker: str) -> str:
        if self._listing is None:
            raise CollectError(self.source_id, ticker, "the listing has not been loaded")
        pattern = _ticker_pattern(ticker)
        for link in self._listing:
            if pattern.search(link):
                return link
        raise CollectError(
            self.source_id,
            ticker,
            f"no bmo{ticker.upper()}_qpd-Eng.pdf link on the listing page",
        )

    def _load_listing(self, client: PoliteClient, source: Source, fund: Fund) -> list[str]:
        if self._listing is not None:
            return self._listing
        try:
            response = client.get(_LISTING_URL, source=source)
        except RobotsDisallowedError as exc:
            raise CollectError(
                self.source_id, fund, f"robots.txt disallows {_LISTING_URL}"
            ) from exc
        except HTTPRetryError as exc:
            raise CollectError(
                self.source_id, fund, f"HTTP {exc.status_code} for {_LISTING_URL}"
            ) from exc
        if response.status_code != 200:
            raise CollectError(
                self.source_id, fund, f"HTTP {response.status_code} for {_LISTING_URL}"
            )
        self._listing = listing_links(response.text)
        return self._listing

    def fetch(
        self,
        fund: Fund,
        day: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        source = self.source()
        require_collectable(source)
        self._load_listing(client, source, fund)
        pdf_url = self.disclosure_url(fund.ticker)
        try:
            response = client.get(pdf_url, source=source)
        except RobotsDisallowedError as exc:
            raise CollectError(self.source_id, fund, f"robots.txt disallows {pdf_url}") from exc
        except HTTPRetryError as exc:
            raise CollectError(
                self.source_id, fund, f"HTTP {exc.status_code} for {pdf_url}"
            ) from exc
        if response.status_code == 304:
            return []
        if response.status_code != 200:
            raise CollectError(self.source_id, fund, f"HTTP {response.status_code} for {pdf_url}")
        if not response.content.startswith(b"%PDF-"):
            raise CollectError(
                self.source_id,
                fund,
                f"response is not a PDF ({response.headers.get('content-type', 'unknown')})",
            )
        raw = store.write(
            response.content,
            source_id=self.source_id,
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
        return [raw]


COLLECTORS: list[BmoQpdCollector] = [BmoQpdCollector()]
