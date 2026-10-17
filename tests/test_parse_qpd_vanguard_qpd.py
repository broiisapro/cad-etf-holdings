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
from tests._qpd_helpers import qpd_fund, raw_file_for_pdf, write_qpd_pdf


def _lookalike(tmp_path: Path) -> Path:
    return write_qpd_pdf(
        tmp_path / "vanguard.pdf",
        as_of="June 30, 2026",
        fund_name="Northline Balanced ETF",
        published=None,
        header="Security % of Net Assets",
        rows=[
            "Canadian Equity",
            ("Maplecrest Properties", "18.40"),
            ("Northgate Bank", "12.10"),
            "U.S. Equity",
            ("Uplands Chemicals Inc.", "24.50"),
            ("Pineview Software Corp.", "19.90"),
            "Fixed Income",
            ("Fairhaven Bond Fund", "20.00"),
            ("Cash and Cash Equivalents", "4.00"),
        ],
        total="Total Investments 98.90%",
    )


def test_vanguard_rules_are_registered() -> None:
    assert QPD_RULES["vanguard_ca_qpd"].name == "vanguard_ca"


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
    assert stated == pytest.approx(0.989)
    assert round(float(parsed.frame["weight"].sum()), 4) == 0.989
    assert parsed.as_of_date == date(2026, 6, 30)
    sectors = dict(
        zip(
            parsed.frame["security_name"].to_list(),
            parsed.frame["sector"].to_list(),
            strict=True,
        )
    )
    assert sectors["Maplecrest Properties"] == "Canadian Equity"
    assert sectors["Pineview Software Corp."] == "U.S. Equity"
    assert sectors["Fairhaven Bond Fund"] == "Fixed Income"


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
