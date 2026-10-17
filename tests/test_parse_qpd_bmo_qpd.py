"""Parser tests for the BMO quarterly disclosure rules."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from etfh.collect.bmo_qpd import BmoQpdCollector
from etfh.http import PoliteClient
from etfh.parse.qpd_pdf import QPD_RULES, QpdPdfParser
from etfh.registry import load_sources
from etfh.schema import ParsedHoldings
from etfh.storage import RawStore
from tests._qpd_helpers import qpd_fund, raw_file_for_pdf, write_bmo_lookalike


def test_bmo_rules_are_registered() -> None:
    assert QPD_RULES["bmo_qpd"].name == "bmo"


def test_bmo_lookalike_ignores_shares_and_market_value(tmp_path: Path) -> None:
    parsed = QpdPdfParser().parse(
        raw_file_for_pdf(
            write_bmo_lookalike(tmp_path / "bmo.pdf"),
            source_id="bmo_qpd",
            fund_ticker="ZFN",
            issuer="bmo",
        )
    )
    ParsedHoldings.validate(parsed.frame)
    frame = parsed.frame
    assert parsed.as_of_date == date(2026, 6, 30)
    assert frame["security_name"].to_list() == [
        "Northgate Bank",
        "Maplecrest Properties",
        "Pineview Software Corp.",
        "Cash and Cash Equivalents",
    ]
    assert frame["shares"].null_count() == frame.height
    assert frame["market_value"].null_count() == frame.height
    assert "stated_total_fraction=0.991" in parsed.notes
    total = float(
        next(
            note.split("=", 1)[1]
            for note in parsed.notes
            if note.startswith("stated_total_fraction=")
        )
    )
    assert total == pytest.approx(0.991)
    assert round(float(frame["weight"].sum()), 4) == 0.991


@pytest.mark.network
def test_bmo_qpd_live_smoke(tmp_path: Path) -> None:
    if not os.environ.get("ETFH_USER_AGENT"):
        pytest.skip("ETFH_USER_AGENT is required for live collection")
    source = load_sources("config/sources.yaml")["bmo_qpd"]
    collector = BmoQpdCollector(source=source)
    with PoliteClient() as client:
        try:
            raw_files = collector.fetch(
                qpd_fund("ZSP", issuer="bmo", source_id="bmo_qpd"),
                datetime.now(UTC).date(),
                client,
                RawStore(tmp_path / "raw"),
            )
        except Exception as exc:
            assert "robots" in str(exc) or "timed out" in str(exc).lower()
            return
    parsed = QpdPdfParser().parse(raw_files[0])
    assert parsed.as_of_date is not None
