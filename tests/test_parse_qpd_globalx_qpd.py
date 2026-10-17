"""Parser tests for the Global X Canada quarterly disclosure rules."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from etfh.collect.globalx_qpd import GlobalXQpdCollector
from etfh.http import PoliteClient
from etfh.parse.qpd_pdf import QPD_RULES, QpdPdfParser
from etfh.registry import load_sources
from etfh.schema import ParsedHoldings
from etfh.storage import RawStore
from tests._qpd_helpers import (
    qpd_fund,
    raw_file_for_pdf,
    write_globalx_direct_lookalike,
    write_globalx_lookalike,
)


def _stated_total(notes: list[str]) -> float:
    return float(
        next(note.split("=", 1)[1] for note in notes if note.startswith("stated_total_fraction="))
    )


def test_globalx_rules_are_registered() -> None:
    assert QPD_RULES["globalx_ca_qpd"].name == "globalx_ca"


def test_globalx_lookalike_uses_top_holdings_section(tmp_path: Path) -> None:
    parsed = QpdPdfParser().parse(
        raw_file_for_pdf(
            write_globalx_lookalike(tmp_path / "globalx.pdf"),
            source_id="globalx_ca_qpd",
            fund_ticker="GXF",
            issuer="globalx_ca",
        )
    )
    ParsedHoldings.validate(parsed.frame)
    frame = parsed.frame
    assert parsed.as_of_date == date(2026, 3, 31)
    assert parsed.published_at is None
    assert frame.height == 3
    assert frame["security_name"].to_list() == [
        "Fair Value of Index Swaps",
        "Cash held for Collateral",
        "Cash - Other",
    ]
    assert "Northgate Bank" not in frame["security_name"].to_list()
    classes = dict(
        zip(frame["security_name"].to_list(), frame["asset_class"].to_list(), strict=True)
    )
    assert classes["Fair Value of Index Swaps"] == "derivative"
    assert classes["Cash held for Collateral"] == "cash"
    stated = _stated_total(parsed.notes)
    assert stated == pytest.approx(1.0)
    assert abs(float(frame["weight"].sum()) - stated) <= 0.005


def test_globalx_direct_top25_omits_unstated_total(tmp_path: Path) -> None:
    parsed = QpdPdfParser().parse(
        raw_file_for_pdf(
            write_globalx_direct_lookalike(tmp_path / "globalx_direct.pdf"),
            source_id="globalx_ca_qpd",
            fund_ticker="GXD",
            issuer="globalx_ca",
        )
    )
    ParsedHoldings.validate(parsed.frame)
    frame = parsed.frame
    assert frame.height == 5
    assert "Juniper Health Corp." in frame["security_name"].to_list()
    assert "U.S. Equities" not in frame["security_name"].to_list()
    assert "partial_list=true" in parsed.notes
    assert not any(note.startswith("stated_total_fraction=") for note in parsed.notes)


@pytest.mark.network
def test_globalx_qpd_live_smoke(tmp_path: Path) -> None:
    if not os.environ.get("ETFH_USER_AGENT"):
        pytest.skip("ETFH_USER_AGENT is required for live collection")
    source = load_sources("config/sources.yaml")["globalx_ca_qpd"]
    collector = GlobalXQpdCollector(source=source)
    with PoliteClient() as client:
        raw_files = collector.fetch(
            qpd_fund("HXT", issuer="globalx_ca", source_id="globalx_ca_qpd"),
            datetime.now(UTC).date(),
            client,
            RawStore(tmp_path / "raw"),
        )
    parsed = QpdPdfParser().parse(raw_files[0])
    assert abs(float(parsed.frame["weight"].sum()) - _stated_total(parsed.notes)) <= 0.005
