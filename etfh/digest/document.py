"""Build the weekly "what changed" digest document (FR-3.1 data half).

``build_digest`` turns one built database into the JSON-ready document of
interfaces-08 §3: for every fund with a comparable portfolio by ``week_end``,
what was added, dropped, reweighted, how much the portfolio turned over, how
concentrated it is, how much money the share count implies moved, and which fund
pairs drifted apart or together. Publication policy (D79) decides per fund
whether the security-level lines may appear at all.

Two properties the consumers rely on:

- **point-in-time**: nothing published after :func:`etfh.asof.cutoff_utc` of the
  week end affects the document (D7, D30);
- **deterministic**: the same database produces byte-identical
  ``json.dumps(doc, sort_keys=True)``. Funds are ordered by ticker, overlap
  shifts by size then ticker pair, change lines by size then identifier, and
  every float is rounded to six decimals with negative zero normalised.

The document is deliberately silent about *why* a fund has no data: funds whose
only versions are quarterly top-holdings files are listed in
``quality.funds_without_data`` rather than reported with a comparison of partial
lists (interfaces-08 §3).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

import duckdb

from etfh.analytics import FLOW_METHOD, FundNotFoundError, flows, overlap
from etfh.digest.compare import compare_week
from etfh.digest.policy import securities_disclosed
from etfh.digest.schema import SCHEMA_VERSION, round_value
from etfh.digest.window import iso_date, known_by, week_start
from etfh.registry import Fund, Source

__all__ = ["build_digest"]


def build_digest(
    con: duckdb.DuckDBPyConnection,
    week_end: date,
    *,
    public: bool,
    sources: Mapping[str, Source],
    funds: Sequence[Fund],
    top_n: int = 10,
    overlap_threshold: float = 0.01,
) -> dict[str, Any]:
    """Return the digest document for the week ending on ``week_end``.

    ``sources`` is the loaded ``config/sources.yaml`` registry and ``funds`` the
    loaded ``config/funds.csv`` universe; both decide the publication policy
    (D79). ``top_n`` caps the change lines per fund and the concentration cut;
    ``overlap_threshold`` is the smallest absolute overlap shift worth listing.
    Validate the result with :func:`etfh.digest.validate_digest` before writing
    it anywhere.
    """
    if top_n < 1:
        raise ValueError(f"top_n must be a positive integer, got {top_n}")
    if not math.isfinite(overlap_threshold) or overlap_threshold < 0:
        raise ValueError(f"overlap_threshold must be >= 0, got {overlap_threshold!r}")
    universe = _fund_universe(funds)

    start = week_start(week_end)
    entries: list[dict[str, Any]] = []
    tickers: list[str] = []
    portable: list[str] = []
    for fund in universe:
        comparison = compare_week(con, fund.ticker, start, week_end, top_n=top_n)
        if comparison is None:
            continue
        disclosed = securities_disclosed(fund, sources, public=public)
        flow_sum, flow_days = _flows(con, fund.ticker, start, week_end)
        entries.append(
            {
                "ticker": fund.ticker,
                "issuer": fund.issuer,
                "name": fund.name,
                "securities_disclosed": disclosed,
                "status": comparison.status,
                "portfolio_date_start": iso_date(comparison.start.as_of_date),
                "portfolio_date_end": iso_date(comparison.end.as_of_date),
                "counts": {
                    "adds": comparison.adds,
                    "drops": comparison.drops,
                    "weight_changes": comparison.weight_changes,
                },
                "turnover": round_value(comparison.turnover),
                "top10_weight_start": round_value(comparison.top_weight_start),
                "top10_weight_end": round_value(comparison.top_weight_end),
                "flow_estimate_sum": flow_sum,
                "flow_days": flow_days,
                "flow_method": FLOW_METHOD,
                "changes": comparison.changes[:top_n] if disclosed else [],
            }
        )
        tickers.append(fund.ticker)
        if comparison.known_at_week_start:
            portable.append(fund.ticker)

    return {
        "schema_version": SCHEMA_VERSION,
        "week_start": iso_date(start),
        "week_end": iso_date(week_end),
        "known_by": known_by(week_end),
        "public": bool(public),
        "demo": False,
        "funds": entries,
        "overlap_shifts": _overlap_shifts(con, portable, start, week_end, overlap_threshold),
        "quality": _quality(con, universe, set(tickers)),
    }


def _fund_universe(funds: Sequence[Fund]) -> list[Fund]:
    """Funds sorted by ticker, rejecting a duplicated ticker loudly."""
    seen: set[str] = set()
    for fund in funds:
        if fund.ticker in seen:
            raise ValueError(f"duplicate fund ticker in funds: {fund.ticker!r}")
        seen.add(fund.ticker)
    return sorted(funds, key=lambda fund: fund.ticker)


def _flows(
    con: duckdb.DuckDBPyConnection, fund: str, start: date, end: date
) -> tuple[float | None, int]:
    """Sum of estimated flows inside the week and the days that produced one."""
    try:
        frame = flows(con, fund, start + timedelta(days=1), end, asof=end)
    except FundNotFoundError:
        return None, 0
    values = [float(value) for value in frame["flow_estimate"].to_list() if value is not None]
    if not values:
        return None, 0
    return round_value(math.fsum(values)), len(values)


def _overlap_shifts(
    con: duckdb.DuckDBPyConnection,
    tickers: Sequence[str],
    start: date,
    end: date,
    threshold: float,
) -> list[dict[str, Any]]:
    """Overlap shifts for every pair of funds that has a portfolio at both cutoffs."""
    rows: list[dict[str, Any]] = []
    for index, fund_a in enumerate(tickers):
        for fund_b in tickers[index + 1 :]:
            overlap_start = round_value(overlap(con, fund_a, fund_b, start).overlap)
            overlap_end = round_value(overlap(con, fund_a, fund_b, end).overlap)
            delta = round_value(overlap_end - overlap_start)
            if abs(delta) < threshold:
                continue
            rows.append(
                {
                    "fund_a": fund_a,
                    "fund_b": fund_b,
                    "overlap_start": overlap_start,
                    "overlap_end": overlap_end,
                    "delta": delta,
                }
            )
    rows.sort(key=lambda row: (-abs(row["delta"]), row["fund_a"], row["fund_b"]))
    return rows


def _quality(
    con: duckdb.DuckDBPyConnection, universe: Sequence[Fund], present: set[str]
) -> dict[str, Any]:
    """File accounting of the build plus the funds it could not cover this week.

    ``funds_without_data`` lists funds the database has files for but no
    comparable portfolio by the week end: files that failed validation, or only
    quarterly top-holdings disclosures. A configured fund with no file at all
    belongs to collection coverage (the QA report), not to this document, so it
    is not listed here.
    """
    frame = con.execute("SELECT status, COUNT(*) AS n FROM file_versions GROUP BY status").pl()
    counts = {str(status): int(count) for status, count in frame.iter_rows()}
    known_frame = con.execute("SELECT DISTINCT fund_ticker FROM file_versions").pl()
    known = {str(ticker) for ticker in known_frame["fund_ticker"]}
    return {
        "files_included": counts.get("included", 0),
        "files_excluded": counts.get("excluded", 0),
        "funds_without_data": sorted(
            fund.ticker for fund in universe if fund.ticker in known and fund.ticker not in present
        ),
    }
