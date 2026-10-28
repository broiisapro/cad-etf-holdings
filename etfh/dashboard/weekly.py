"""The "This week" tab: the weekly digest document, rendered in the dashboard.

The tab shows the same document the static pages publish (interfaces-08 §3,
FR-3.1) for the week that covers the sidebar's as-of date: the week ends on
:func:`etfh.digest.default_week_end` (the most recent Friday), and the document
is built with :func:`etfh.digest.build_digest` from the point-in-time tables, so
it never sees a file published after the week-end cutoff.

Publication policy (D79) follows the deployment, not the tab: the local
dashboard is an internal research tool and asks for ``public=False`` (security
names, ids and weights for every fund), while the public demo passes
``public=True``, so a security line appears only for synthetic funds or for a
source whose terms record redistribution. The demo also sets the document's own
``demo`` flag, exactly as the CLI does, because it renders the synthetic store.

Configuration comes from ``ETFH_CONFIG_DIR`` (default ``./config``) like every
other command; the demo passes the committed ``config/`` directory the demo
build used, so the policy is read from the same sources and funds the data was
built with.

The table builders are plain functions over the document (no Streamlit calls),
so they are testable without a runtime; :mod:`etfh.dashboard.app` owns the
rendering and the per-database cache.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
from polars.datatypes import DataType, DataTypeClass

from etfh.build import connect
from etfh.dashboard.format import MISSING, format_count, percent, signed_money, signed_percent
from etfh.digest import build_digest, default_week_end, validate_digest
from etfh.registry import Fund, Source, load_funds, load_sources

__all__ = [
    "CHANGE_LABELS",
    "DEFAULT_CONFIG_DIR",
    "STATUS_LABELS",
    "changes_table",
    "config_dir",
    "digest_for_week",
    "fund_table",
    "overlap_table",
    "quality_label",
    "undisclosed_funds",
    "week_end",
]

DEFAULT_CONFIG_DIR = Path("config")
"""Config directory used when ``ETFH_CONFIG_DIR`` is unset, as in the CLI."""

STATUS_LABELS: dict[str, str] = {
    "ok": "OK",
    "new_in_window": "New in window",
    "no_change_data": "No change data",
}
"""Week-comparison statuses in the words the static pages use."""

CHANGE_LABELS: dict[str, str] = {
    "add": "Add",
    "drop": "Drop",
    "weight_change": "Weight change",
}
"""Change types in the words the static pages use."""

_FUND_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "Fund": pl.String,
    "Name": pl.String,
    "Status": pl.String,
    "Adds": pl.String,
    "Drops": pl.String,
    "Weight changes": pl.String,
    "Turnover": pl.String,
    "Top 10 weight": pl.String,
    "Estimated flow (CAD)": pl.String,
}

_CHANGE_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "Fund": pl.String,
    "Change": pl.String,
    "Security": pl.String,
    "Security id": pl.String,
    "Weight before": pl.String,
    "Weight after": pl.String,
    "Delta": pl.String,
}

_OVERLAP_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "Fund A": pl.String,
    "Fund B": pl.String,
    "Overlap start": pl.String,
    "Overlap end": pl.String,
    "Delta": pl.String,
}


def config_dir() -> Path:
    """The config directory the command line uses: ``ETFH_CONFIG_DIR`` or ``./config``."""
    return Path(os.environ.get("ETFH_CONFIG_DIR", str(DEFAULT_CONFIG_DIR)))


def week_end(asof: date) -> date:
    """The digest week that covers ``asof``: the most recent Friday on or before it."""
    return default_week_end(asof)


def _load_universe(path: Path) -> tuple[dict[str, Source], list[Fund]]:
    """Load ``sources.yaml`` and ``funds.csv`` from ``path`` (the D79 policy inputs)."""
    sources = load_sources(path / "sources.yaml")
    funds = load_funds(path / "funds.csv", sources)
    return sources, funds


def digest_for_week(
    db_file: Path,
    end: date,
    *,
    public: bool,
    config_dir: Path,
    demo: bool = False,
) -> dict[str, Any]:
    """Build and validate the digest for the week ending on ``end``.

    ``db_file`` is opened read-only through :func:`etfh.build.connect`, so
    ``ETFH_DUCKDB_THREADS`` applies (D49) and the digest can never change the
    database. ``public`` selects the D79 policy and ``demo`` marks a document
    built from the synthetic store, exactly as the CLI does.

    Raises :class:`duckdb.Error` when the database has no build tables yet,
    :class:`OSError` when the config files are missing and :class:`ValueError`
    when the document fails :func:`etfh.digest.validate_digest`.
    """
    sources, funds = _load_universe(config_dir)
    con = connect(db_file, read_only=True)
    try:
        document = build_digest(con, end, public=public, sources=sources, funds=funds)
    finally:
        con.close()
    document["demo"] = demo
    validate_digest(document)
    return document


def fund_table(document: dict[str, Any]) -> pl.DataFrame:
    """One row per fund with a comparable portfolio, ready for ``st.dataframe``."""
    rows = [
        {
            "Fund": entry["ticker"],
            "Name": entry["name"],
            "Status": STATUS_LABELS.get(entry["status"], entry["status"]),
            "Adds": format_count(entry["counts"]["adds"]),
            "Drops": format_count(entry["counts"]["drops"]),
            "Weight changes": format_count(entry["counts"]["weight_changes"]),
            "Turnover": percent(entry["turnover"]),
            "Top 10 weight": percent(entry["top10_weight_end"]),
            "Estimated flow (CAD)": signed_money(entry["flow_estimate_sum"]),
        }
        for entry in document["funds"]
    ]
    return pl.DataFrame(rows, schema=_FUND_SCHEMA)


def changes_table(document: dict[str, Any]) -> pl.DataFrame:
    """Security-level change lines of the funds that may disclose securities (D79)."""
    rows = [
        {
            "Fund": entry["ticker"],
            "Change": CHANGE_LABELS.get(change["change_type"], change["change_type"]),
            "Security": change["security_name"],
            "Security id": change["security_id"] or MISSING,
            "Weight before": percent(change["weight_before"]),
            "Weight after": percent(change["weight_after"]),
            "Delta": signed_percent(change["delta"]),
        }
        for entry in document["funds"]
        if entry["securities_disclosed"]
        for change in entry["changes"]
    ]
    return pl.DataFrame(rows, schema=_CHANGE_SCHEMA)


def overlap_table(document: dict[str, Any]) -> pl.DataFrame:
    """Overlap shifts above the document's threshold, largest move first."""
    rows = [
        {
            "Fund A": shift["fund_a"],
            "Fund B": shift["fund_b"],
            "Overlap start": percent(shift["overlap_start"]),
            "Overlap end": percent(shift["overlap_end"]),
            "Delta": signed_percent(shift["delta"]),
        }
        for shift in document["overlap_shifts"]
    ]
    return pl.DataFrame(rows, schema=_OVERLAP_SCHEMA)


def undisclosed_funds(document: dict[str, Any]) -> list[str]:
    """Funds whose security lines the policy hides, sorted by ticker."""
    return sorted(
        entry["ticker"] for entry in document["funds"] if not entry["securities_disclosed"]
    )


def quality_label(document: dict[str, Any]) -> str:
    """The file accounting of the build plus the funds it could not compare this week."""
    quality = document["quality"]
    without_data = quality["funds_without_data"]
    listed = ", ".join(without_data) if without_data else "none"
    return (
        f"Files included: {format_count(quality['files_included'])}; "
        f"excluded: {format_count(quality['files_excluded'])}. "
        f"Funds without a comparable portfolio this week: {listed}."
    )
