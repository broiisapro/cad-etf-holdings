"""Tests for the Purpose daily holdings parser."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from etfh.parse.purpose import PARSERS, PurposeParser
from etfh.schema import ParsedHoldings
from etfh.types import RawFile
from tests._parse_helpers import make_raw

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issuers" / "purpose" / "lookalike.json"


def _raw(path: Path = FIXTURE, fund_ticker: str = "PURA") -> RawFile:
    return make_raw(
        path,
        source_id="purpose_daily",
        fund_ticker=fund_ticker,
        as_of_hint=date(2026, 10, 16),
    )


def test_parses_lookalike_holdings() -> None:
    parsed = PurposeParser().parse(_raw())
    assert parsed.as_of_date == date(2026, 10, 16)
    assert parsed.published_at is None
    assert parsed.notes[0] == "declared_unit=percent"
    assert not any(note.startswith("unit_check=") for note in parsed.notes)
    frame = parsed.frame
    assert frame.height == 3
    assert frame["security_name"].to_list() == [
        "Northvale Banking Corp.",
        "Pinehurst Utilities Inc.",
        "Cedar Hollow Cash Account",
    ]
    assert frame["weight"].to_list() == pytest.approx([0.40, 0.30, 0.30], abs=1e-12)
    assert frame["sector"].to_list() == ["Financials", "Utilities", "Unknown"]
    assert frame["country"].to_list() == ["Canada", "Canada", "Unknown"]
    assert frame["asset_class"].to_list() == ["equity", "equity", "cash"]
    cash = frame.filter(pl.col("asset_class") == "cash")
    assert cash["security_ticker"][0] is None
    assert cash["isin"][0] is None
    assert "Total" not in frame["security_name"].to_list()


def test_parsed_frame_validates_once_nullable_shares_land() -> None:
    schema = ParsedHoldings.to_schema()
    shares_nullable = bool(schema.columns["shares"].nullable)
    if not shares_nullable:
        pytest.skip("waiting for the D45 schema correction (nullable shares)")
    ParsedHoldings.validate(PurposeParser().parse(_raw()).frame)


def test_missing_portfolio_date_raises(tmp_path: Path) -> None:
    path = tmp_path / "no_date.json"
    path.write_text(json.dumps({"portfolio": {"Level4": {"holdings": []}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="no portfolio date"):
        PurposeParser().parse(_raw(path))


def test_no_holdings_raises(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text(
        json.dumps({"portfolio": {"dt": "2026-10-16", "Level4": {"holdings": []}}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no holdings"):
        PurposeParser().parse(_raw(path))


def test_can_parse_only_purpose_json() -> None:
    parser = PurposeParser()
    raw = _raw()
    assert parser.can_parse(raw)
    pdf = make_raw(FIXTURE, source_id="purpose_qpd", fund_ticker="PURA")
    assert not parser.can_parse(pdf)
    other = make_raw(FIXTURE, source_id="evolve_daily", fund_ticker="EURA")
    assert not parser.can_parse(other)
    assert len(PARSERS) == 1
    assert isinstance(PARSERS[0], PurposeParser)


def test_uses_fund_level_asset_class_for_non_cash_rows() -> None:
    parsed = PurposeParser().parse(_raw())
    assert set(parsed.frame["asset_class"].to_list()) == {"equity", "cash"}
    assert parsed.frame["fund_ticker"].unique().to_list() == ["PURA"]
    assert parsed.frame["source_id"].unique().to_list() == ["purpose_daily"]
