"""Tests for the dashboard's "This week" tab (FR-3.1, D79).

The tab renders the weekly digest document (:mod:`etfh.digest`) for the week
that covers the sidebar's as-of date. The table builders are pure functions
over a document, so most tests need no Streamlit runtime; the AppTest cases
drive the real widget tree against the committed synthetic store and the demo
build.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest
from streamlit.testing.v1 import AppTest

from etfh.dashboard import demo, format, weekly
from tests._dashboard_helpers import (
    APP_PATH,
    AS_OF,
    REPO_ROOT,
    build_synthetic_db,
    install_fake_analytics,
    make_empty_db,
)

PUBLIC_DOC_PATH = REPO_ROOT / "tests" / "fixtures" / "digest" / "2026-10-23.json"
GOLDEN_DOC_PATH = REPO_ROOT / "tests" / "fixtures" / "digest_golden" / "2026-10-30.json"
WEEK_ENDING = date(2026, 10, 30)


@pytest.fixture(scope="module")
def synthetic_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_synthetic_db(tmp_path_factory.mktemp("week-synthetic"))


@pytest.fixture(scope="module")
def public_doc() -> dict[str, Any]:
    """A public document with one undisclosed fund, flows and an overlap shift."""
    return json.loads(PUBLIC_DOC_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def golden_doc() -> dict[str, Any]:
    """The committed public digest of the synthetic store for 2026-10-30."""
    return json.loads(GOLDEN_DOC_PATH.read_text(encoding="utf-8"))


def _run_app(monkeypatch: pytest.MonkeyPatch, db_path: Path, asof: date = AS_OF) -> AppTest:
    monkeypatch.setenv("ETFH_DB", str(db_path))
    app = AppTest.from_file(str(APP_PATH), default_timeout=120)
    app.run()
    app.date_input(key="asof").set_value(asof).run()
    return app


def _week_frames(app: AppTest) -> list[Any]:
    """Frames the tab rendered, in render order (funds, changes, overlaps).

    ``AppTest`` exposes ``st.dataframe`` input as pandas frames.
    """
    return [
        frame.value
        for frame in app.dataframe
        if set(frame.value.columns) & {"Turnover", "Weight before", "Overlap start"}
    ]


def test_week_end_is_the_most_recent_friday() -> None:
    assert weekly.week_end(date(2026, 10, 28)) == date(2026, 10, 23)  # Wednesday
    assert weekly.week_end(date(2026, 10, 29)) == date(2026, 10, 23)  # Thursday
    assert weekly.week_end(date(2026, 10, 30)) == date(2026, 10, 30)  # Friday
    assert weekly.week_end(date(2026, 10, 31)) == date(2026, 10, 30)  # Saturday


def test_config_dir_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ETFH_CONFIG_DIR", raising=False)
    assert weekly.config_dir() == Path("config")
    monkeypatch.setenv("ETFH_CONFIG_DIR", "/tmp/etfh-config")
    assert weekly.config_dir() == Path("/tmp/etfh-config")


def test_format_helpers_keep_a_missing_value_and_a_zero_unsigned() -> None:
    assert format.percent(None) == "-"
    assert format.percent(-0.0) == "0.00%"
    assert format.signed_percent(None) == "-"
    assert format.signed_percent(0.0) == "0.00%"
    assert format.signed_percent(-0.0) == "0.00%"
    assert format.signed_percent(-0.0127) == "-1.27%"
    assert format.signed_money(0.0) == "0"
    assert format.signed_money(-340_000.4) == "-340,000"
    assert format.format_count(None) == "-"
    assert format.format_count(1_234) == "1,234"


def test_fund_table_formats_like_the_other_tabs(public_doc: dict[str, Any]) -> None:
    table = weekly.fund_table(public_doc)

    assert table.columns == [
        "Fund",
        "Name",
        "Status",
        "Adds",
        "Drops",
        "Weight changes",
        "Turnover",
        "Top 10 weight",
        "Estimated flow (CAD)",
    ]
    rows = {row["Fund"]: row for row in table.to_dicts()}
    assert list(rows) == ["RLEQ", "SYNA"]
    assert rows["RLEQ"]["Status"] == "OK"
    assert (rows["RLEQ"]["Adds"], rows["RLEQ"]["Drops"]) == ("3", "1")
    assert rows["RLEQ"]["Weight changes"] == "9"
    assert rows["RLEQ"]["Turnover"] == "2.07%"
    assert rows["RLEQ"]["Top 10 weight"] == "39.50%"
    assert rows["RLEQ"]["Estimated flow (CAD)"] == "+2,500,000"
    assert rows["RLEQ"]["Name"] == "Riverline Canadian Equity Fund"
    assert rows["SYNA"]["Estimated flow (CAD)"] == "-120,000"


def test_fund_table_uses_the_documents_values(golden_doc: dict[str, Any]) -> None:
    table = weekly.fund_table(golden_doc)

    assert table["Estimated flow (CAD)"].to_list() == ["-", "-", "-"]
    assert table["Status"].to_list() == ["New in window"] * 3
    assert table["Turnover"].to_list() == ["2.23%", "1.03%", "1.41%"]


def test_fund_table_handles_a_week_without_funds() -> None:
    document = {
        "funds": [],
        "overlap_shifts": [],
        "quality": {"files_included": 0, "files_excluded": 0, "funds_without_data": []},
    }
    table = weekly.fund_table(document)

    assert table.is_empty()
    assert table.columns == [
        "Fund",
        "Name",
        "Status",
        "Adds",
        "Drops",
        "Weight changes",
        "Turnover",
        "Top 10 weight",
        "Estimated flow (CAD)",
    ]


def test_changes_table_hides_undisclosed_funds(public_doc: dict[str, Any]) -> None:
    table = weekly.changes_table(public_doc)

    assert table["Fund"].to_list() == ["SYNA", "SYNA"]
    assert table["Change"].to_list() == ["Add", "Weight change"]
    assert table["Security"].to_list() == ["Granite Grid Ltd.", "Arctic Aluminum Corp."]
    assert table["Security id"].to_list() == ["TKR:XTSE:GG", "TKR:XTSE:AA"]
    assert table["Weight before"].to_list() == ["0.00%", "0.90%"]
    assert table["Weight after"].to_list() == ["0.70%", "1.20%"]
    assert table["Delta"].to_list() == ["+0.70%", "+0.30%"]
    assert "RLEQ" not in table["Fund"].to_list()


def test_changes_table_marks_a_missing_identifier(public_doc: dict[str, Any]) -> None:
    document = copy.deepcopy(public_doc)
    document["funds"][1]["changes"][0]["security_id"] = None

    table = weekly.changes_table(document)

    assert table["Security id"].to_list()[0] == "-"


def test_changes_table_without_disclosed_lines(public_doc: dict[str, Any]) -> None:
    document = copy.deepcopy(public_doc)
    for fund in document["funds"]:
        fund["changes"] = []

    assert weekly.changes_table(document).is_empty()


def test_overlap_table_formats_shifts(public_doc: dict[str, Any]) -> None:
    table = weekly.overlap_table(public_doc)

    assert table.to_dicts() == [
        {
            "Fund A": "SYNA",
            "Fund B": "RLEQ",
            "Overlap start": "19.02%",
            "Overlap end": "20.44%",
            "Delta": "+1.42%",
        }
    ]


def test_overlap_table_is_empty_without_shifts(golden_doc: dict[str, Any]) -> None:
    table = weekly.overlap_table(golden_doc)

    assert table.is_empty()
    assert table.columns == ["Fund A", "Fund B", "Overlap start", "Overlap end", "Delta"]


def test_undisclosed_funds_are_listed_by_ticker(public_doc: dict[str, Any]) -> None:
    assert weekly.undisclosed_funds(public_doc) == ["RLEQ"]
    assert weekly.undisclosed_funds(json.loads(GOLDEN_DOC_PATH.read_text())) == []


def test_quality_label_counts_files_and_funds(public_doc: dict[str, Any]) -> None:
    assert weekly.quality_label(public_doc) == (
        "Files included: 12; excluded: 1. Funds without a comparable portfolio this week: SYNC."
    )


def test_quality_label_says_none_without_missing_funds(public_doc: dict[str, Any]) -> None:
    document = copy.deepcopy(public_doc)
    document["quality"]["funds_without_data"] = []

    assert weekly.quality_label(document).endswith(
        "Funds without a comparable portfolio this week: none."
    )


def test_digest_for_week_matches_the_committed_golden(synthetic_db: Path) -> None:
    document = weekly.digest_for_week(
        synthetic_db,
        WEEK_ENDING,
        public=True,
        config_dir=REPO_ROOT / "config",
    )

    assert document == json.loads(GOLDEN_DOC_PATH.read_text(encoding="utf-8"))
    assert document["demo"] is False


def test_digest_for_week_marks_the_demo_document(synthetic_db: Path) -> None:
    document = weekly.digest_for_week(
        synthetic_db,
        WEEK_ENDING,
        public=True,
        config_dir=REPO_ROOT / "config",
        demo=True,
    )

    expected = json.loads(GOLDEN_DOC_PATH.read_text(encoding="utf-8"))
    expected["demo"] = True
    assert document == expected


def test_digest_for_week_needs_the_build_tables(tmp_path: Path) -> None:
    empty = make_empty_db(tmp_path / "empty.duckdb")

    with pytest.raises(duckdb.Error):
        weekly.digest_for_week(
            empty,
            WEEK_ENDING,
            public=False,
            config_dir=REPO_ROOT / "config",
        )


def test_digest_for_week_needs_the_config(tmp_path: Path, synthetic_db: Path) -> None:
    with pytest.raises(FileNotFoundError):
        weekly.digest_for_week(
            synthetic_db,
            WEEK_ENDING,
            public=True,
            config_dir=tmp_path / "missing",
        )


def test_week_tab_renders_the_synthetic_week(
    monkeypatch: pytest.MonkeyPatch, synthetic_db: Path
) -> None:
    app = _run_app(monkeypatch, synthetic_db)

    assert not app.exception
    assert "Week ending 2026-10-30" in [subheader.value for subheader in app.subheader]
    assert any(
        "Week 2026-10-23 to 2026-10-30; portfolios known by 2026-10-31T04:00:00+00:00."
        in caption.value
        for caption in app.caption
    )
    funds, changes = _week_frames(app)[:2]
    assert funds["Fund"].tolist() == ["SYNA", "SYNB", "SYNE"]
    assert len(changes) == 11
    assert changes["Delta"].str.startswith(("+", "-", "0")).all()
    captions = [caption.value for caption in app.caption]
    assert any(
        "Files included: 13; excluded: 2. "
        "Funds without a comparable portfolio this week: SYNC, SYND, SYNF." in caption
        for caption in captions
    )
    assert "No fund pair moved its overlap by the reporting threshold this week." in captions


def test_week_tab_follows_the_selected_date(
    monkeypatch: pytest.MonkeyPatch, synthetic_db: Path
) -> None:
    app = _run_app(monkeypatch, synthetic_db, asof=date(2026, 11, 1))

    assert not app.exception
    assert "Week ending 2026-10-30" in [subheader.value for subheader in app.subheader]

    app.date_input(key="asof").set_value(date(2026, 10, 28)).run()

    assert not app.exception
    assert "Week ending 2026-10-23" in [subheader.value for subheader in app.subheader]
    assert any(
        "No fund has a comparable portfolio for this week yet." in caption.value
        for caption in app.caption
    )


def test_week_tab_reports_a_database_without_a_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = make_empty_db(tmp_path / "etfh.duckdb")
    with install_fake_analytics():
        app = _run_app(monkeypatch, db)

    assert not app.exception
    assert any(
        "No weekly digest is available in this database yet" in caption.value
        for caption in app.caption
    )


def test_week_tab_renders_the_demo_document(monkeypatch: pytest.MonkeyPatch) -> None:
    demo.demo_database.clear()
    monkeypatch.delenv("ETFH_DEMO", raising=False)
    app = AppTest.from_file(str(REPO_ROOT / "streamlit_app.py"), default_timeout=180).run()

    assert not app.exception
    assert "Week ending 2026-10-30" in [subheader.value for subheader in app.subheader]
    funds, changes = _week_frames(app)[:2]
    assert funds["Fund"].tolist() == ["SYNA", "SYNB", "SYNE"]
    assert sorted(changes["Fund"].unique().tolist()) == ["SYNA", "SYNB", "SYNE"]
    assert app.info[0].value.startswith("Demo data — synthetic funds")


def test_public_policy_follows_the_deployment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, public_doc: dict[str, Any]
) -> None:
    calls: list[tuple[date, bool, bool, str]] = []

    def fake_digest(
        db_file: Path,
        end: date,
        *,
        public: bool,
        config_dir: Path,
        demo: bool = False,
    ) -> dict[str, Any]:
        calls.append((end, public, demo, Path(config_dir).name))
        return copy.deepcopy(public_doc)

    monkeypatch.setattr(weekly, "digest_for_week", fake_digest)
    db = make_empty_db(tmp_path / "etfh.duckdb")
    with install_fake_analytics():
        app = _run_app(monkeypatch, db)
        assert not app.exception
        app.run()
        assert not app.exception

    assert calls == [(date(2026, 10, 30), False, False, "config")]
    assert any(
        "Security lines are hidden" in caption.value and "RLEQ" in caption.value
        for caption in app.caption
    )

    demo.demo_database.clear()
    calls.clear()
    monkeypatch.setenv("ETFH_DEMO", "1")
    with install_fake_analytics():
        demo_app = AppTest.from_file(str(APP_PATH), default_timeout=180).run()

    assert not demo_app.exception
    assert calls == [(date(2026, 10, 30), True, True, "config")]
