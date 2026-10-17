"""Tests for the Evolve daily holdings parser."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from etfh.parse.evolve import PARSERS, EvolveParser
from etfh.parse.registry import find_parser
from etfh.registry import load_sources
from etfh.schema import ParsedHoldings
from etfh.types import RawFile
from tests._parse_helpers import make_raw

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issuers" / "evolve" / "lookalike.csv"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _raw(path: Path = FIXTURE, as_of: date | None = date(2026, 10, 7)) -> RawFile:
    return make_raw(
        path,
        source_id="evolve_daily",
        fund_ticker="EVLK",
        as_of_hint=as_of,
    )


def test_parses_lookalike_csv() -> None:
    parsed = EvolveParser().parse(_raw())
    assert parsed.as_of_date == date(2026, 10, 7)
    assert parsed.published_at is None
    assert parsed.notes[0] == "declared_unit=fraction"
    assert not any(note.startswith("unit_check=") for note in parsed.notes)
    frame = parsed.frame
    ParsedHoldings.validate(frame)
    assert frame.height == 5
    assert frame["weight"].to_list() == pytest.approx(
        [0.40, 0.35, 0.20, 0.051234, -0.001234], abs=1e-12
    )
    assert float(frame["weight"].sum()) == pytest.approx(1.0, abs=1e-9)
    names = frame["security_name"].to_list()
    assert names[0] == "Northolt Instruments PLC"


def test_exchange_labels_use_the_mic_table() -> None:
    frame = EvolveParser().parse(_raw()).frame
    by_name = {row["security_name"]: row["exchange"] for row in frame.to_dicts()}
    assert by_name["Northolt Instruments PLC"] == "XLON"
    assert by_name["Xenon Quarry Ventures Inc"] is None
    assert by_name["Brookstone Railway Systems Ltd"] is None


def test_cash_and_derivative_rows() -> None:
    frame = EvolveParser().parse(_raw()).frame
    cash = frame.filter(pl.col("asset_class") == "cash")
    assert cash.height == 1
    assert cash["currency"][0] == "CAD"
    assert cash["sector"][0] == "Unknown"
    assert cash["country"][0] == "Unknown"
    assert cash["security_ticker"][0] == "CASH"
    derivative = frame.filter(pl.col("asset_class") == "derivative")
    assert derivative.height == 1
    assert derivative["security_ticker"][0] is None
    assert derivative["weight"][0] < 0
    assert derivative["shares"][0] == -1.0
    assert derivative["market_value"][0] == -1234.0


def test_scientific_notation_and_sector_country() -> None:
    frame = EvolveParser().parse(_raw()).frame
    xqvi = frame.filter(pl.col("security_name") == "Xenon Quarry Ventures Inc")
    assert float(xqvi["weight"][0]) == pytest.approx(0.35, abs=1e-12)
    assert xqvi["sector"][0] == "Technology"
    assert xqvi["country"][0] == "United States"


def test_latin1_fallback(tmp_path: Path) -> None:
    path = tmp_path / "latin1.csv"
    path.write_bytes(FIXTURE.read_bytes().decode("utf-8").encode("latin-1"))
    assert b"\xc9tats-Unis" in path.read_bytes()
    parsed = EvolveParser().parse(_raw(path))
    assert parsed.frame.height == 5
    assert parsed.frame["security_name"][0] == "Northolt Instruments PLC"


def test_missing_as_of_hint_raises() -> None:
    with pytest.raises(ValueError, match="no as-of date"):
        EvolveParser().parse(_raw(as_of=None))


def test_discovered_by_the_parser_registry() -> None:
    source = load_sources(CONFIG_DIR / "sources.yaml")["evolve_daily"]
    parser = find_parser(_raw(), source)
    assert isinstance(parser, EvolveParser)


def test_can_parse_only_evolve_csv() -> None:
    parser = EvolveParser()
    assert parser.can_parse(_raw())
    other = make_raw(FIXTURE, source_id="bmo_daily", fund_ticker="EVLK")
    assert not parser.can_parse(other)
    assert len(PARSERS) == 1
    assert isinstance(PARSERS[0], EvolveParser)
