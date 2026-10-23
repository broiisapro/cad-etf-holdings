"""Quarterly disclosure backfill (FR-2.2, D68).

The backfill walks a fund's published quarterly portfolio disclosures a few
quarters back and stores whatever the issuer still links, so the normalized
holdings panel can cover more than the current quarter. It is deliberately
conservative:

- Only sources with an approved terms record are used, and every request goes
  through :class:`~etfh.http.PoliteClient` (per-host limiter, robots.txt, the
  source's ``Crawl-delay``).
- A file is discovered from the issuer's own listing (product page document
  list or fund JSON document index). A URL is never guessed beyond a pattern
  the issuer itself publishes.
- Every requested quarter is reported as ``fetched``, ``present`` (already in
  the raw store, no request made) or ``missing`` with the reason. A quarter
  that no longer appears on the issuer's page is reported, never fabricated.

Quarter ends are the last ``quarters`` calendar quarter-ends strictly before
today in America/Toronto.
"""

from __future__ import annotations

import calendar
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from etfh.collect.registry import CollectError, collector_for
from etfh.http import PoliteClient
from etfh.parse.qpd_pdf import disclosure_as_of
from etfh.registry import (
    Fund,
    Source,
    funds_for_source,
    is_collectable,
    load_funds,
    load_sources,
)
from etfh.storage import RawStore
from etfh.types import RawFile, SourceKind

__all__ = ["BackfillError", "BackfillResult", "backfill_qpd", "quarter_ends"]

_LOGGER = logging.getLogger(__name__)
_TORONTO = ZoneInfo("America/Toronto")

# A disclosure is dated at its portfolio date, which is the quarter end for
# every issuer we collect. A few days of slack absorbs a document dated on the
# last business day of the quarter; quarterly dates are far enough apart that
# the window cannot match two requested quarters.
_REPORT_TOLERANCE_DAYS = 7

BackfillStatus = Literal["fetched", "present", "missing"]


class BackfillError(ValueError):
    """Raised when a backfill cannot start for the requested issuer."""


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """One fund x quarter outcome of a backfill run."""

    fund_ticker: str
    quarter_end: date
    status: BackfillStatus
    detail: str
    raw: RawFile | None = None


def _last_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _quarter_end_on_or_before(day: date) -> date:
    quarter = (day.month - 1) // 3 + 1
    end = _last_day(day.year, quarter * 3)
    if end <= day:
        return end
    if quarter == 1:
        return date(day.year - 1, 12, 31)
    return _last_day(day.year, (quarter - 1) * 3)


def quarter_ends(quarters: int, today: date) -> list[date]:
    """Return the last ``quarters`` quarter-ends strictly before ``today``.

    The result is ascending (oldest first). Quarter-ends are calendar
    quarter-ends, so the list crosses year boundaries and honours leap years.
    """
    if quarters < 1:
        raise BackfillError("quarters must be at least 1")
    latest = _quarter_end_on_or_before(today - timedelta(days=1))
    ends: list[date] = []
    end = latest
    for _ in range(quarters):
        ends.append(end)
        end = _quarter_end_on_or_before(end - timedelta(days=1))
    return sorted(ends)


def _resolve_source(sources: dict[str, Source], issuer: str) -> Source:
    matching = [source for source in sources.values() if source.issuer == issuer]
    if not matching:
        raise BackfillError(f"unknown issuer {issuer!r}")
    disclosures = [source for source in matching if source.kind is SourceKind.quarterly_disclosure]
    if not disclosures:
        raise BackfillError(f"issuer {issuer!r} has no quarterly disclosure source")
    for source in disclosures:
        if is_collectable(source):
            return source
    ids = ", ".join(source.id for source in disclosures)
    raise BackfillError(f"issuer {issuer!r} has no approved quarterly disclosure source ({ids})")


def _select_funds(
    sources: dict[str, Source],
    config_dir: Path,
    source: Source,
    tickers: Sequence[str] | None,
) -> list[Fund]:
    funds = funds_for_source(load_funds(config_dir / "funds.csv", sources), source.id)
    if not funds:
        raise BackfillError(f"no fund lists source {source.id!r}")
    if tickers is None:
        return funds
    wanted = {ticker.strip().upper() for ticker in tickers}
    known = {fund.ticker for fund in funds}
    unknown = sorted(wanted - known)
    if unknown:
        raise BackfillError(
            f"fund(s) {', '.join(unknown)} are not configured for source {source.id!r}"
        )
    return [fund for fund in funds if fund.ticker in wanted]


@contextmanager
def _active_client(client: PoliteClient | None) -> Iterator[PoliteClient]:
    if client is not None:
        yield client
        return
    with PoliteClient() as created:
        yield created


def _group_raw(store: RawStore) -> dict[tuple[str, str], list[RawFile]]:
    grouped: dict[tuple[str, str], list[RawFile]] = {}
    for raw in store.iter_raw():
        grouped.setdefault((raw.source_id, raw.fund_ticker), []).append(raw)
    return grouped


def _stated_date(raw: RawFile) -> date | None:
    if raw.as_of_hint is not None:
        return raw.as_of_hint
    try:
        return disclosure_as_of(raw)
    except Exception as exc:  # a stored file that cannot be read must not stop the run
        _LOGGER.info("cannot read the portfolio date of %s: %s", raw.path, exc)
        return None


def _stored_reports(raws: Sequence[RawFile]) -> list[tuple[RawFile, date]]:
    reports: list[tuple[RawFile, date]] = []
    for raw in raws:
        if raw.ext.lower() != "pdf":
            continue
        stated = _stated_date(raw)
        if stated is not None:
            reports.append((raw, stated))
    return reports


def _stored_for_quarter(
    reports: Sequence[tuple[RawFile, date]], quarter_end: date
) -> RawFile | None:
    matches = [
        raw for raw, stated in reports if abs((stated - quarter_end).days) <= _REPORT_TOLERANCE_DAYS
    ]
    if not matches:
        return None
    return max(matches, key=lambda raw: raw.fetched_at)


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def backfill_qpd(
    issuer: str,
    quarters: int,
    raw_root: Path,
    *,
    config_dir: Path = Path("config"),
    client: PoliteClient | None = None,
    today: date | None = None,
    funds: Sequence[str] | None = None,
) -> list[BackfillResult]:
    """Backfill historical quarterly disclosures for one issuer.

    ``quarters`` counts calendar quarter-ends before today (America/Toronto).
    Every fund configured for the issuer's approved quarterly-disclosure source
    is covered, in ``config/funds.csv`` order, unless ``funds`` names a subset
    of tickers. ``today`` and ``client`` are injectable for tests; the caller
    owns a supplied client.

    Raises :class:`BackfillError` for an unknown issuer, an issuer without an
    approved quarterly-disclosure source, a ticker that is not configured for
    the source, or a collector that cannot fetch history. Per-quarter network
    and discovery failures are reported as ``missing`` instead.
    """
    config_dir = Path(config_dir)
    today_date = today if today is not None else datetime.now(_TORONTO).date()
    ends = quarter_ends(quarters, today_date)
    sources = load_sources(config_dir / "sources.yaml")
    source = _resolve_source(sources, issuer)
    funds_for_source_list = _select_funds(sources, config_dir, source, funds)
    collector = collector_for(source)
    if collector is None:
        raise BackfillError(f"no collector is registered for source {source.id!r}")
    fetch_historical = getattr(collector, "fetch_historical", None)
    if fetch_historical is None:
        raise BackfillError(f"source {source.id!r} has no historical collector")

    store = RawStore(raw_root)
    grouped = _group_raw(store)
    results: list[BackfillResult] = []
    with _active_client(client) as active:
        for fund in funds_for_source_list:
            reports = _stored_reports(grouped.get((source.id, fund.ticker), []))
            for quarter_end in ends:
                stored = _stored_for_quarter(reports, quarter_end)
                if stored is not None:
                    results.append(
                        BackfillResult(
                            fund_ticker=fund.ticker,
                            quarter_end=quarter_end,
                            status="present",
                            detail=f"already stored: {_display_path(stored.path, store.root)}",
                            raw=stored,
                        )
                    )
                    continue
                try:
                    fetched = fetch_historical(fund, quarter_end, active, store)
                except CollectError as exc:
                    results.append(
                        BackfillResult(
                            fund_ticker=fund.ticker,
                            quarter_end=quarter_end,
                            status="missing",
                            detail=exc.reason,
                        )
                    )
                    continue
                if not fetched:
                    results.append(
                        BackfillResult(
                            fund_ticker=fund.ticker,
                            quarter_end=quarter_end,
                            status="missing",
                            detail="the collector returned no file for this quarter",
                        )
                    )
                    continue
                raw = fetched[0]
                results.append(
                    BackfillResult(
                        fund_ticker=fund.ticker,
                        quarter_end=quarter_end,
                        status="fetched",
                        detail=f"downloaded {raw.url}",
                        raw=raw,
                    )
                )
    return results
