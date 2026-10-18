"""Parser tests for the Purpose and Evolve quarterly disclosure rules."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from etfh.collect.purpose_evolve_qpd import EvolveQpdCollector, PurposeQpdCollector
from etfh.collect.registry import CollectError
from etfh.http import PoliteClient
from etfh.parse.qpd_pdf import QPD_RULES, QpdPdfParser
from etfh.registry import load_sources
from etfh.schema import ParsedHoldings
from etfh.storage import RawStore
from tests._qpd_helpers import (
    qpd_fund,
    raw_file_for_pdf,
    write_evolve_lookalike,
    write_purpose_lookalike,
)


def _stated_total(notes: list[str]) -> float:
    return float(
        next(note.split("=", 1)[1] for note in notes if note.startswith("stated_total_fraction="))
    )


def test_rules_are_registered() -> None:
    assert QPD_RULES["purpose_qpd"].name == "purpose"
    assert QPD_RULES["evolve_qpd"].name == "evolve"


def test_purpose_lookalike_parses(tmp_path: Path) -> None:
    parsed = QpdPdfParser().parse(
        raw_file_for_pdf(
            write_purpose_lookalike(tmp_path / "purpose.pdf"),
            source_id="purpose_qpd",
            fund_ticker="PDIV",
            issuer="purpose",
        )
    )
    ParsedHoldings.validate(parsed.frame)
    assert parsed.as_of_date == date(2026, 6, 30)
    assert parsed.notes == ["declared_unit=percent", "partial_list=true"]
    names = parsed.frame["security_name"].to_list()
    weights = dict(zip(names, parsed.frame["weight"].to_list(), strict=True))
    # The right-column Top 25 list parses; the left-column SECTOR MIX and
    # ASSET MIX tables and the footer do not leak in.
    assert parsed.frame.height == 5
    assert "Financials" not in names
    assert "Canadian Equities" not in names
    assert all(sector == "Unknown" for sector in parsed.frame["sector"].to_list())
    # The wrapped bond name's weight was printed on its own line but is kept.
    wrapped = [name for name in names if name.startswith("Fjord Shipping")]
    assert len(wrapped) == 1
    assert weights[wrapped[0]] == pytest.approx(0.032)
    assert round(float(parsed.frame["weight"].sum()), 4) == 0.221


def test_evolve_lookalike_parses(tmp_path: Path) -> None:
    parsed = QpdPdfParser().parse(
        raw_file_for_pdf(
            write_evolve_lookalike(tmp_path / "evolve.pdf"),
            source_id="evolve_qpd",
            fund_ticker="CYBX",
            issuer="evolve",
        )
    )
    ParsedHoldings.validate(parsed.frame)
    assert parsed.as_of_date == date(2026, 6, 30)
    names = parsed.frame["security_name"].to_list()
    # Top 25 Positions (left column) only: the sector mix and its 100.00%
    # total on the right are excluded, and the left column's own total wins.
    assert parsed.frame.height == 5
    assert "Information Technology" not in names
    assert _stated_total(parsed.notes) == pytest.approx(0.468)
    assert round(float(parsed.frame["weight"].sum()), 4) == 0.468


@pytest.mark.network
def test_purpose_qpd_live_smoke(tmp_path: Path) -> None:
    if not os.environ.get("ETFH_USER_AGENT"):
        pytest.skip("ETFH_USER_AGENT is required for live collection")
    source = load_sources("config/sources.yaml")["purpose_qpd"]
    collector = PurposeQpdCollector(source=source)
    fund = qpd_fund("PDF", issuer="purpose", source_id="purpose_qpd").model_copy(
        update={"params": {"purpose_slug": "purpose-core-dividend-fund"}}
    )
    with PoliteClient() as client:
        try:
            raw_files = collector.fetch(
                fund, datetime.now(UTC).date(), client, RawStore(tmp_path / "raw")
            )
        except CollectError as exc:
            assert "robots" in exc.reason
            return
    parsed = QpdPdfParser().parse(raw_files[0])
    # Purpose's Top 25 list has no stated total; the file is partial and the
    # parsed rows just have to be 25 non-empty positions.
    assert "partial_list=true" in parsed.notes
    assert not any(note.startswith("stated_total_fraction=") for note in parsed.notes)
    assert parsed.frame.height == 25
    assert float(parsed.frame["weight"].sum()) > 0.0


@pytest.mark.network
def test_evolve_qpd_live_smoke(tmp_path: Path) -> None:
    if not os.environ.get("ETFH_USER_AGENT"):
        pytest.skip("ETFH_USER_AGENT is required for live collection")
    source = load_sources("config/sources.yaml")["evolve_qpd"]
    collector = EvolveQpdCollector(source=source)
    with PoliteClient() as client:
        try:
            raw_files = collector.fetch(
                qpd_fund("CYBR", issuer="evolve", source_id="evolve_qpd"),
                datetime.now(UTC).date(),
                client,
                RawStore(tmp_path / "raw"),
            )
        except CollectError as exc:
            assert "robots" in exc.reason
            return
    parsed = QpdPdfParser().parse(raw_files[0])
    assert abs(float(parsed.frame["weight"].sum()) - _stated_total(parsed.notes)) <= 0.005
