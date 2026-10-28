"""Point-in-time week comparison for one fund (interfaces-08 §3).

The comparison never uses the ``holdings_changes`` table: that table is built
from each fund's latest versions (D19), while a digest must compare exactly what
was knowable at the two week cutoffs. Snapshots therefore come straight from
``holdings`` filtered by ``known_at`` (D7, D30, D53).

Snapshot selection mirrors :mod:`etfh.analytics` (daily holdings beat quarterly
disclosures; among equals the latest as-of date, the highest file version, then
the lowest source id) with one deliberate difference: **quarterly top-holdings
files are removed before the choice**, because a partial list must never mix
into a week-over-week comparison (interfaces-08 §3). A fund whose only versions
are quarterly files therefore has no comparable snapshot and stays out of the
document's ``funds`` list.

Status precedence: a fund whose first comparable snapshot arrived inside the
week is ``new_in_window``; a fund whose start and end are the same version is
``no_change_data`` (nothing new was published); everything else is ``ok``.

Thresholds and groupings:

- ``WEIGHT_CHANGE_THRESHOLD`` is the smallest absolute weight move counted as a
  ``weight_change`` (0.0005, half a basis point);
- cash, fx and derivative rows are never compared: a row is non-security when
  its ``asset_class`` is one of those or its id starts with ``CASH:``/``DERIV:``
  (D33);
- unmatched rows (null ``security_id``) still count. They are matched across
  dates on the build's fallback key — name, ticker and currency — so a
  name-only issuer still reports adds, drops and turnover instead of an empty
  comparison. Only rows with a real identifier appear in ``changes``, because a
  change line has to name an identifier to be useful.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import duckdb
import polars as pl

from etfh.asof import cutoff_utc
from etfh.digest.schema import (
    STATUS_NEW_IN_WINDOW,
    STATUS_NO_CHANGE_DATA,
    STATUS_OK,
    round_value,
)
from etfh.types import FileFormat, SourceKind

__all__ = [
    "NON_SECURITY_CLASSES",
    "WEIGHT_CHANGE_THRESHOLD",
    "Snapshot",
    "WeekComparison",
    "compare_week",
    "top_weight",
]

#: Smallest absolute weight move reported as a ``weight_change`` (0.0005 = 0.05
#: percentage points, half a basis point).
WEIGHT_CHANGE_THRESHOLD = 0.0005

#: Asset classes that are not securities (D33). A row is also non-security when
#: its id starts with ``CASH:`` or ``DERIV:``, whatever its asset class.
NON_SECURITY_CLASSES = frozenset({"cash", "fx", "derivative"})

_NON_SECURITY_PREFIXES = ("CASH:", "DERIV:")

#: Prefix of the fallback key used for rows without a ``security_id``, so such a
#: key can never collide with a real identifier.
UNMATCHED_KEY_PREFIX = "name:"

_VERSIONS_SQL = """
SELECT h.as_of_date, h.file_version, h.source_id, h.raw_file_sha256,
       s.kind, s.format
FROM holdings h
LEFT JOIN sources s ON s.source_id = h.source_id
WHERE h.fund_ticker = ? AND h.known_at < ?
GROUP BY 1, 2, 3, 4, 5, 6
"""

_ROWS_SQL = """
SELECT * FROM holdings
WHERE fund_ticker = ? AND as_of_date = ? AND file_version = ? AND source_id = ?
"""

_KEYED_SCHEMA: dict[str, Any] = {
    "_key": pl.String,
    "security_id": pl.String,
    "security_name": pl.String,
    "weight": pl.Float64,
}


@dataclass(frozen=True)
class Snapshot:
    """One fund's portfolio as one file version published it."""

    fund: str
    as_of_date: date
    file_version: int
    source_id: str
    rows: pl.DataFrame

    @property
    def key(self) -> tuple[date, int, str]:
        """Identity of the version, used to tell "same snapshot" from "new"."""
        return (self.as_of_date, self.file_version, self.source_id)


@dataclass(frozen=True)
class WeekComparison:
    """One fund's comparison between the two week cutoffs."""

    fund: str
    status: str
    start: Snapshot
    end: Snapshot
    known_at_week_start: bool
    adds: int
    drops: int
    weight_changes: int
    turnover: float
    top_weight_start: float
    top_weight_end: float
    changes: list[dict[str, Any]]


def compare_week(
    con: duckdb.DuckDBPyConnection,
    fund: str,
    week_start: date,
    week_end: date,
    *,
    top_n: int = 10,
) -> WeekComparison | None:
    """Compare ``fund``'s snapshots at the two cutoffs.

    Returns ``None`` when the fund has no comparable (non-quarterly) portfolio
    known by the ``week_end`` cutoff. An unknown fund is not an error: it simply
    has no snapshots.
    """
    end_versions = _comparable(_versions(con, fund, cutoff_utc(week_end)))
    if end_versions.is_empty():
        return None
    end = _snapshot(con, fund, _newest(end_versions))

    start_versions = _comparable(_versions(con, fund, cutoff_utc(week_start)))
    known_at_week_start = not start_versions.is_empty()
    new_in_window = False
    start: Snapshot
    if known_at_week_start:
        start = _snapshot(con, fund, _newest(start_versions))
    else:
        inside = end_versions.filter(
            (pl.col("as_of_date") >= week_start) & (pl.col("as_of_date") <= week_end)
        )
        if inside.is_empty():
            # Nothing known at week start and nothing dated inside the window:
            # the newest snapshot is all the fund has, so compare it with itself.
            start = end
        else:
            start = _snapshot(con, fund, _oldest(inside))
            new_in_window = True

    if new_in_window:
        status = STATUS_NEW_IN_WINDOW
    elif start.key == end.key:
        status = STATUS_NO_CHANGE_DATA
    else:
        status = STATUS_OK

    adds, drops, weight_changes, turnover, changes = _changes(start.rows, end.rows)
    return WeekComparison(
        fund=fund,
        status=status,
        start=start,
        end=end,
        known_at_week_start=known_at_week_start,
        adds=adds,
        drops=drops,
        weight_changes=weight_changes,
        turnover=turnover,
        top_weight_start=top_weight(start.rows, top_n),
        top_weight_end=top_weight(end.rows, top_n),
        changes=changes,
    )


def top_weight(rows: pl.DataFrame, top_n: int = 10) -> float:
    """Sum of the ``top_n`` largest security weights, as concentration counts them.

    Every security row counts, unmatched rows included; cash, fx and derivative
    rows are excluded (D33). This mirrors ``etfh.analytics.concentration`` but
    works on the snapshot the digest selected rather than on a second query.
    """
    weights = _securities(rows)["weight"]
    if weights.is_empty():
        return 0.0
    return float(weights.sort(descending=True).head(top_n).sum())


def _versions(con: duckdb.DuckDBPyConnection, fund: str, cutoff: datetime) -> pl.DataFrame:
    return con.execute(_VERSIONS_SQL, [fund, cutoff]).pl()


def _comparable(versions: pl.DataFrame) -> pl.DataFrame:
    """Drop quarterly top-holdings versions; everything else is comparable."""
    quarterly = pl.col("kind") == SourceKind.quarterly_disclosure.value
    synthetic_pdf = (pl.col("kind") == SourceKind.synthetic.value) & (
        pl.col("format") == FileFormat.pdf.value
    )
    return versions.filter(~(quarterly.fill_null(False) | synthetic_pdf.fill_null(False)))


def _newest(versions: pl.DataFrame) -> dict[str, Any]:
    """The preferred version: latest date, highest version, lowest source id."""
    row: dict[str, Any] = versions.sort(
        ["as_of_date", "file_version", "source_id"], descending=[True, True, False]
    ).row(0, named=True)
    return row


def _oldest(versions: pl.DataFrame) -> dict[str, Any]:
    """The earliest date in the window, preferring its latest version."""
    row: dict[str, Any] = versions.sort(
        ["as_of_date", "file_version", "source_id"], descending=[False, True, False]
    ).row(0, named=True)
    return row


def _snapshot(con: duckdb.DuckDBPyConnection, fund: str, version: dict[str, Any]) -> Snapshot:
    rows = con.execute(
        _ROWS_SQL,
        [fund, version["as_of_date"], version["file_version"], version["source_id"]],
    ).pl()
    return Snapshot(
        fund=fund,
        as_of_date=version["as_of_date"],
        file_version=int(version["file_version"]),
        source_id=str(version["source_id"]),
        rows=rows.sort(["security_id", "security_name", "weight"]),
    )


def _non_security() -> pl.Expr:
    by_class = pl.col("asset_class").str.to_lowercase().is_in(sorted(NON_SECURITY_CLASSES))
    by_id = pl.col("security_id").str.starts_with(_NON_SECURITY_PREFIXES[0])
    for prefix in _NON_SECURITY_PREFIXES[1:]:
        by_id = by_id | pl.col("security_id").str.starts_with(prefix)
    return by_class | by_id.fill_null(False)


def _securities(rows: pl.DataFrame) -> pl.DataFrame:
    if rows.is_empty():
        return rows
    return rows.filter(~_non_security())


def _key_expr() -> pl.Expr:
    fallback = pl.concat_str(
        [pl.col("security_name"), pl.col("security_ticker"), pl.col("currency")],
        separator="|",
        ignore_nulls=True,
    )
    return pl.coalesce([pl.col("security_id"), pl.lit(UNMATCHED_KEY_PREFIX) + fallback])


def _keyed(rows: pl.DataFrame) -> pl.DataFrame:
    """One row per security key with summed weight; cash/derivatives removed."""
    securities = _securities(rows)
    if securities.is_empty():
        return pl.DataFrame(schema=_KEYED_SCHEMA)
    return (
        securities.with_columns(_key_expr().alias("_key"))
        .group_by("_key")
        .agg(
            pl.col("security_id").drop_nulls().first().alias("security_id"),
            pl.col("security_name").sort().first().alias("security_name"),
            pl.col("weight").sum().alias("weight"),
        )
        .select(["_key", "security_id", "security_name", "weight"])
        .sort("_key")
    )


def _changes(
    start_rows: pl.DataFrame, end_rows: pl.DataFrame
) -> tuple[int, int, int, float, list[dict[str, Any]]]:
    """Adds, drops, weight changes, turnover and change lines between snapshots."""
    before = _keyed(start_rows).rename(
        {
            "weight": "weight_before",
            "security_id": "security_id_before",
            "security_name": "security_name_before",
        }
    )
    after = _keyed(end_rows).rename(
        {
            "weight": "weight_after",
            "security_id": "security_id_after",
            "security_name": "security_name_after",
        }
    )
    joined = before.join(after, on="_key", how="full", coalesce=True).with_columns(
        pl.col("weight_before").is_not_null().alias("_in_before"),
        pl.col("weight_after").is_not_null().alias("_in_after"),
        pl.col("weight_before").fill_null(0.0),
        pl.col("weight_after").fill_null(0.0),
        pl.coalesce("security_id_after", "security_id_before").alias("security_id"),
        pl.coalesce("security_name_after", "security_name_before").alias("security_name"),
    )
    joined = joined.with_columns((pl.col("weight_after") - pl.col("weight_before")).alias("delta"))
    in_both = pl.col("_in_before") & pl.col("_in_after")
    added = joined.filter(~pl.col("_in_before"))
    dropped = joined.filter(~pl.col("_in_after"))
    changed = joined.filter(in_both & (pl.col("delta").abs() >= WEIGHT_CHANGE_THRESHOLD))
    turnover = 0.5 * float(joined["delta"].abs().sum())
    return (
        added.height,
        dropped.height,
        changed.height,
        turnover,
        _change_lines(added, dropped, changed),
    )


def _change_lines(
    added: pl.DataFrame, dropped: pl.DataFrame, changed: pl.DataFrame
) -> list[dict[str, Any]]:
    """JSON-ready change lines, identified securities only, in report order."""
    columns = (
        "security_id",
        "security_name",
        "weight_before",
        "weight_after",
        "delta",
    )
    frames = [
        frame.select(pl.lit(kind).alias("change_type"), *columns)
        for kind, frame in (
            ("add", added),
            ("drop", dropped),
            ("weight_change", changed),
        )
    ]
    combined = pl.concat(frames, how="vertical").filter(pl.col("security_id").is_not_null())
    lines: list[dict[str, Any]] = []
    for row in combined.iter_rows(named=True):
        weight_before = round_value(float(row["weight_before"]))
        weight_after = round_value(float(row["weight_after"]))
        lines.append(
            {
                "change_type": str(row["change_type"]),
                "security_id": str(row["security_id"]),
                "security_name": str(row["security_name"]),
                "weight_before": weight_before,
                "weight_after": weight_after,
                "delta": round_value(weight_after - weight_before),
            }
        )
    lines.sort(key=lambda line: (-abs(float(line["delta"])), str(line["security_id"])))
    return lines
