"""Tests for the Streamlit dashboard (``etfh/dashboard/app.py``)."""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest
from streamlit.testing.v1 import AppTest

from tests._dashboard_helpers import (
    APP_PATH,
    AS_OF,
    REPO_ROOT,
    build_synthetic_db,
    install_fake_analytics,
    make_empty_db,
    overlap_matrix,
)

EXPECTED_OVERLAP_PATH = REPO_ROOT / "tests" / "fixtures" / "expected_overlap.json"


@pytest.fixture(scope="module")
def synthetic_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_synthetic_db(tmp_path_factory.mktemp("synthetic"))


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
    assert len(app.get("vega_lite_chart")) == 3
    assert len(list(app.dataframe)) == 3


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
    assert len(charts) == 3


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
)


def test_app_only_calls_the_analytics_contract(tmp_path, monkeypatch) -> None:
    calls: list[str] = []
    db = make_empty_db(tmp_path / "etfh.duckdb")
    with install_fake_analytics() as fake:
        for name in ANALYTICS_FUNCTIONS:
            original = getattr(fake, name)

            def wrapper(*args, _name=name, _original=original, **kwargs):
                calls.append(_name)
                return _original(*args, **kwargs)

            setattr(fake, name, wrapper)
        app = _run_app(monkeypatch, db)

    assert not app.exception
    assert set(calls) == set(ANALYTICS_FUNCTIONS)


def test_app_has_no_sql_calls() -> None:
    source = APP_PATH.read_text(encoding="utf-8")
    for forbidden in (".execute(", ".sql(", "duckdb.sql", "read_only=False"):
        assert forbidden not in source
    assert "read_only=True" in source


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


def test_apptest_against_real_synthetic_build(tmp_path, monkeypatch, synthetic_db) -> None:
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


def test_real_build_labels_partial_funds(tmp_path, monkeypatch, synthetic_db) -> None:
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
