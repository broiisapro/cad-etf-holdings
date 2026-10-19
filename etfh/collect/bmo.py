"""Collector for BMO Global Asset Management daily holdings XLSX.

The holdings URL embeds the portfolio date:
``/assets/static/reports/etf-funds-holdings/Holdings_Extract_en_US_<TICKER>_<YYYYMMDD>.xlsx``.

The profile endpoint named in the issue
(``/api/sca/etf-fund-profile-comp?profileId=<TICKER>-a...``) no longer returns
JSON: it serves a ~3.7 MB HTML application shell with no fund data (verified
2026-10-17), so it cannot date the file without a JavaScript-capable fetch,
which D26 forbids. The collector therefore tries the requested day first and
steps back through business days (at most 5 steps) until a file answers 200;
404s are expected and other HTTP errors abort. Each successful file's own
name date is stored as the raw file's as-of hint.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from etfh.collect.registry import CollectError
from etfh.http import HTTPRetryError, PoliteClient
from etfh.registry import Fund, Source, load_sources
from etfh.storage import RawStore
from etfh.types import RawFile

__all__ = ["COLLECTORS", "BmoCollector", "candidate_days"]

_SOURCE_ID = "bmo_daily"
_FILE_URL = (
    "https://df.bmogam.com/assets/static/reports/etf-funds-holdings/"
    "Holdings_Extract_en_US_{ticker}_{stamp}.xlsx"
)
_CONFIG_PATH = Path("config/sources.yaml")
_MAX_STEPS_BACK = 5


def _most_recent_business_day(day: date) -> date:
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def candidate_days(day: date, steps: int = _MAX_STEPS_BACK) -> list[date]:
    """``day`` (rounded back to a business day) plus up to ``steps`` earlier ones."""
    candidates = [_most_recent_business_day(day)]
    current = candidates[0]
    for _ in range(steps):
        current = _most_recent_business_day(current - timedelta(days=1))
        candidates.append(current)
    return candidates


class BmoCollector:
    """Find and store the newest BMO holdings workbook near a requested day.

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
        """Fetch the most recent holdings workbook at or before ``day``."""
        source = self._resolve_source(fund)
        for candidate in candidate_days(day):
            url = _FILE_URL.format(ticker=fund.ticker.upper(), stamp=candidate.strftime("%Y%m%d"))
            etag, last_modified = _conditional_for_url(store, self.source_id, fund.ticker, url)
            try:
                response = client.get(url, source=source, etag=etag, last_modified=last_modified)
            except (HTTPRetryError, httpx.HTTPError) as exc:
                raise CollectError(self.source_id, fund, str(exc)) from exc
            if response.status_code == 404:
                continue
            if response.status_code == 304:
                return []
            if response.status_code != 200:
                raise CollectError(
                    self.source_id,
                    fund,
                    f"unexpected HTTP status {response.status_code} for {url}",
                )
            content_type = response.headers.get("content-type", "")
            if "spreadsheet" not in content_type.lower() and "xlsx" not in content_type.lower():
                raise CollectError(
                    self.source_id,
                    fund,
                    f"expected XLSX from {url}, got content-type {content_type!r}",
                )
            if not response.content.startswith(b"PK"):
                raise CollectError(
                    self.source_id,
                    fund,
                    f"expected an XLSX archive from {url}, got {response.content[:8]!r}",
                )
            raw = store.write(
                response.content,
                source_id=source.id,
                issuer=source.issuer,
                fund_ticker=fund.ticker,
                url=str(response.url),
                fetched_at=datetime.now(UTC),
                ext="xlsx",
                content_type=content_type,
                as_of_hint=candidate,
                http_status=response.status_code,
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
            )
            return [raw]
        raise CollectError(
            self.source_id,
            fund,
            f"no holdings file found for {fund.ticker} in the last "
            f"{_MAX_STEPS_BACK} business days up to {day.isoformat()}",
        )

    def _resolve_source(self, fund: Fund) -> Source:
        if self.source is not None:
            return self.source
        sources = load_sources(_CONFIG_PATH)
        if self.source_id not in sources:
            raise CollectError(self.source_id, fund, f"source {self.source_id!r} not in config")
        return sources[self.source_id]


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


COLLECTORS = [BmoCollector()]
