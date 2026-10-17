"""Vanguard Canada quarterly portfolio disclosure collector.

Vanguard publishes one disclosure PDF per fund at a predictable URL. The host
serves a non-404 4xx for ``/robots.txt``, which D37 treats as disallow-all, so
:class:`PoliteClient` refuses these downloads by design; the collector reports
that refusal as a :class:`~etfh.collect.registry.CollectError` instead of
working around it.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

from etfh.collect.registry import CollectError
from etfh.http import HTTPRetryError, PoliteClient, RobotsDisallowedError
from etfh.registry import Fund, Source, load_sources, require_collectable
from etfh.types import RawFile, RawStore

__all__ = ["COLLECTORS", "VanguardQpdCollector"]

_SOURCE_ID = "vanguard_ca_qpd"
_BASE_URL = "https://fund-docs.vanguard.com/qpd-{ticker}-en.pdf"


def _configured_source(source_id: str) -> Source:
    config_dir = Path(os.environ.get("ETFH_CONFIG_DIR", "config"))
    return load_sources(config_dir / "sources.yaml")[source_id]


class VanguardQpdCollector:
    """Fetch a fund's quarterly disclosure PDF from Vanguard's document host."""

    source_id: str = _SOURCE_ID

    def __init__(self, source: Source | None = None) -> None:
        self._source = source

    def source(self) -> Source:
        if self._source is None:
            self._source = _configured_source(self.source_id)
        return self._source

    def disclosure_url(self, ticker: str) -> str:
        return _BASE_URL.format(ticker=ticker.strip().upper())

    def fetch(
        self,
        fund: Fund,
        day: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        source = self.source()
        require_collectable(source)
        url = self.disclosure_url(fund.ticker)
        try:
            response = client.get(url, source=source)
        except RobotsDisallowedError as exc:
            raise CollectError(self.source_id, fund, f"robots.txt disallows {url}") from exc
        except HTTPRetryError as exc:
            raise CollectError(self.source_id, fund, f"HTTP {exc.status_code} for {url}") from exc
        if response.status_code == 304:
            return []
        if response.status_code == 404:
            raise CollectError(self.source_id, fund, f"no disclosure PDF at {url}")
        if response.status_code != 200:
            raise CollectError(self.source_id, fund, f"HTTP {response.status_code} for {url}")
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


COLLECTORS: list[VanguardQpdCollector] = [VanguardQpdCollector()]
