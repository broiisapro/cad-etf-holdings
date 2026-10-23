"""Vanguard Canada quarterly portfolio disclosure collector.

Vanguard publishes one disclosure PDF per fund at a predictable URL. The host
answers a missing ``/robots.txt`` with 403, which RFC 9309 (D46) treats as
"no restrictions", so the download is reachable; every request still goes
through :class:`PoliteClient` (terms gate, limiter, robots, retries).
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

from etfh.collect.registry import CollectError
from etfh.http import HTTPRetryError, PoliteClient, RobotsDisallowedError
from etfh.registry import Fund, Source, load_sources, require_collectable
from etfh.storage import RawStore
from etfh.types import RawFile

__all__ = ["COLLECTORS", "VanguardQpdCollector"]

_SOURCE_ID = "vanguard_ca_qpd"
_BASE_URL = "https://fund-docs.vanguard.com/qpd-{ticker}-en.pdf"


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


class VanguardQpdCollector:
    """Fetch a fund's quarterly disclosure PDF from Vanguard's document host."""

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

    def disclosure_url(self, ticker: str) -> str:
        return _BASE_URL.format(ticker=ticker.strip().upper())

    def fetch(
        self,
        fund: Fund,
        day: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        source = self._resolve_source(fund)
        require_collectable(source)
        url = self.disclosure_url(fund.ticker)
        etag, last_modified = _conditional_for_url(store, self.source_id, fund.ticker, url)
        try:
            response = client.get(url, source=source, etag=etag, last_modified=last_modified)
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

    def fetch_historical(
        self,
        fund: Fund,
        quarter_end: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        """Report that Vanguard publishes only its current disclosure.

        The fund page links a single stable URL, ``qpd-<TICKER>-en.pdf``, which
        always serves the latest document; it has no date and no archive. Using
        it for an older quarter would store the current document under a
        different quarter, so no request is made and the quarter is reported
        missing. The current quarter is covered by :meth:`fetch` (or reported
        ``present`` by the backfill when its file is already stored).
        """
        source = self._resolve_source(fund)
        require_collectable(source)
        raise CollectError(
            self.source_id,
            fund,
            "not published: Vanguard links only its current quarterly portfolio disclosure "
            f"(qpd-{fund.ticker.strip().upper()}-en.pdf); its fund page lists no older quarters",
        )


COLLECTORS: list[VanguardQpdCollector] = [VanguardQpdCollector()]
