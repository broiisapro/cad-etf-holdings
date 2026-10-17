"""Streamlit dashboard for the point-in-time Canadian ETF holdings dataset.

The app opens the DuckDB file named by ``ETFH_DB`` (default
``./build/etfh.duckdb``) in read-only mode and delegates every query to
:mod:`etfh.analytics`; it never issues SQL itself.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import altair as alt
import duckdb
import polars as pl
import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from etfh import analytics

DEFAULT_DB_PATH = "./build/etfh.duckdb"
CHANGE_WINDOWS: dict[str, int] = {
    "Last 7 days": 7,
    "Last 30 days": 30,
    "Last 90 days": 90,
}


def db_path() -> Path:
    """Return the DuckDB path from ``ETFH_DB`` (default ``./build/etfh.duckdb``)."""
    return Path(os.environ.get("ETFH_DB", DEFAULT_DB_PATH)).expanduser()


def today() -> date:
    """Current date in America/Toronto (the dataset's reference timezone)."""
    return datetime.now(tz=ZoneInfo("America/Toronto")).date()


def percent(value: float) -> str:
    """Format a fraction as a percentage string."""
    return f"{value:.2%}"


def breakdown_chart(data: pl.DataFrame, title: str) -> alt.Chart:
    """Horizontal bar chart of a sector or country breakdown frame."""
    chart: alt.Chart = (
        alt.Chart(data)
        .mark_bar()
        .encode(
            x=alt.X("weight:Q", title="Weight", axis=alt.Axis(format=".0%")),
            y=alt.Y("key:N", title=None, sort="-x"),
            tooltip=[
                alt.Tooltip("key:N", title=title),
                alt.Tooltip("weight:Q", title="Weight", format=".2%"),
            ],
        )
        .properties(title=title)
    )
    return chart


def heatmap_chart(matrix: pl.DataFrame) -> alt.LayerChart:
    """Overlap heatmap for a symmetric fund-by-fund matrix."""
    funds = matrix["fund_a"].unique(maintain_order=True).to_list()
    base = alt.Chart(matrix).encode(
        x=alt.X("fund_a:N", sort=funds, title=None),
        y=alt.Y("fund_b:N", sort=funds, title=None),
    )
    cells = base.mark_rect().encode(
        color=alt.Color(
            "overlap:Q",
            title="Overlap",
            scale=alt.Scale(domain=[0.0, 1.0], scheme="blues"),
        ),
        tooltip=[
            alt.Tooltip("fund_a:N", title="Fund A"),
            alt.Tooltip("fund_b:N", title="Fund B"),
            alt.Tooltip("overlap:Q", title="Overlap", format=".2%"),
        ],
    )
    labels = base.mark_text(baseline="middle", align="center").encode(
        text=alt.Text("overlap:Q", format=".0%"),
        color=alt.condition(alt.datum.overlap >= 0.5, alt.value("white"), alt.value("#1f2937")),
    )
    chart: alt.LayerChart = cells + labels
    return chart


def render_breakdown(
    con: duckdb.DuckDBPyConnection,
    fund: str,
    asof: date,
    column: DeltaGenerator,
    by: Literal["sector", "country"],
) -> None:
    """Render one breakdown chart into ``column``."""
    data = analytics.breakdown(con, fund, asof, by)
    with column:
        if data.is_empty():
            st.caption(f"No {by} data as of this date.")
            return
        st.altair_chart(breakdown_chart(data, by.title()), width="stretch")


def render_fund(con: duckdb.DuckDBPyConnection, fund: str, asof: date) -> None:
    """Holdings, concentration metrics, breakdowns and recent changes for one fund."""
    metrics = analytics.concentration(con, fund, asof)
    heading = fund if metrics.as_of_date is None else f"{fund} — {metrics.as_of_date}"
    st.subheader(heading)
    if metrics.partial:
        st.info("Top holdings only: this fund is covered by a quarterly disclosure.")
    columns = st.columns(3)
    columns[0].metric("Holdings", metrics.n_holdings)
    columns[1].metric("Top 10 weight", percent(metrics.top10_weight))
    columns[2].metric("HHI", f"{metrics.hhi:.4f}")

    holdings = analytics.holdings_asof(con, fund, asof)
    if holdings.is_empty():
        st.caption("No holdings known as of this date.")
    else:
        st.dataframe(holdings, width="stretch", hide_index=True)

    left, right = st.columns(2)
    render_breakdown(con, fund, asof, left, "sector")
    render_breakdown(con, fund, asof, right, "country")

    window = st.selectbox("Change window", list(CHANGE_WINDOWS), index=1, key="change_window")
    start = asof - timedelta(days=CHANGE_WINDOWS[window])
    changes = analytics.changes(con, fund, start, asof)
    st.subheader("Recent changes")
    if changes.is_empty():
        st.caption(f"No changes recorded between {start} and {asof}.")
    else:
        st.dataframe(changes, width="stretch", hide_index=True)


def render_overlap(con: duckdb.DuckDBPyConnection, funds: list[str], asof: date) -> None:
    """Pairwise overlap between two chosen funds and their common holdings."""
    left, right = st.columns(2)
    fund_a = left.selectbox("Fund A", funds, index=0, key="overlap_fund_a")
    fund_b = right.selectbox("Fund B", funds, index=min(1, len(funds) - 1), key="overlap_fund_b")
    if fund_a == fund_b:
        st.info("Select two different funds to compare.")
        return
    result = analytics.overlap(con, fund_a, fund_b, asof)
    st.metric("Overlap", percent(result.overlap))
    if result.partial:
        st.info("Top holdings only: at least one fund is a quarterly disclosure.")
    st.subheader("Common holdings")
    if result.common.is_empty():
        st.caption("No common securities as of this date.")
    else:
        st.dataframe(result.common, width="stretch", hide_index=True)


def render_heatmap(con: duckdb.DuckDBPyConnection, funds: list[str], asof: date) -> None:
    """Overlap heatmap across a chosen set of funds."""
    chosen = st.multiselect(
        "Funds",
        funds,
        default=funds[:3],
        key="heatmap_funds",
        help="Pick two or more funds to compare.",
    )
    if len(chosen) < 2:
        st.info("Select at least two funds to build the overlap heatmap.")
        return
    matrix = analytics.overlap_matrix(con, chosen, asof)
    if matrix.is_empty():
        st.caption("No overlap data as of this date.")
        return
    st.altair_chart(heatmap_chart(matrix), width="stretch")


def render(con: duckdb.DuckDBPyConnection) -> None:
    """Render the whole dashboard against an open read-only connection."""
    try:
        funds = analytics.funds_available(con)
    except duckdb.Error as exc:
        st.error(f"Could not read the fund universe from the database: {exc}")
        return
    if not funds:
        st.warning("No funds are available in this database yet.")
        return

    with st.sidebar:
        st.header("Selection")
        fund = st.selectbox("Fund", funds, index=0, key="fund")
        preview = analytics.concentration(con, fund, today())
        latest = preview.as_of_date or today()
        picked = st.date_input("As of", value=latest, key="asof")
        asof: date = picked if isinstance(picked, date) else latest
        if preview.partial:
            st.caption("Top holdings only for this fund.")
        st.caption(f"Database: {db_path()}")

    fund_tab, overlap_tab, heatmap_tab = st.tabs(["Fund", "Overlap", "Heatmap"])
    with fund_tab:
        render_fund(con, fund, asof)
    with overlap_tab:
        render_overlap(con, funds, asof)
    with heatmap_tab:
        render_heatmap(con, funds, asof)


def main() -> None:
    """Entry point: check the database, open it read-only and render."""
    st.set_page_config(page_title="Canadian ETF holdings", layout="wide")
    st.title("Canadian ETF holdings")
    st.caption("Point-in-time overlap and concentration across published ETF portfolios.")

    path = db_path()
    if not path.is_file():
        st.error(
            f"Database not found: {path}. Build one first with "
            "`uv run etfh build --raw <raw dir> --out <out dir>` or point ETFH_DB "
            "at an existing etfh.duckdb file."
        )
        st.stop()

    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.Error as exc:
        st.error(f"Could not open {path} read-only: {exc}")
        st.stop()

    try:
        render(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
