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
from typing import Any, Literal
from zoneinfo import ZoneInfo

import altair as alt
import duckdb
import polars as pl
import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from etfh import analytics
from etfh.build import connect
from etfh.dashboard import demo, quality, weekly
from etfh.dashboard.format import format_amount, format_number, percent

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


def flows_chart(flows: pl.DataFrame) -> alt.Chart:
    """Bar chart of estimated daily flows; rows without an estimate are omitted."""
    data = flows.filter(pl.col("flow_estimate").is_not_null())
    chart: alt.Chart = (
        alt.Chart(data)
        .mark_bar()
        .encode(
            x=alt.X("as_of_date:T", title=None),
            y=alt.Y("flow_estimate:Q", title="Estimated flow", axis=alt.Axis(format="~s")),
            tooltip=[
                alt.Tooltip("as_of_date:T", title="Date"),
                alt.Tooltip("flow_estimate:Q", title="Estimated flow", format=",.0f"),
                alt.Tooltip("prev_as_of_date:T", title="Previous date"),
                alt.Tooltip("gap_days:Q", title="Business days since previous"),
            ],
        )
        .properties(title="Estimated daily flows")
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
    render_metrics_qa(state.metrics)


def render_metrics_qa(state: quality.MetricsQa) -> None:
    """Fund-metrics findings the QA report records, for this fund and overall."""
    if not (state.has_inputs or state.has_findings):
        return
    for finding in state.findings:
        st.warning(
            f"Fund metrics {finding.severity} {finding.check}: {finding.detail} "
            f"({finding.source_id})"
        )
    if state.has_findings:
        st.caption(f"Fund-metrics findings: {state.counts_label}.")
    else:
        st.caption("No fund-metrics findings.")


def flows_table(flows: pl.DataFrame) -> pl.DataFrame:
    """The flow columns shown in the dashboard table, with display names."""
    return flows.select(
        pl.col("as_of_date").alias("As of"),
        pl.col("prev_as_of_date").alias("Previous date"),
        pl.col("shares_outstanding").alias("Shares outstanding"),
        pl.col("nav_per_unit").alias("NAV per unit"),
        pl.col("flow_estimate").alias("Estimated flow"),
        pl.col("gap_days").alias("Business days gap"),
    )


def render_metrics(con: duckdb.DuckDBPyConnection, fund: str, asof: date) -> None:
    """Latest fund metrics, how stale they are, and the D67 flow estimates."""
    st.subheader("Fund metrics & flows")
    view = quality.fund_metrics(con, fund, asof)
    latest = view.latest
    if latest is None:
        st.caption(f"No fund metrics for {fund} yet.")
        st.caption(
            "`etfh collect-aum` records NAV per unit, shares outstanding and AUM "
            "when the issuer publishes them."
        )
        return
    columns = st.columns(3)
    columns[0].metric("NAV per unit", format_number(latest.nav_per_unit, 4))
    columns[1].metric("Shares outstanding", format_number(latest.shares_outstanding, 0))
    columns[2].metric("AUM", format_amount(latest.aum, latest.currency))
    st.caption(f"Latest figures as of {latest.as_of_date.isoformat()}; {view.staleness_label}.")
    if view.is_stale:
        st.warning(
            f"Fund metrics are {view.staleness_days} days old: the figures and flow "
            "estimates reflect the issuer's stated date, not the selected date."
        )
    if view.n_dates < 2:
        st.caption(
            "Flow estimates need at least two metrics dates with share counts; only one "
            "date is known as of this date, so no flow is shown."
        )
    else:
        st.dataframe(flows_table(view.flows), width="stretch", hide_index=True)
        if view.can_estimate_flows:
            st.altair_chart(flows_chart(view.flows), width="stretch")
        else:
            st.caption("No flow estimate yet: the known dates do not carry two share counts.")
    st.caption(f"Method: {view.method}")


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
    render_metrics(con, fund, asof)

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


@st.cache_data(show_spinner="Reading the weekly digest…")
def _cached_digest(
    db_file: str,
    db_modified_ns: int,
    end: date,
    public: bool,
    config_dir: str,
    is_demo: bool,
) -> dict[str, Any]:
    """One digest per database revision, week, policy and config directory.

    ``db_modified_ns`` only participates in the cache key, so a rebuilt
    database is read again while repeated interactions on the same file reuse
    the document (a real store takes a few seconds to compare every fund pair).
    """
    del db_modified_ns
    return weekly.digest_for_week(
        Path(db_file),
        end,
        public=public,
        config_dir=Path(config_dir),
        demo=is_demo,
    )


def render_week(db_file: Path, asof: date, *, demo_mode: bool) -> None:
    """The weekly digest for the week that contains ``asof`` (FR-3.1, D79).

    Publication policy follows the deployment: the public demo builds a public
    document, so security-level lines appear only for synthetic funds or for
    sources that allow redistribution; the local dashboard is internal and
    shows every fund's lines.
    """
    end = weekly.week_end(asof)
    st.subheader(f"Week ending {end.isoformat()}")
    public = demo_mode
    config = demo.DEMO_CONFIG_DIR if demo_mode else weekly.config_dir()
    try:
        modified_ns = db_file.stat().st_mtime_ns
    except OSError:
        modified_ns = 0
    try:
        document = _cached_digest(str(db_file), modified_ns, end, public, str(config), demo_mode)
    except duckdb.Error as exc:
        st.caption(
            f"No weekly digest is available in this database yet: {exc}. "
            "Build it with `etfh build` and check back."
        )
        return
    except OSError as exc:
        st.error(f"Could not read the digest configuration ({exc}).")
        return

    st.caption(
        f"Week {document['week_start']} to {document['week_end']}; "
        f"portfolios known by {document['known_by']}."
    )
    funds = weekly.fund_table(document)
    if funds.is_empty():
        st.caption("No fund has a comparable portfolio for this week yet.")
        return
    st.dataframe(funds, width="stretch", hide_index=True)

    st.subheader("Security changes")
    changes = weekly.changes_table(document)
    if changes.is_empty():
        st.caption("No security-level changes are available for this week.")
    else:
        st.dataframe(changes, width="stretch", hide_index=True)
    hidden = weekly.undisclosed_funds(document)
    if hidden:
        st.caption(
            "Security lines are hidden for funds whose sources do not allow "
            f"redistribution (D79): {', '.join(hidden)}."
        )

    st.subheader("Overlap shifts")
    shifts = weekly.overlap_table(document)
    if shifts.is_empty():
        st.caption("No fund pair moved its overlap by the reporting threshold this week.")
    else:
        st.dataframe(shifts, width="stretch", hide_index=True)

    st.caption(weekly.quality_label(document))


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


def render(con: duckdb.DuckDBPyConnection, *, db_file: Path, demo_mode: bool = False) -> None:
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

    fund_tab, week_tab, overlap_tab, heatmap_tab = st.tabs(
        ["Fund", "This week", "Overlap", "Heatmap"]
    )
    with fund_tab:
        render_fund(con, fund, asof)
    with week_tab:
        render_week(db_file, asof, demo_mode=demo_mode)
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
        render(con, demo_mode=demo_mode, db_file=path)
    finally:
        con.close()


if __name__ == "__main__":
    main()
