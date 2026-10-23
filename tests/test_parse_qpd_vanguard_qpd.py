"""Parser tests for the Vanguard Canada quarterly disclosure rules."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from etfh.collect.registry import CollectError
from etfh.collect.vanguard_qpd import VanguardQpdCollector
from etfh.http import PoliteClient
from etfh.parse.qpd_pdf import QPD_RULES, QpdPdfParser
from etfh.registry import load_sources
from etfh.schema import ParsedHoldings
from etfh.storage import RawStore
from tests._qpd_helpers import (
    qpd_fund,
    raw_file_for_pdf,
    write_vanguard_lookalike,
    write_vanguard_older_lookalike,
    write_vanguard_wrapped_lookalike,
)


def _lookalike(tmp_path: Path) -> Path:
    return write_vanguard_lookalike(tmp_path / "vanguard.pdf")


def test_vanguard_rules_are_registered() -> None:
    rules = QPD_RULES["vanguard_ca_qpd"]
    assert rules.name == "vanguard_ca"
    # The real document's tight kerning merges words at the default
    # tolerance; the rules lower it.
    assert rules.x_tolerance == 1.0
    assert rules.column_scope == "right"


def test_vanguard_lookalike_parses_to_stated_total(tmp_path: Path) -> None:
    parsed = QpdPdfParser().parse(
        raw_file_for_pdf(_lookalike(tmp_path), source_id="vanguard_ca_qpd", fund_ticker="NORTH")
    )
    ParsedHoldings.validate(parsed.frame)
    stated = float(
        next(
            note.split("=", 1)[1]
            for note in parsed.notes
            if note.startswith("stated_total_fraction=")
        )
    )
    # The right-column Top 25 list parses; the left-column sector allocation
    # (whose total is 100.0) and the top-of-page titles do not leak in.
    assert stated == pytest.approx(0.50)
    assert parsed.frame.height == 6
    assert round(float(parsed.frame["weight"].sum()), 4) == 0.50
    assert parsed.as_of_date == date(2026, 6, 30)
    names = parsed.frame["security_name"].to_list()
    assert "Financials" not in names
    assert "Other assets and liabilities (net)" not in names
    assert parsed.frame["sector"].unique().to_list() == ["Unknown"]


def test_vanguard_wrapped_name_keeps_the_full_fund_name(tmp_path: Path) -> None:
    """VXC wraps a fund name above the line that carries its weight.

    The name must be joined across the two lines, the unlabelled listed total
    on its own line must not become a suffix, and a weight printed without a
    ``%`` (six points left of one printed with it) must still be recognised.
    """
    parsed = QpdPdfParser().parse(
        raw_file_for_pdf(
            write_vanguard_wrapped_lookalike(tmp_path / "vanguard_wrapped.pdf"),
            source_id="vanguard_ca_qpd",
            fund_ticker="VWXC",
        )
    )
    ParsedHoldings.validate(parsed.frame)
    names = parsed.frame["security_name"].to_list()
    assert names == [
        "Vanguard Fictional Large-Cap ETF*",
        "Vanguard Fictional Developed All Cap ex North America Index ETF**",
        "Vanguard Fictional Emerging Markets ETF*",
        "Cash",
    ]
    assert parsed.frame["weight"].to_list() == pytest.approx([0.125, 0.264, 0.11, 0.003])
    assert round(float(parsed.frame["weight"].sum()), 4) == 0.502
    stated = float(
        next(
            note.split("=", 1)[1]
            for note in parsed.notes
            if note.startswith("stated_total_fraction=")
        )
    )
    assert stated == pytest.approx(0.502)


def test_vanguard_older_layout_labelled_total_parses(tmp_path: Path) -> None:
    """An older quarter prints "Total investments 50.0%" instead of a bare total.

    The date is "December 31, 2024" and the listed total is labelled, so the
    issuer rule's second total pattern has to find it; the right-column
    holdings must still parse to the same rows.
    """
    parsed = QpdPdfParser().parse(
        raw_file_for_pdf(
            write_vanguard_older_lookalike(tmp_path / "vanguard_old.pdf"),
            source_id="vanguard_ca_qpd",
            fund_ticker="OLD",
        )
    )
    ParsedHoldings.validate(parsed.frame)
    assert parsed.as_of_date == date(2024, 12, 31)
    assert parsed.frame.height == 6
    stated = float(
        next(
            note.split("=", 1)[1]
            for note in parsed.notes
            if note.startswith("stated_total_fraction=")
        )
    )
    assert stated == pytest.approx(0.50)
    assert round(float(parsed.frame["weight"].sum()), 4) == 0.50


@pytest.mark.network
def test_vanguard_qpd_live_smoke(tmp_path: Path) -> None:
    if not os.environ.get("ETFH_USER_AGENT"):
        pytest.skip("ETFH_USER_AGENT is required for live collection")
    source = load_sources("config/sources.yaml")["vanguard_ca_qpd"]
    collector = VanguardQpdCollector(source=source)
    with PoliteClient() as client:
        try:
            raw_files = collector.fetch(
                qpd_fund("VEQT"),
                datetime.now(UTC).date(),
                client,
                RawStore(tmp_path / "raw"),
            )
        except CollectError as exc:
            assert "robots" in exc.reason
            return
    parsed = QpdPdfParser().parse(raw_files[0])
    stated = float(
        next(
            note.split("=", 1)[1]
            for note in parsed.notes
            if note.startswith("stated_total_fraction=")
        )
    )
    assert abs(float(parsed.frame["weight"].sum()) - stated) <= 0.005
