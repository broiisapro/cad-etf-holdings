"""Collector for Purpose Investments daily fund JSON.

The fund endpoint (``https://www.purposeinvest.com/api/funds/<slug>``) is a
single JSON document per fund. The slug comes from ``Fund.params``
(``purpose_slug``). Files are stored unmodified through :class:`RawStore`;
conditional requests reuse the stored ETag / Last-Modified so an unchanged
document produces a 304 and no new version.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from etfh.collect.registry import CollectError
from etfh.http import HTTPRetryError, PoliteClient
from etfh.registry import Fund, Source, load_sources
from etfh.storage import RawStore
from etfh.types import RawFile

__all__ = ["COLLECTORS", "PurposeCollector"]

_API_URL = "https://www.purposeinvest.com/api/funds/{slug}"
_CONFIG_PATH = Path("config/sources.yaml")


class PurposeCollector:
    """Fetch the daily holdings JSON for one Purpose fund."""

    source_id = "purpose_daily"

    def __init__(self, source: Source | None = None) -> None:
        self.source = source

    def bind(self, source: Source) -> None:
        """Bind the registry entry the collector should fetch under."""
        self.source = source

    def fetch(
        self,
        fund: Fund,
        day: date,
        client: PoliteClient,
        store: RawStore,
    ) -> list[RawFile]:
        """Fetch and store the current holdings document for ``fund``."""
        slug = fund.params.get("purpose_slug")
        if not slug:
            raise CollectError(
                self.source_id,
                fund.ticker,
                "missing purpose_slug in fund params",
            )
        source = self._resolve_source(fund)
        etag, last_modified = _conditional_headers(store, self.source_id, fund.ticker)
        url = _API_URL.format(slug=slug)
        try:
            response = client.get(url, source=source, etag=etag, last_modified=last_modified)
        except (HTTPRetryError, httpx.HTTPError) as exc:
            raise CollectError(self.source_id, fund.ticker, str(exc)) from exc
        if response.status_code == 304:
            return []
        if response.status_code != 200:
            raise CollectError(
                self.source_id,
                fund.ticker,
                f"unexpected HTTP status {response.status_code} for {url}",
            )
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise CollectError(
                self.source_id,
                fund.ticker,
                f"expected JSON from {url}, got content-type {content_type!r}",
            )
        try:
            payload = response.json()
            as_of_hint = date.fromisoformat(payload["portfolio"]["dt"])
        except (ValueError, KeyError, TypeError) as exc:
            raise CollectError(
                self.source_id,
                fund.ticker,
                f"malformed fund document from {url}: {exc}",
            ) from exc
        raw = store.write(
            response.content,
            source_id=source.id,
            issuer=source.issuer,
            fund_ticker=fund.ticker,
            url=str(response.url),
            fetched_at=datetime.now(UTC),
            ext="json",
            content_type=content_type or "application/json",
            as_of_hint=as_of_hint,
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
        self.source = sources[self.source_id]
        return self.source


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


COLLECTORS = [PurposeCollector()]
