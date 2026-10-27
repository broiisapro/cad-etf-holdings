"""Tests for the Streamlit dashboard (``etfh/dashboard/app.py``)."""

from __future__ import annotations

import importlib
import json
import math
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import ModuleType

import duckdb
import pytest
from streamlit.testing.v1 import AppTest

from etfh.dashboard import quality
from tests._dashboard_helpers import (
    APP_PATH,
    AS_OF,
    METRICS_DB_AS_OF,
    METRICS_DB_FUNDS,
    QUALITY_AS_OF,
    QUALITY_FUNDS,
    REPO_ROOT,
    build_synthetic_db,
    install_fake_analytics,
    make_empty_db,
    make_metrics_db,
    make_quality_db,
    overlap_matrix,
)

EXPECTED_OVERLAP_PATH = REPO_ROOT / "tests" / "fixtures" / "expected_overlap.json"


@pytest.fixture(scope="module")
def synthetic_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_synthetic_db(tmp_path_factory.mktemp("synthetic"))


@pytest.fixture(scope="module")
def quality_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_quality_db(tmp_path_factory.mktemp("quality") / "etfh.duckdb")


@pytest.fixture(scope="module")
def metrics_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_metrics_db(tmp_path_factory.mktemp("metrics") / "etfh.duckdb")


def _run_app(monkeypatch, db_path: Path) -> AppTest:
    monkeypatch.setenv("ETFH_DB", str(db_path))
    app = AppTest.from_file(str(APP_PATH), default_timeout=30)
    return app.run()


@contextmanager
def _app_module() -> Iterator[ModuleType]:
    with install_fake_analytics():
        sys.modules.pop("etfh.dashboard.app", None)
        try:
            yield importlib.import_module("etfh.dashboard.app")
        finally:
            sys.modules.pop("etfh.dashboard.app", None)


def test_renders_with_fake_analytics(tmp_path, monkeypatch) -> None:
    db = make_empty_db(tmp_path / "etfh.duckdb")
    with install_fake_analytics():
        app = _run_app(monkeypatch, db)

    assert not app.exception
    assert app.title[0].value == "Canadian ETF holdings"
    assert app.selectbox(key="fund").value == "SYNA"
    assert app.date_input(key="asof").value == AS_OF
    assert app.selectbox(key="overlap_fund_a").value == "SYNA"
    assert app.selectbox(key="overlap_fund_b").value == "SYNB"
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Holdings"] == "2"
    assert metrics["Top 10 weight"] == "100.00%"
    assert metrics["HHI"] == "0.5200"
    assert metrics["Overlap"] == "50.00%"
    assert metrics["NAV per unit"] == "25.5000"
    assert metrics["Shares outstanding"] == "1,040,000"
    assert metrics["AUM"] == "26,520,000 CAD"
    assert len(app.get("vega_lite_chart")) == 4
    assert len(list(app.dataframe)) == 4


def test_partial_fund_labelled_top_holdings_only(tmp_path, monkeypatch) -> None:
    db = make_empty_db(tmp_path / "etfh.duckdb")
    with install_fake_analytics():
        app = _run_app(monkeypatch, db)
        app.selectbox(key="fund").select("SYNC").run()

    assert not app.exception
    assert any("Top holdings only" in info.value for info in app.info)


def test_missing_database_shows_clear_message(tmp_path, monkeypatch) -> None:
    missing = tmp_path / "does-not-exist" / "etfh.duckdb"
    with install_fake_analytics():
        app = _run_app(monkeypatch, missing)

    assert not app.exception
    errors = [error.value for error in app.error]
    assert len(errors) == 1
    assert "Database not found" in errors[0]
    assert str(missing) in errors[0]


def test_heatmap_apptest_with_three_funds(tmp_path, monkeypatch) -> None:
    db = make_empty_db(tmp_path / "etfh.duckdb")
    with install_fake_analytics():
        app = _run_app(monkeypatch, db)
        app.multiselect(key="heatmap_funds").set_value(["SYNA", "SYNB", "SYNC"]).run()

    assert not app.exception
    assert app.multiselect(key="heatmap_funds").value == ["SYNA", "SYNB", "SYNC"]
    charts = app.get("vega_lite_chart")
    assert len(charts) == 4


def test_heatmap_chart_layers_three_funds() -> None:
    with _app_module() as module:
        matrix = overlap_matrix(None, ["SYNA", "SYNB", "SYNC"], AS_OF)
        spec = module.heatmap_chart(matrix).to_dict()

    values = spec["datasets"][spec["data"]["name"]]
    assert len(values) == 9
    assert {row["fund_a"] for row in values} == {"SYNA", "SYNB", "SYNC"}
    assert {row["fund_b"] for row in values} == {"SYNA", "SYNB", "SYNC"}
    marks = [layer["mark"]["type"] for layer in spec["layer"]]
    assert marks == ["rect", "text"]


def test_breakdown_chart_sorts_by_weight() -> None:
    with _app_module() as module:
        data = module.analytics.breakdown(None, "SYNA", AS_OF, "sector")
        spec = module.breakdown_chart(data, "Sector").to_dict()

    assert spec["encoding"]["y"]["sort"] == "-x"
    assert spec["encoding"]["x"]["type"] == "quantitative"


def test_db_path_default_and_env(monkeypatch) -> None:
    with _app_module() as module:
        monkeypatch.delenv("ETFH_DB", raising=False)
        assert module.db_path() == Path("./build/etfh.duckdb")
        monkeypatch.setenv("ETFH_DB", "/tmp/etfh-test.duckdb")
        assert module.db_path() == Path("/tmp/etfh-test.duckdb")


ANALYTICS_FUNCTIONS = (
    "funds_available",
    "holdings_asof",
    "concentration",
    "breakdown",
    "overlap",
    "overlap_matrix",
    "changes",
    "metrics_asof",
    "flows",
)
QA_FUNCTIONS = ("summarize", "unmatched_shares")


def test_app_only_calls_the_analytics_and_qa_contract(tmp_path, monkeypatch) -> None:
    analytics_calls: list[str] = []
    qa_calls: list[str] = []
    db = make_empty_db(tmp_path / "etfh.duckdb")
    with install_fake_analytics() as fake:
        for name in ANALYTICS_FUNCTIONS:
            original = getattr(fake, name)

            def wrapper(*args, _name=name, _original=original, **kwargs):
                analytics_calls.append(_name)
                return _original(*args, **kwargs)

            setattr(fake, name, wrapper)
        from etfh import qa

        for name in QA_FUNCTIONS:
            original_qa = getattr(qa, name)

            def qa_wrapper(*args, _name=name, _original=original_qa, **kwargs):
                qa_calls.append(_name)
                return _original(*args, **kwargs)

            monkeypatch.setattr(qa, name, qa_wrapper)
        app = _run_app(monkeypatch, db)

    assert not app.exception
    assert set(analytics_calls) == set(ANALYTICS_FUNCTIONS)
    assert set(qa_calls) == set(QA_FUNCTIONS)


def test_dashboard_has_no_sql_calls() -> None:
    for path in sorted((REPO_ROOT / "etfh" / "dashboard").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for forbidden in (".execute(", ".sql(", "duckdb.sql", "read_only=False"):
            assert forbidden not in source, f"{forbidden} found in {path.name}"
    assert "read_only=True" in APP_PATH.read_text(encoding="utf-8")


def test_asof_picker_defaults_to_latest_available(tmp_path, monkeypatch) -> None:
    db = make_empty_db(tmp_path / "etfh.duckdb")
    with install_fake_analytics():
        app = _run_app(monkeypatch, db)
        assert app.date_input(key="asof").value == AS_OF
        app.selectbox(key="fund").select("SYNC").run()
        assert app.date_input(key="asof").value == AS_OF
        app.date_input(key="asof").set_value(date(2025, 1, 2)).run()

    assert not app.exception
    assert app.date_input(key="asof").value == date(2025, 1, 2)


def test_apptest_against_real_synthetic_build(monkeypatch, synthetic_db) -> None:
    monkeypatch.setenv("ETFH_DB", str(synthetic_db))
    app = AppTest.from_file(str(APP_PATH), default_timeout=60)
    app.run()
    assert not app.exception
    assert app.selectbox(key="fund").options == ["SYNA", "SYNB", "SYNE", "SYNF"]

    app.date_input(key="asof").set_value(AS_OF).run()
    app.selectbox(key="overlap_fund_a").select("SYNA").run()
    app.selectbox(key="overlap_fund_b").select("SYNB").run()
    assert not app.exception

    expected = json.loads(EXPECTED_OVERLAP_PATH.read_text(encoding="utf-8"))
    assert expected["fund_a"] == "SYNA"
    assert expected["fund_b"] == "SYNB"
    assert expected["asof"] == AS_OF.isoformat()
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Overlap"] == f"{float(expected['overlap']):.2%}"

    common = list(app.dataframe)[2].value
    rows = sorted(common.to_dict("records"), key=lambda row: row["security_id"])
    wanted = sorted(expected["common"], key=lambda row: row["security_id"])
    assert len(rows) == int(expected["common_count"])
    assert [row["security_id"] for row in rows] == [row["security_id"] for row in wanted]
    for row, want in zip(rows, wanted, strict=True):
        assert round(float(row["weight_a"]), 4) == float(want["weight_a"])
        assert round(float(row["weight_b"]), 4) == float(want["weight_b"])


def test_real_build_labels_partial_funds(monkeypatch, synthetic_db) -> None:
    monkeypatch.setenv("ETFH_DB", str(synthetic_db))
    app = AppTest.from_file(str(APP_PATH), default_timeout=60)
    app.run()
    app.date_input(key="asof").set_value(AS_OF).run()
    app.selectbox(key="fund").select("SYNF").run()

    assert not app.exception
    assert any("Top holdings only" in info.value for info in app.info)
    assert any(
        "SYNF" in subheader.value and "2026-09-30" in subheader.value for subheader in app.subheader
    )


def _run_quality_app(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, asof: date = QUALITY_AS_OF
) -> AppTest:
    """Run the app against the quality database with a pinned as-of date."""
    monkeypatch.setenv("ETFH_DB", str(db_path))
    app = AppTest.from_file(str(APP_PATH), default_timeout=60)
    app.run()
    app.date_input(key="asof").set_value(asof).run()
    return app


def _metric_values(app: AppTest) -> dict[str, str]:
    return {metric.label: metric.value for metric in app.metric}


@pytest.fixture
def quality_con(quality_db: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    con = duckdb.connect(str(quality_db), read_only=True)
    try:
        yield con
    finally:
        con.close()


def test_fund_quality_reports_kind_partial_and_share(quality_con) -> None:
    state = quality.fund_quality(quality_con, "HIGH", QUALITY_AS_OF)
    assert state.as_of_date == QUALITY_AS_OF
    assert (state.source_kind, state.source_format) == ("daily_holdings", "csv")
    assert state.source_label == "Daily holdings (CSV)"
    assert state.partial is False
    assert state.coverage_label == "Full portfolio"
    assert state.unmatched_share == pytest.approx(0.60)
    assert state.unmatched_weight == pytest.approx(0.60)
    assert state.total_weight == pytest.approx(1.0)
    assert state.excluded_files == ()
    assert state.understates_overlap is True


def test_fund_quality_marks_quarterly_disclosure_partial(quality_con) -> None:
    state = quality.fund_quality(quality_con, "QTR", QUALITY_AS_OF)
    assert state.as_of_date == date(2026, 9, 30)
    assert state.source_label == "Quarterly disclosure (PDF)"
    assert state.partial is True
    assert state.coverage_label == "Top holdings only"
    assert state.unmatched_share == pytest.approx(0.50)


def test_fund_quality_lists_excluded_files(quality_con) -> None:
    state = quality.fund_quality(quality_con, "EXC", QUALITY_AS_OF)
    assert state.as_of_date == date(2026, 10, 30)
    assert state.unmatched_share == pytest.approx(0.0)
    assert len(state.excluded_files) == 1
    excluded = state.excluded_files[0]
    assert excluded.as_of_date == QUALITY_AS_OF
    assert excluded.source_id == "delta_daily"
    assert excluded.checks == ("weight_sum",)
    assert excluded.reason == "weight_sum"
    assert excluded.as_of_label == "2026-11-02"


def test_fund_quality_without_a_known_file(quality_con) -> None:
    state = quality.fund_quality(quality_con, "LATE", QUALITY_AS_OF)
    assert state.as_of_date is None
    assert state.source_id is None
    assert state.source_label == "Unknown"
    assert state.unmatched_share is None
    assert state.coverage_label == "Full portfolio"
    assert state.excluded_files == ()


def test_understated_note_threshold_is_strict() -> None:
    def state(fund: str, share: float | None) -> quality.FundQuality:
        return quality.FundQuality(
            fund=fund,
            asof=QUALITY_AS_OF,
            as_of_date=QUALITY_AS_OF,
            source_id="test",
            source_kind="daily_holdings",
            source_format="csv",
            partial=False,
            unmatched_share=share,
            unmatched_weight=share,
            total_weight=1.0,
        )

    assert quality.understated_note([state("AAA", 0.20)]) is None
    assert quality.understated_note([state("AAA", None)]) is None
    note = quality.understated_note([state("AAA", 0.21), state("BBB", 0.05)])
    assert note is not None
    assert "AAA has 21.00% of its weight unmatched" in note
    assert "BBB" not in note


def test_source_metadata_without_sources_table(tmp_path: Path) -> None:
    path = make_empty_db(tmp_path / "empty.duckdb")
    con = duckdb.connect(str(path), read_only=True)
    try:
        assert quality.source_metadata(con) == {}
    finally:
        con.close()


def test_quality_panel_apptest(monkeypatch, quality_db: Path) -> None:
    monkeypatch.setenv("ETFH_DUCKDB_THREADS", "1")
    app = _run_quality_app(monkeypatch, quality_db)
    app.selectbox(key="fund").select("HIGH").run()

    assert not app.exception
    assert app.selectbox(key="fund").options == QUALITY_FUNDS
    metrics = _metric_values(app)
    assert metrics["Portfolio date"] == "2026-11-02"
    assert metrics["Source"] == "Daily holdings (CSV)"
    assert metrics["Coverage"] == "Full portfolio"
    assert metrics["Unmatched weight share"] == "60.00%"
    assert any("No excluded files for this fund" in caption.value for caption in app.caption)


def test_quality_panel_lists_excluded_files_apptest(monkeypatch, quality_db: Path) -> None:
    app = _run_quality_app(monkeypatch, quality_db)
    app.selectbox(key="fund").select("EXC").run()

    assert not app.exception
    metrics = _metric_values(app)
    assert metrics["Portfolio date"] == "2026-10-30"
    warnings = [warning.value for warning in app.warning]
    assert any(
        "Excluded file: delta_daily as of 2026-11-02 (weight_sum)" in warning
        for warning in warnings
    )


def test_quality_panel_quarterly_fund_apptest(monkeypatch, quality_db: Path) -> None:
    app = _run_quality_app(monkeypatch, quality_db)
    app.selectbox(key="fund").select("QTR").run()

    assert not app.exception
    metrics = _metric_values(app)
    assert metrics["Portfolio date"] == "2026-09-30"
    assert metrics["Source"] == "Quarterly disclosure (PDF)"
    assert metrics["Coverage"] == "Top holdings only"
    assert metrics["Unmatched weight share"] == "50.00%"
    assert any("Top holdings only" in info.value for info in app.info)


def test_quality_panel_without_data_apptest(monkeypatch, quality_db: Path) -> None:
    app = _run_quality_app(monkeypatch, quality_db)
    app.selectbox(key="fund").select("LATE").run()

    assert not app.exception
    metrics = _metric_values(app)
    assert metrics["Portfolio date"] == "-"
    assert metrics["Source"] == "Unknown"
    assert metrics["Unmatched weight share"] == "n/a"
    assert any("No included file is known" in caption.value for caption in app.caption)


def test_overlap_warns_when_unmatched_weight_is_high(monkeypatch, quality_db: Path) -> None:
    app = _run_quality_app(monkeypatch, quality_db)
    app.selectbox(key="overlap_fund_a").select("HIGH").run()
    app.selectbox(key="overlap_fund_b").select("LOW").run()

    assert not app.exception
    warnings = [warning.value for warning in app.warning]
    assert any("Overlap is understated: HIGH has 60.00%" in warning for warning in warnings)


def test_overlap_has_no_note_when_unmatched_weight_is_low(monkeypatch, quality_db: Path) -> None:
    app = _run_quality_app(monkeypatch, quality_db)
    app.selectbox(key="overlap_fund_a").select("LOW").run()
    app.selectbox(key="overlap_fund_b").select("EXC").run()
    app.multiselect(key="heatmap_funds").set_value(["LOW", "EXC", "LATE"]).run()

    assert not app.exception
    assert not any("Overlap is understated" in warning.value for warning in app.warning)


def test_heatmap_warns_when_unmatched_weight_is_high(monkeypatch, quality_db: Path) -> None:
    app = _run_quality_app(monkeypatch, quality_db)
    app.multiselect(key="heatmap_funds").set_value(["HIGH", "LOW"]).run()

    assert not app.exception
    assert any(
        "Overlap is understated: HIGH has 60.00%" in warning.value for warning in app.warning
    )
    assert len(app.get("vega_lite_chart")) == 3


def test_open_database_honours_thread_limit(tmp_path: Path, monkeypatch) -> None:
    db = make_empty_db(tmp_path / "etfh.duckdb")
    monkeypatch.setenv("ETFH_DUCKDB_THREADS", "2")
    with _app_module() as module:
        con = module.open_database(db)
        try:
            assert con.execute("SELECT current_setting('threads')").fetchone() == (2,)
        finally:
            con.close()


def test_open_database_rejects_invalid_thread_limit(tmp_path: Path, monkeypatch) -> None:
    db = make_empty_db(tmp_path / "etfh.duckdb")
    monkeypatch.setenv("ETFH_DUCKDB_THREADS", "0")
    with _app_module() as module, pytest.raises(ValueError, match="ETFH_DUCKDB_THREADS"):
        module.open_database(db)


def test_real_build_quality_panel(monkeypatch, synthetic_db: Path) -> None:
    app = _run_quality_app(monkeypatch, synthetic_db, asof=AS_OF)
    app.selectbox(key="fund").select("SYNF").run()

    assert not app.exception
    metrics = _metric_values(app)
    assert metrics["Portfolio date"] == "2026-09-30"
    assert metrics["Source"] == "Synthetic (PDF)"
    assert metrics["Coverage"] == "Top holdings only"
    assert metrics["Unmatched weight share"] == "100.00%"

    app.selectbox(key="fund").select("SYNA").run()
    assert not app.exception
    metrics = _metric_values(app)
    assert metrics["Source"] == "Synthetic (CSV)"
    assert metrics["Unmatched weight share"] == "0.00%"


def test_real_build_overlap_note(monkeypatch, synthetic_db: Path) -> None:
    app = _run_quality_app(monkeypatch, synthetic_db, asof=AS_OF)
    app.selectbox(key="overlap_fund_a").select("SYNF").run()
    app.selectbox(key="overlap_fund_b").select("SYNA").run()

    assert not app.exception
    warnings = [warning.value for warning in app.warning]
    assert any("Overlap is understated: SYNF has 100.00%" in warning for warning in warnings)


@pytest.fixture
def metrics_con(metrics_db: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    con = duckdb.connect(str(metrics_db), read_only=True)
    try:
        yield con
    finally:
        con.close()


def test_fund_metrics_view_reports_latest_and_flows(metrics_con) -> None:
    view = quality.fund_metrics(metrics_con, "FLOW", METRICS_DB_AS_OF)
    assert view.has_metrics is True
    assert view.n_dates == 2
    assert view.latest == quality.MetricsPoint(
        as_of_date=METRICS_DB_AS_OF,
        nav_per_unit=25.5,
        shares_outstanding=1_050_000.0,
        aum=26_775_000.0,
        currency="CAD",
    )
    assert view.staleness_days == 0
    assert view.staleness_label == "same day as the selected date"
    assert view.is_stale is False
    assert view.can_estimate_flows is True
    assert view.method.startswith("Δshares × NAV")
    estimates = view.estimate_rows["flow_estimate"].to_list()
    assert estimates == [pytest.approx(1_275_000.0)]


def test_fund_metrics_view_handles_a_fund_without_metrics(metrics_con) -> None:
    view = quality.fund_metrics(metrics_con, "HOLD", METRICS_DB_AS_OF)
    assert view.has_metrics is False
    assert view.latest is None
    assert view.staleness_days is None
    assert view.can_estimate_flows is False
    assert view.flows.is_empty()


def test_fund_metrics_view_handles_a_database_without_metrics_table(tmp_path: Path) -> None:
    path = make_empty_db(tmp_path / "empty.duckdb")
    con = duckdb.connect(str(path), read_only=True)
    try:
        view = quality.fund_metrics(con, "FLOW", METRICS_DB_AS_OF)
    finally:
        con.close()
    assert view.has_metrics is False


def test_metrics_panel_shows_figures_flows_and_method(monkeypatch, metrics_db: Path) -> None:
    app = _run_quality_app(monkeypatch, metrics_db, asof=METRICS_DB_AS_OF)
    app.selectbox(key="fund").select("FLOW").run()

    assert not app.exception
    metrics = _metric_values(app)
    assert metrics["NAV per unit"] == "25.5000"
    assert metrics["Shares outstanding"] == "1,050,000"
    assert metrics["AUM"] == "26,775,000 CAD"
    captions = [caption.value for caption in app.caption]
    assert any(
        "Latest figures as of 2026-10-22; same day as the selected date." in caption
        for caption in captions
    )
    assert any("Method: Δshares × NAV" in caption for caption in captions)
    flows = [frame.value for frame in app.dataframe if "Estimated flow" in frame.value.columns]
    assert len(flows) == 1
    estimates = flows[0]["Estimated flow"].to_list()
    assert math.isnan(estimates[0])
    assert estimates[1] == pytest.approx(1_275_000.0)
    assert len(app.get("vega_lite_chart")) == 4


def test_metrics_panel_explains_a_single_metrics_date(monkeypatch, metrics_db: Path) -> None:
    app = _run_quality_app(monkeypatch, metrics_db, asof=METRICS_DB_AS_OF)
    app.selectbox(key="fund").select("ONE").run()

    assert not app.exception
    captions = [caption.value for caption in app.caption]
    assert any(
        "Latest figures as of 2026-09-01; 51 days before the selected date." in caption
        for caption in captions
    )
    assert any("only one date is known as of this date" in caption for caption in captions)
    warnings = [warning.value for warning in app.warning]
    assert any("Fund metrics are 51 days old" in warning for warning in warnings)
    assert not [frame for frame in app.dataframe if "Estimated flow" in frame.value.columns]


def test_metrics_panel_explains_missing_metrics(monkeypatch, metrics_db: Path) -> None:
    app = _run_quality_app(monkeypatch, metrics_db, asof=METRICS_DB_AS_OF)
    app.selectbox(key="fund").select("HOLD").run()

    assert not app.exception
    captions = [caption.value for caption in app.caption]
    assert "No fund metrics for HOLD yet." in captions
    assert not any("Method:" in caption for caption in captions)
    assert not [frame for frame in app.dataframe if "Estimated flow" in frame.value.columns]


def test_metrics_qa_panel_lists_a_funds_excluded_metrics_input(
    monkeypatch, metrics_db: Path
) -> None:
    app = _run_quality_app(monkeypatch, metrics_db, asof=METRICS_DB_AS_OF)
    app.selectbox(key="fund").select("BAD").run()

    assert not app.exception
    warnings = [warning.value for warning in app.warning]
    assert any(
        "Fund metrics error nav_positive: nav_per_unit=-1.0000 must be > 0 (bad_metrics)" in warning
        for warning in warnings
    )
    captions = [caption.value for caption in app.caption]
    assert any(
        "Fund-metrics findings: 1 error(s), 1 warning(s), 0 info(s) "
        "across all fund-metrics inputs." in caption
        for caption in captions
    )


def test_metrics_qa_panel_reports_store_wide_counts_for_a_clean_fund(
    monkeypatch, metrics_db: Path
) -> None:
    app = _run_quality_app(monkeypatch, metrics_db, asof=METRICS_DB_AS_OF)
    app.selectbox(key="fund").select("FLOW").run()

    assert not app.exception
    warnings = [warning.value for warning in app.warning]
    assert not any("Fund metrics error" in warning for warning in warnings)
    captions = [caption.value for caption in app.caption]
    assert any(
        "Fund-metrics findings: 1 error(s), 1 warning(s), 0 info(s) "
        "across all fund-metrics inputs." in caption
        for caption in captions
    )


def test_metrics_panel_apptest_lists_every_metrics_fund(monkeypatch, metrics_db: Path) -> None:
    app = _run_quality_app(monkeypatch, metrics_db, asof=METRICS_DB_AS_OF)

    assert not app.exception
    assert app.selectbox(key="fund").options == METRICS_DB_FUNDS
