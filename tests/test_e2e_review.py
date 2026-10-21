"""End-to-end proof of the reviewed-name workflow (D54).

One build proposes names for holdings that carry no identifier, ``etfh review
approve`` records only the reviewed ones, and the next build resolves them. The
test uses the real security master, validator and review functions against the
committed synthetic store; only the config (and therefore the overrides file)
is a temporary copy, so nothing reviewed here can reach git.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import duckdb
import polars as pl
import pytest
from typer.testing import CliRunner

from etfh.analytics import overlap
from etfh.build import BuildResult, build
from etfh.cli import app
from etfh.secmaster.overrides import Override
from etfh.secmaster.resolve import SecurityMaster
from tests._build_helpers import CONFIG_DIR, RAW_ROOT

runner = CliRunner()
_ASOF = date(2026, 11, 5)
_OVERRIDE_HEADER = "match_type,match_value,security_id,reviewer,reviewed_on,note"


@pytest.fixture()
def config(tmp_path: Path) -> Path:
    """A temporary copy of the config with an empty reviewed-overrides file."""
    config_dir = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, config_dir)
    (config_dir / "security_overrides.csv").write_text(_OVERRIDE_HEADER + "\n", encoding="utf-8")
    return config_dir


def _build(config: Path, out: Path) -> BuildResult:
    return build(RAW_ROOT, out, config_dir=config)


def _connect(out: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(out / "etfh.duckdb"), read_only=True)


def _null_security_ids(out: Path, fund: str) -> int:
    con = _connect(out)
    try:
        return int(
            con.execute(
                "SELECT count(*) FROM holdings WHERE fund_ticker = ? "
                "AND security_id IS NULL AND lower(asset_class) "
                "NOT IN ('cash', 'fx', 'derivative')",
                [fund],
            ).fetchone()[0]
        )
    finally:
        con.close()


def test_review_workflow_assigns_approved_names_on_the_next_build(
    tmp_path: Path, config: Path
) -> None:
    before = _build(config, tmp_path / "before")
    assert before.n_name_proposals > 0
    proposals = pl.read_csv(tmp_path / "before" / "reports" / "name_proposals.csv")
    exact = proposals.filter(pl.col("confidence") == "exact")
    assert exact.height > 0
    unmatched_before = _null_security_ids(tmp_path / "before", "SYNF")
    assert unmatched_before > 0

    overrides = config / "security_overrides.csv"
    original = overrides.read_bytes()
    dry_run = runner.invoke(
        app,
        [
            "review",
            "approve",
            "--db",
            str(tmp_path / "before" / "etfh.duckdb"),
            "--confidence",
            "exact",
            "--reviewer",
            "end-to-end reviewer",
            "--overrides",
            str(overrides),
            "--dry-run",
        ],
    )
    assert dry_run.exit_code == 0, dry_run.output
    assert "would add" in dry_run.output
    assert overrides.read_bytes() == original

    approved = runner.invoke(
        app,
        [
            "review",
            "approve",
            "--db",
            str(tmp_path / "before" / "etfh.duckdb"),
            "--confidence",
            "exact",
            "--reviewer",
            "end-to-end reviewer",
            "--overrides",
            str(overrides),
        ],
    )
    assert approved.exit_code == 0, approved.output
    reviewed = pl.read_csv(overrides)
    assert reviewed.height == exact.height
    assert set(reviewed["match_type"].to_list()) == {"name"}
    assert set(reviewed["reviewer"].to_list()) == {"end-to-end reviewer"}
    assert all(value.startswith("*|") for value in reviewed["match_value"].to_list())

    after = _build(config, tmp_path / "after")
    unmatched_after = _null_security_ids(tmp_path / "after", "SYNF")
    assert unmatched_after == 0
    assert after.n_unmatched < before.n_unmatched

    before_con = _connect(tmp_path / "before")
    after_con = _connect(tmp_path / "after")
    try:
        overlap_before = overlap(before_con, "SYNA", "SYNF", _ASOF)
        overlap_after = overlap(after_con, "SYNA", "SYNF", _ASOF)
    finally:
        before_con.close()
        after_con.close()
    assert overlap_before.overlap == 0.0
    assert overlap_after.overlap > 0.0
    assert overlap_after.common.height > 0

    # approving a name must never rewrite a row that already has its own id
    con = _connect(tmp_path / "after")
    try:
        con.execute(f"ATTACH '{tmp_path / 'before' / 'etfh.duckdb'}' AS before (READ_ONLY)")
        changed = con.execute(
            """
            SELECT count(*)
            FROM main.holdings a
            JOIN before.holdings b
              ON a.fund_ticker = b.fund_ticker
             AND a.as_of_date = b.as_of_date
             AND a.file_version = b.file_version
             AND a.source_id = b.source_id
             AND a.security_name = b.security_name
             AND a.security_ticker IS NOT DISTINCT FROM b.security_ticker
             AND a.isin IS NOT DISTINCT FROM b.isin
             AND a.cusip IS NOT DISTINCT FROM b.cusip
             AND a.currency IS NOT DISTINCT FROM b.currency
            WHERE b.security_id IS NOT NULL
              AND a.security_id IS DISTINCT FROM b.security_id
            """
        ).fetchone()[0]
    finally:
        con.close()
    assert changed == 0


def test_a_global_name_override_never_rewrites_an_identified_row() -> None:
    """Global name overrides identify name-only rows; identifiers keep precedence.

    A review approval is global by name, and the same name can appear on a row
    that already carries an ISIN or an exchange-qualified ticker. Those rows
    keep their own id (D8, D23), otherwise approving a name could merge two
    securities (for example two share classes or two bonds of one issuer).
    """
    override = Override(
        match_type="name",
        match_value="*|NORTHWIND MATERIALS",
        security_id="FIGI:BBG000000001",
        reviewer="review test",
        reviewed_on=date(2026, 10, 21),
    )
    frame = pl.DataFrame(
        {
            "fund_ticker": ["NAME", "ISIN", "TICKER", "COMPOSITE", "CASH"],
            "security_name": [
                "Northwind Materials Inc.",
                "Northwind Materials Inc.",
                "Northwind Materials Inc.",
                "Northwind Materials Inc.",
                "Cash and Cash Equivalents",
            ],
            "security_ticker": [None, None, "NWM", "NWM US", None],
            "exchange": [None, None, "XTSE", None, None],
            "isin": [None, "CA600000AA15", None, None, None],
            "cusip": [None, None, None, None, None],
            "asset_class": ["equity", "equity", "equity", "equity", "cash"],
            "country": ["CA", "CA", "CA", "CA", "CA"],
            "currency": ["CAD", "CAD", "CAD", "CAD", "CAD"],
        }
    )
    assigned = SecurityMaster([override]).assign(frame)
    assert assigned["security_id"].to_list() == [
        "FIGI:BBG000000001",
        "ISIN:CA600000AA15",
        "TKR:XTSE:NWM",
        None,
        "CASH:CAD",
    ]


def test_build_never_applies_its_own_proposals(tmp_path: Path, config: Path) -> None:
    first = _build(config, tmp_path / "first")
    second = _build(config, tmp_path / "second")
    assert first.n_name_proposals > 0
    assert (config / "security_overrides.csv").read_text(encoding="utf-8") == (
        _OVERRIDE_HEADER + "\n"
    )
    assert first.n_unmatched == second.n_unmatched
    assert first.n_holdings == second.n_holdings
    left = (tmp_path / "first" / "reports" / "name_proposals.csv").read_bytes()
    right = (tmp_path / "second" / "reports" / "name_proposals.csv").read_bytes()
    assert left == right


def test_approved_override_is_visible_to_the_next_build_only(tmp_path: Path, config: Path) -> None:
    """A by-name approval changes the next build, never the review report."""
    first = _build(config, tmp_path / "first")
    proposals = pl.read_csv(tmp_path / "first" / "reports" / "name_proposals.csv")
    name = proposals.filter(pl.col("confidence") == "exact")["normalized_name"][0]
    overrides = config / "security_overrides.csv"
    result = runner.invoke(
        app,
        [
            "review",
            "approve",
            "--db",
            str(tmp_path / "first" / "etfh.duckdb"),
            "--name",
            name,
            "--reviewer",
            "end-to-end reviewer",
            "--overrides",
            str(overrides),
        ],
    )
    assert result.exit_code == 0, result.output
    assert pl.read_csv(overrides).height == 1
    second = _build(config, tmp_path / "second")
    assert second.n_unmatched < first.n_unmatched
