"""PRD acceptance test for the weekly page (FR-3.1) and the ``etfh weekly`` command.

The PRD's acceptance check for the weekly digest is concrete: run the pipeline
on the committed synthetic store and open the page - the index must carry a
changes table with one row per fund that has data for the week. These tests
drive the real command (BuildResult in a temp directory, ``etfh weekly`` through
Typer's runner) and read the HTML it writes.

``--demo``, ``--weeks``, the empty week and the ``--internal``/``--public``
split (D79) are covered too, because they are the four ways the command can be
wired wrongly while the single-week public page still looks right.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest
from typer.testing import CliRunner

from etfh.build import build
from etfh.cli import app
from etfh.digest import build_digest
from etfh.registry import load_funds, load_sources
from tests._build_helpers import CONFIG_DIR, RAW_ROOT

runner = CliRunner()
#: Wide terminal so Typer's error panel does not truncate the message in CI.
_WIDE = {"COLUMNS": "200"}

#: The synthetic fixtures run to 2026-11-03; the week ending then has three
#: funds with a comparable portfolio (SYNA, SYNB, SYNE).
LATEST_WEEK_END = date(2026, 11, 3)
#: One week earlier: nothing is known by its cutoff yet, so the week is empty.
EMPTY_WEEK_END = date(2026, 10, 27)

_CHANGES_TABLE = re.compile(r'<table id="changes">.*?</table>', re.DOTALL)
_FUND_ROW = re.compile(r'<tr><th scope="row">([A-Z0-9.]+)</th>')


@pytest.fixture(scope="module")
def synthetic_build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out_dir = tmp_path_factory.mktemp("weekly-build")
    result = build(RAW_ROOT, out_dir, config_dir=CONFIG_DIR)
    return result.db_path


def _changes_funds(index_html: str) -> list[str]:
    """The fund tickers of the changes table, in document order."""
    table = _CHANGES_TABLE.search(index_html)
    assert table is not None, 'index.html has no <table id="changes">'
    return _FUND_ROW.findall(table.group(0))


def _run(*args: str) -> Any:
    return runner.invoke(app, ["weekly", *args], env=_WIDE)


def test_index_has_one_changes_row_per_synthetic_fund_with_data(
    synthetic_build: Path, tmp_path: Path
) -> None:
    """The PRD acceptance check: build, run ``etfh weekly``, read the index."""
    out = tmp_path / "site"
    result = _run(
        "--db", str(synthetic_build), "--out", str(out), "--week-end", LATEST_WEEK_END.isoformat()
    )
    assert result.exit_code == 0, result.output
    index = out / "index.html"
    assert index.is_file()
    assert "wrote" in result.output and str(index) in result.output

    html = index.read_text(encoding="utf-8")
    funds = _changes_funds(html)
    sources = load_sources(CONFIG_DIR / "sources.yaml")
    connection = duckdb.connect(str(synthetic_build), read_only=True)
    try:
        expected = build_digest(
            connection,
            LATEST_WEEK_END,
            public=True,
            sources=sources,
            funds=load_funds(CONFIG_DIR / "funds.csv", sources),
        )
    finally:
        connection.close()
    assert funds == [fund["ticker"] for fund in expected["funds"]]
    assert funds == ["SYNA", "SYNB", "SYNE"]
    assert "Derived data · exploratory · not investment advice" in html
    assert "Demo data" not in html
    # The other site files the interfaces promise.
    for relative in (
        "archive.html",
        "feed.xml",
        ".nojekyll",
        f"weeks/{LATEST_WEEK_END.isoformat()}.html",
        f"data/{LATEST_WEEK_END.isoformat()}.json",
    ):
        assert (out / relative).is_file(), relative


def test_the_published_json_is_the_validated_document(tmp_path: Path) -> None:
    out = tmp_path / "site"
    result = _run(
        "--db",
        str(build(RAW_ROOT, tmp_path / "build", config_dir=CONFIG_DIR).db_path),
        "--out",
        str(out),
        "--week-end",
        LATEST_WEEK_END.isoformat(),
    )
    assert result.exit_code == 0, result.output
    document = json.loads((out / "data" / f"{LATEST_WEEK_END.isoformat()}.json").read_text())
    assert document["schema_version"] == 1
    assert document["week_end"] == LATEST_WEEK_END.isoformat()
    assert document["public"] is True
    assert document["demo"] is False
    assert [fund["ticker"] for fund in document["funds"]] == _changes_funds(
        (out / "index.html").read_text(encoding="utf-8")
    )


def test_demo_builds_the_synthetic_store_and_flags_every_document(tmp_path: Path) -> None:
    """``--demo`` ignores ``--db``, builds the fixtures and marks the documents."""
    out = tmp_path / "demo-site"
    result = _run("--demo", "--out", str(out), "--config", str(CONFIG_DIR))
    assert result.exit_code == 0, result.output
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "Demo data" in html
    # The week end is the synthetic data's latest as-of date, not the clock.
    assert (out / "data" / f"{LATEST_WEEK_END.isoformat()}.json").is_file()
    document = json.loads(
        (out / "data" / f"{LATEST_WEEK_END.isoformat()}.json").read_text(encoding="utf-8")
    )
    assert document["demo"] is True
    assert [fund["ticker"] for fund in document["funds"]] == _changes_funds(html)


def test_weeks_two_skips_the_week_without_a_comparable_portfolio(
    synthetic_build: Path, tmp_path: Path
) -> None:
    """Two week ends are requested; only the one with data reaches the site."""
    out = tmp_path / "two-weeks"
    result = _run(
        "--db",
        str(synthetic_build),
        "--out",
        str(out),
        "--week-end",
        LATEST_WEEK_END.isoformat(),
        "--weeks",
        "2",
    )
    assert result.exit_code == 0, result.output
    assert f"skipped week ending {EMPTY_WEEK_END.isoformat()}: no comparable portfolio" in (
        result.output
    )
    pages = sorted(path.name for path in (out / "weeks").glob("*.html"))
    assert pages == [f"{LATEST_WEEK_END.isoformat()}.html"]
    assert "Week ending 2026-10-27" not in (out / "index.html").read_text(encoding="utf-8")


def test_internal_and_public_differ_for_a_non_redistributable_fund(
    synthetic_build: Path, tmp_path: Path
) -> None:
    """A fund whose terms forbid redistribution (D79) discloses only internally.

    The committed synthetic sources permit redistribution, so the scratch config
    relabels one fund's issuer and revokes that permission: the public page must
    keep the fund's counts but drop every security line, while the internal page
    keeps them and says it must not be published.
    """
    config = tmp_path / "config"
    config.mkdir()
    shutil.copy(CONFIG_DIR / "validation.yaml", config / "validation.yaml")
    funds = (CONFIG_DIR / "funds.csv").read_text(encoding="utf-8")
    funds = funds.replace(
        "SYNA,synthetic,Synthetic Aurora Equity Fund", "SYNA,acme,Synthetic Aurora Equity Fund"
    )
    (config / "funds.csv").write_text(funds, encoding="utf-8")
    sources = (CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8")
    sources = sources.replace(
        '      url: "synthetic://fixtures/syn_csv"\n'
        '      reviewer: "synthetic fixture"\n'
        "      reviewed_on: 2026-10-16\n"
        "      decision: allowed\n"
        "      redistribution_allowed: true\n",
        '      url: "synthetic://fixtures/syn_csv"\n'
        '      reviewer: "synthetic fixture"\n'
        "      reviewed_on: 2026-10-16\n"
        "      decision: allowed\n"
        "      redistribution_allowed: false\n",
        1,
    )
    assert "redistribution_allowed: false" in sources
    (config / "sources.yaml").write_text(sources, encoding="utf-8")

    common = [
        "--db",
        str(synthetic_build),
        "--week-end",
        LATEST_WEEK_END.isoformat(),
        "--config",
        str(config),
    ]
    public_out = tmp_path / "public"
    assert _run(*common, "--out", str(public_out)).exit_code == 0
    internal_out = tmp_path / "internal"
    internal = _run(*common, "--out", str(internal_out), "--internal")
    assert internal.exit_code == 0, internal.output
    assert "do not publish" in internal.output

    public_html = (public_out / "index.html").read_text(encoding="utf-8")
    internal_html = (internal_out / "index.html").read_text(encoding="utf-8")
    # Both list the fund: its aggregate counts are publishable.
    assert _changes_funds(public_html) == _changes_funds(internal_html) == ["SYNA", "SYNB", "SYNE"]
    # Only the internal page names SYNA's securities.
    assert "Security changes for SYNA" in internal_html
    assert "Security changes for SYNA" not in public_html
    public_document = json.loads(
        (public_out / "data" / f"{LATEST_WEEK_END.isoformat()}.json").read_text(encoding="utf-8")
    )
    internal_document = json.loads(
        (internal_out / "data" / f"{LATEST_WEEK_END.isoformat()}.json").read_text(encoding="utf-8")
    )
    by_ticker = {fund["ticker"]: fund for fund in public_document["funds"]}
    assert by_ticker["SYNA"]["securities_disclosed"] is False
    assert by_ticker["SYNA"]["changes"] == []
    assert by_ticker["SYNA"]["counts"]["weight_changes"] > 0
    assert by_ticker["SYNB"]["securities_disclosed"] is True
    internal_funds = {fund["ticker"]: fund for fund in internal_document["funds"]}
    assert internal_funds["SYNA"]["securities_disclosed"] is True
    assert internal_funds["SYNA"]["changes"]
    assert internal_document["public"] is False


def test_no_holdings_for_the_requested_week_exits_1(synthetic_build: Path, tmp_path: Path) -> None:
    result = _run(
        "--db", str(synthetic_build), "--out", str(tmp_path / "empty"), "--week-end", "2020-01-03"
    )
    assert result.exit_code == 1, result.output
    assert "error: no holdings for the requested week(s)" in result.output
    assert not (tmp_path / "empty" / "index.html").exists()


def test_missing_database_exits_2(tmp_path: Path) -> None:
    result = _run("--db", str(tmp_path / "nope.duckdb"), "--out", str(tmp_path / "site"))
    assert result.exit_code == 2, result.output
    assert f"error: database not found: {tmp_path / 'nope.duckdb'}" in result.output


def test_weeks_must_be_at_least_one(synthetic_build: Path, tmp_path: Path) -> None:
    result = _run("--db", str(synthetic_build), "--out", str(tmp_path / "site"), "--weeks", "0")
    assert result.exit_code == 2, result.output
    assert "--weeks must be at least 1" in result.output


def test_week_end_must_be_a_date(synthetic_build: Path, tmp_path: Path) -> None:
    result = _run(
        "--db", str(synthetic_build), "--out", str(tmp_path / "site"), "--week-end", "last-week"
    )
    assert result.exit_code == 2, result.output
    assert "is not a date in YYYY-MM-DD form or 'today'" in result.output
