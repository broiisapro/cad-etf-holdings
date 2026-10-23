"""Streamlit dashboard for the point-in-time Canadian ETF holdings dataset.

The app opens the DuckDB file named by ``ETFH_DB`` (default
``./build/etfh.duckdb``) in read-only mode, through :func:`etfh.build.connect`
so ``ETFH_DUCKDB_THREADS`` applies, and delegates every query to
:mod:`etfh.analytics` and :mod:`etfh.qa`; it never issues SQL itself.
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
from etfh.build import connect
from etfh.dashboard import demo, quality

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


def open_database(path: Path) -> duckdb.DuckDBPyConnection:
    """Open ``path`` read-only, honouring ``ETFH_DUCKDB_THREADS`` (D49)."""
    return connect(path, read_only=True)


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


def render_quality(state: quality.FundQuality) -> None:
    """Data-quality panel: portfolio date, source, coverage and exclusions."""
    columns = st.columns(4)
    columns[0].metric(
        "Portfolio date",
        state.as_of_date.isoformat() if state.as_of_date is not None else "-",
    )
    columns[1].metric("Source", state.source_label)
    columns[2].metric("Coverage", state.coverage_label)
    columns[3].metric(
        "Unmatched weight share",
        "n/a" if state.unmatched_share is None else percent(state.unmatched_share),
    )
    if state.as_of_date is None:
        st.caption("No included file is known for this fund as of the selected date.")
    elif state.unmatched_share is None:
        st.caption("Unmatched weight is not measured for this fund version.")
    if state.excluded_files:
        for excluded in state.excluded_files:
            st.warning(
                f"Excluded file: {excluded.source_id} as of {excluded.as_of_label} "
                f"({excluded.reason})."
            )
    else:
        st.caption("No excluded files for this fund.")


def render_fund(con: duckdb.DuckDBPyConnection, fund: str, asof: date) -> None:
    """Holdings, data quality, breakdowns and recent changes for one fund."""
    metrics = analytics.concentration(con, fund, asof)
    heading = fund if metrics.as_of_date is None else f"{fund} — {metrics.as_of_date}"
    st.subheader(heading)
    if metrics.partial:
        st.info("Top holdings only: this source publishes only part of the portfolio.")
    columns = st.columns(3)
    columns[0].metric("Holdings", metrics.n_holdings)
    columns[1].metric("Top 10 weight", percent(metrics.top10_weight))
    columns[2].metric("HHI", f"{metrics.hhi:.4f}")

    holdings = analytics.holdings_asof(con, fund, asof)
    st.subheader("Data quality")
    render_quality(quality.fund_quality(con, fund, asof, holdings=holdings))

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
        st.info("Top holdings only: at least one fund publishes only part of its portfolio.")
    note = quality.understated_note(
        list(quality.fund_qualities(con, [fund_a, fund_b], asof).values())
    )
    if note:
        st.warning(note)
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
    note = quality.understated_note(list(quality.fund_qualities(con, chosen, asof).values()))
    if note:
        st.warning(note)
    st.altair_chart(heatmap_chart(matrix), width="stretch")


def render(con: duckdb.DuckDBPyConnection, *, demo_mode: bool = False) -> None:
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
        reference = demo.DEMO_LATEST_ASOF if demo_mode else today()
        preview = analytics.concentration(con, fund, reference)
        latest = preview.as_of_date or today()
        picked = st.date_input("As of", value=latest, key="asof")
        asof: date = picked if isinstance(picked, date) else latest
        if preview.partial:
            st.caption("Top holdings only for this fund.")
        if demo_mode:
            st.caption("Database: synthetic demo (temporary build)")
        else:
            st.caption(f"Database: {db_path()}")

    fund_tab, overlap_tab, heatmap_tab = st.tabs(["Fund", "Overlap", "Heatmap"])
    with fund_tab:
        render_fund(con, fund, asof)
    with overlap_tab:
        render_overlap(con, funds, asof)
    with heatmap_tab:
        render_heatmap(con, funds, asof)


def main(*, force_demo: bool = False) -> None:
    """Entry point: check the database, open it read-only and render.

    ``force_demo`` is used by the repository-root ``streamlit_app.py`` entry
    point so the public Community Cloud deployment always runs on synthetic
    data without needing an environment variable.
    """
    st.set_page_config(page_title="Canadian ETF holdings", layout="wide")
    st.title("Canadian ETF holdings")
    st.caption("Point-in-time overlap and concentration across published ETF portfolios.")

    demo_mode = force_demo or demo.is_demo()
    if demo_mode:
        st.info(demo.DEMO_BANNER)
        try:
            path = demo.demo_database().db_path
        except demo.DemoError as exc:
            st.error(str(exc))
            st.stop()
            return
    else:
        path = db_path()

    if not path.is_file():
        st.error(
            f"Database not found: {path}. Build one first with "
            "`uv run etfh build --raw <raw dir> --out <out dir>` or point ETFH_DB "
            "at an existing etfh.duckdb file."
        )
        st.stop()

    try:
        con = open_database(path)
    except (duckdb.Error, ValueError) as exc:
        st.error(f"Could not open {path} read-only: {exc}")
        st.stop()

    try:
        render(con, demo_mode=demo_mode)
    finally:
        con.close()


if __name__ == "__main__":
    main()
