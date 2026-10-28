"""Large-addition events reconstructed from point-in-time holdings (FR-3.2).

An event is the moment a fund first showed a security in its portfolio (``add``)
or raised that security's weight sharply (``increase``), dated by the local
(America/Toronto) day on which the version that first shows it became known.

The function reads ``file_versions`` and ``holdings`` directly. It never uses
the ``holdings_changes`` table, because that table is built from the latest
version of every as-of date and would leak republished files backwards in time.
The version chain is rebuilt here from ``known_at`` (D7, D20, D30):

- only ``status = 'included'`` portfolio versions take part; a file the build
  excluded cannot produce an event;
- a fund's daily versions are walked in ``known_at`` order, each compared with
  the portfolio that was known immediately before it;
- a republished version of a portfolio date already seen is compared with the
  version it replaces, not with the previous portfolio date. That is how a
  correction can show a new security without repeating the adds the original
  version already produced;
- a version of a new portfolio date is compared with the latest earlier
  portfolio date already known for the fund. The first portfolio of a fund has
  no predecessor and can never produce events; if a late file arrives for a
  portfolio date older than everything already seen, it is treated the same
  way (no predecessor, no events) rather than compared backwards in time;
- a security that appears on several lines of one file is summed by
  ``security_id`` first (D51), so a double listing is one holding, and the name,
  ticker and exchange are chosen deterministically.

Quarterly disclosures are partial top-holdings lists. ``include_quarterly=False``
(the default) ignores them completely. With ``include_quarterly=True`` they run
as their own chain per fund: only additions relative to the previous quarterly
list count, a security missing from a partial list is never a drop, and a weight
change between two partial lists is never an ``increase`` (the missing part of
the list makes the two weights incomparable). Quarterly events carry
``source_kind = "quarterly"``; daily events carry ``"daily"``.

Cash, FX, derivative and documented balancing rows ("Other Assets less
Liabilities", D33/D80c) are not securities, and unmatched rows have no
``security_id``: none of them can become an event.

``start`` and ``end`` are inclusive bounds on ``known_date``. The result is
sorted by ``known_date, fund_ticker, security_id`` and has the dtypes of the
documented event contract (interfaces §5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import duckdb
import polars as pl

from etfh.asof import TORONTO
from etfh.validate import is_balancing_line

__all__ = ["EVENT_COLUMNS", "large_additions"]

#: Public event columns in contract order.
EVENT_COLUMNS: tuple[str, ...] = (
    "fund_ticker",
    "security_id",
    "security_name",
    "security_ticker",
    "exchange",
    "event_type",
    "portfolio_date",
    "known_date",
    "weight_before",
    "weight_after",
    "delta",
    "source_kind",
)

_EVENT_SCHEMA: dict[str, Any] = {
    "fund_ticker": pl.String,
    "security_id": pl.String,
    "security_name": pl.String,
    "security_ticker": pl.String,
    "exchange": pl.String,
    "event_type": pl.String,
    "portfolio_date": pl.Date,
    "known_date": pl.Date,
    "weight_before": pl.Float64,
    "weight_after": pl.Float64,
    "delta": pl.Float64,
    "source_kind": pl.String,
}

_NON_SECURITY_CLASSES = ("cash", "fx", "derivative")
_NON_SECURITY_PREFIXES = ("CASH:", "DERIV:")

_DAILY = "daily"
_QUARTERLY = "quarterly"

#: Columns identifying one portfolio version in both tables.
_VERSION_KEYS = ("fund_ticker", "source_id", "as_of_date", "file_version", "raw_file_sha256")

_VERSION_ORDER = (
    "fund_ticker",
    "known_at",
    "as_of_date",
    "source_id",
    "file_version",
    "raw_file_sha256",
)


@dataclass(frozen=True, slots=True)
class _Holding:
    """One security's row inside one portfolio version."""

    security_name: str
    security_ticker: str | None
    exchange: str | None
    weight: float


def large_additions(
    con: duckdb.DuckDBPyConnection,
    *,
    min_weight: float = 0.01,
    min_increase: float = 0.005,
    start: date | None = None,
    end: date | None = None,
    include_quarterly: bool = False,
) -> pl.DataFrame:
    """Return add/increase events from the included holdings versions.

    ``min_weight`` is the minimum weight (as a fraction) a security must reach
    to become an ``add`` or the ``weight_after`` of an ``increase``.
    ``min_increase`` is the minimum weight change for an ``increase``.
    ``start``/``end`` filter on ``known_date`` inclusively. See the module
    docstring for the full version-chain and quarterly rules.

    The connection is only read; the database may be opened read-only.
    """
    versions = _load_versions(con).with_columns(
        _chain_expression().alias("_chain"),
        pl.col("known_at").dt.convert_time_zone(str(TORONTO)).dt.date().alias("known_date"),
    )
    versions = versions.filter(pl.col("_chain").is_not_null())
    if not include_quarterly:
        versions = versions.filter(pl.col("_chain") == _DAILY)
    versions = versions.sort(list(_VERSION_ORDER))

    if versions.is_empty():
        return _empty_events()

    holdings = _holdings_by_version(_load_holdings(con))
    events: list[dict[str, Any]] = []
    for fund, chain, group in _group_versions(versions):
        _walk_chain(
            fund=fund,
            chain=chain,
            versions=group,
            holdings=holdings,
            min_weight=min_weight,
            min_increase=min_increase,
            events=events,
        )

    frame = pl.DataFrame(events, schema=_EVENT_SCHEMA)
    if start is not None:
        frame = frame.filter(pl.col("known_date") >= start)
    if end is not None:
        frame = frame.filter(pl.col("known_date") <= end)
    return frame.sort(["known_date", "fund_ticker", "security_id"])


def _load_versions(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Included versions with the kind and format of their source."""
    return con.execute(
        """
        SELECT fv.fund_ticker, fv.source_id, fv.as_of_date, fv.file_version,
               fv.known_at, fv.raw_file_sha256,
               COALESCE(s.kind, '') AS kind,
               COALESCE(s.format, '') AS format
        FROM file_versions fv
        LEFT JOIN sources s ON s.source_id = fv.source_id
        WHERE fv.status = 'included'
        """
    ).pl()


def _load_holdings(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Rows of ``holdings`` that belong to an included file version."""
    return con.execute(
        """
        SELECT h.fund_ticker, h.source_id, h.as_of_date, h.file_version,
               h.raw_file_sha256, h.security_id, h.security_name,
               h.security_ticker, h.exchange, h.asset_class, h.weight
        FROM holdings h
        JOIN (
            SELECT DISTINCT fund_ticker, source_id, as_of_date, file_version,
                            raw_file_sha256
            FROM file_versions
            WHERE status = 'included'
        ) fv
          ON fv.fund_ticker = h.fund_ticker
         AND fv.source_id = h.source_id
         AND fv.as_of_date = h.as_of_date
         AND fv.file_version = h.file_version
         AND fv.raw_file_sha256 = h.raw_file_sha256
        """
    ).pl()


def _chain_expression() -> pl.Expr:
    """``daily``/``quarterly`` for holdings sources, null for anything else.

    Quarterly disclosures are ``quarterly_disclosure`` sources and the
    synthetic quarterly PDF fixture; every other holdings source (daily
    issuers and the synthetic CSV/XLSX/JSON files) is daily. Fund-metrics
    sources have no holdings rows and therefore never enter a chain.
    """
    quarterly = (pl.col("kind") == "quarterly_disclosure") | (
        (pl.col("kind") == "synthetic") & (pl.col("format") == "pdf")
    )
    daily = pl.col("kind").is_in(["daily_holdings", "synthetic", ""])
    return (
        pl.when(quarterly)
        .then(pl.lit(_QUARTERLY))
        .when(daily)
        .then(pl.lit(_DAILY))
        .otherwise(pl.lit(None, dtype=pl.String))
    )


def _holdings_by_version(frame: pl.DataFrame) -> dict[tuple[Any, ...], dict[str, _Holding]]:
    """Security rows per version, after exclusions, summed by ``security_id``."""
    if frame.is_empty():
        return {}
    balancing_names = {
        name for name in frame["security_name"].unique().to_list() if is_balancing_line(name)
    }
    ids = pl.col("security_id")
    cleaned = frame.filter(
        ids.is_not_null()
        & ~pl.col("asset_class").str.to_lowercase().is_in(list(_NON_SECURITY_CLASSES))
        & ~ids.str.starts_with(_NON_SECURITY_PREFIXES[0])
        & ~ids.str.starts_with(_NON_SECURITY_PREFIXES[1])
        & ~pl.col("security_name").is_in(sorted(balancing_names))
    )
    rows = cleaned.group_by([*_VERSION_KEYS, "security_id"]).agg(
        pl.col("weight").sum().alias("weight"),
        pl.col("security_name").sort().first().alias("security_name"),
        pl.col("security_ticker").drop_nulls().sort().first().alias("security_ticker"),
        pl.col("exchange").drop_nulls().sort().first().alias("exchange"),
    )
    result: dict[tuple[Any, ...], dict[str, _Holding]] = {}
    for row in rows.iter_rows(named=True):
        key = (
            row["fund_ticker"],
            row["source_id"],
            row["as_of_date"],
            row["file_version"],
            row["raw_file_sha256"],
        )
        result.setdefault(key, {})[row["security_id"]] = _Holding(
            security_name=row["security_name"],
            security_ticker=row["security_ticker"],
            exchange=row["exchange"],
            weight=float(row["weight"]),
        )
    return result


def _group_versions(
    versions: pl.DataFrame,
) -> list[tuple[str, str, pl.DataFrame]]:
    """Versions grouped by fund and chain, in first-appearance order."""
    grouped: list[tuple[str, str, pl.DataFrame]] = []
    for row in (
        versions.select("fund_ticker", "_chain").unique(maintain_order=True).iter_rows(named=True)
    ):
        fund = str(row["fund_ticker"])
        chain = str(row["_chain"])
        group = versions.filter((pl.col("fund_ticker") == fund) & (pl.col("_chain") == chain))
        grouped.append((fund, chain, group))
    return grouped


def _walk_chain(
    *,
    fund: str,
    chain: str,
    versions: pl.DataFrame,
    holdings: dict[tuple[Any, ...], dict[str, _Holding]],
    min_weight: float,
    min_increase: float,
    events: list[dict[str, Any]],
) -> None:
    """Walk one fund's versions of one chain and append its events."""
    snapshots: dict[date, dict[str, _Holding]] = {}
    for version in versions.iter_rows(named=True):
        key = (
            version["fund_ticker"],
            version["source_id"],
            version["as_of_date"],
            version["file_version"],
            version["raw_file_sha256"],
        )
        current = holdings.get(key, {})
        portfolio_date: date = version["as_of_date"]
        known_date: date = version["known_date"]

        if portfolio_date in snapshots:
            # A republish: compare with the version it replaces so an add that
            # the original already reported is not reported again.
            previous = snapshots[portfolio_date]
        else:
            earlier = [seen for seen in snapshots if seen < portfolio_date]
            if not earlier:
                # The first portfolio of this fund/chain has no predecessor.
                snapshots[portfolio_date] = current
                continue
            previous = snapshots[max(earlier)]

        for security_id, holding in current.items():
            if holding.weight < min_weight:
                continue
            before = previous.get(security_id)
            if before is None:
                events.append(
                    _event(
                        fund=fund,
                        security_id=security_id,
                        holding=holding,
                        event_type="add",
                        portfolio_date=portfolio_date,
                        known_date=known_date,
                        weight_before=0.0,
                        weight_after=holding.weight,
                        source_kind=chain,
                    )
                )
            elif chain == _DAILY:
                delta = holding.weight - before.weight
                if delta >= min_increase:
                    events.append(
                        _event(
                            fund=fund,
                            security_id=security_id,
                            holding=holding,
                            event_type="increase",
                            portfolio_date=portfolio_date,
                            known_date=known_date,
                            weight_before=before.weight,
                            weight_after=holding.weight,
                            source_kind=chain,
                        )
                    )
        snapshots[portfolio_date] = current


def _event(
    *,
    fund: str,
    security_id: str,
    holding: _Holding,
    event_type: str,
    portfolio_date: date,
    known_date: date,
    weight_before: float,
    weight_after: float,
    source_kind: str,
) -> dict[str, Any]:
    return {
        "fund_ticker": fund,
        "security_id": security_id,
        "security_name": holding.security_name,
        "security_ticker": holding.security_ticker,
        "exchange": holding.exchange,
        "event_type": event_type,
        "portfolio_date": portfolio_date,
        "known_date": known_date,
        "weight_before": weight_before,
        "weight_after": weight_after,
        "delta": weight_after - weight_before,
        "source_kind": source_kind,
    }


def _empty_events() -> pl.DataFrame:
    return pl.DataFrame(schema=_EVENT_SCHEMA)
