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
    assert "exchange_codes=CN,LN,US" in parsed.notes
    frame = parsed.frame
    ParsedHoldings.validate(frame)
    assert frame.height == 7
    assert frame["weight"].to_list() == pytest.approx(
        [0.40, 0.35, 0.20, 0.052434, -0.001234, -0.000500, -0.000700], abs=1e-12
    )
    assert float(frame["weight"].sum()) == pytest.approx(1.0, abs=1e-9)
    names = frame["security_name"].to_list()
    assert names[0] == "Northolt Instruments PLC"


def test_exchange_codes_are_passed_through_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    import etfh.parse.evolve as evolve

    seen: list[str | None] = []

    def recorder(label: str | None) -> str | None:
        seen.append(label)
        return f"MIC:{label}"

    monkeypatch.setattr(evolve, "normalize_exchange", recorder)
    frame = EvolveParser().parse(_raw()).frame
    by_name = {row["security_name"]: row["exchange"] for row in frame.to_dicts()}
    assert by_name["Northolt Instruments PLC"] == "MIC:LN"
    assert by_name["Xenon Quarry Ventures Inc"] == "MIC:US"
    assert by_name["Brookstone Railway Systems Ltd"] == "MIC:CN"
    assert sorted(set(seen)) == ["CN", "LN", "US"]


def test_cash_and_derivative_rows() -> None:
    frame = EvolveParser().parse(_raw()).frame
    cash = frame.filter(pl.col("asset_class") == "cash")
    assert cash.height == 1
    assert cash["currency"][0] == "CAD"
    assert cash["sector"][0] == "Unknown"
    assert cash["country"][0] == "Unknown"
    assert cash["security_ticker"][0] == "CASH"
    derivative = frame.filter(pl.col("asset_class") == "derivative")
    assert derivative.height == 3
    assert all(weight < 0 for weight in derivative["weight"].to_list())
    assert derivative["shares"].to_list() == [-1.0, -2.0, -3.0]
    assert derivative["market_value"].to_list() == [-1234.0, -500.0, -700.0]


def test_derivative_tickers_keep_the_option_descriptor() -> None:
    frame = EvolveParser().parse(_raw()).frame
    derivative = frame.filter(pl.col("asset_class") == "derivative")
    tickers = set(derivative["security_ticker"].to_list())
    assert tickers == {
        "NORL LN 10/16/26 C125",
        "XQVI US 10/16/26 C30",
        "XQVA US 10/16/26 C30",
    }
    # Two option lines share the security name; the descriptors keep them apart.
    same_name = derivative.filter(
        pl.col("security_name") == "Xenon Quarry Ventures Inc - CALL Option"
    )
    assert same_name.height == 2
    assert same_name["security_ticker"].n_unique() == 2


def test_company_names_are_not_misread_as_derivatives(tmp_path: Path) -> None:
    path = tmp_path / "names.csv"
    path.write_text(
        "TICKER,PORTFOLIO_MWEIGHT,POSITION,SECURITY_NAME,GICS_SECTOR_NAME,COUNTRY\n"
        '"BRGT US EQUITY",0.5,1,"Brightstone Rail Systems Inc",Industrials,"United States"\n'
        '"WRTC US EQUITY",0.5,1,"Warranty Corp",Industrials,"United States"\n',
        encoding="utf-8",
    )
    frame = EvolveParser().parse(_raw(path)).frame
    assert set(frame["asset_class"].to_list()) == {"equity"}


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
    assert parsed.frame.height == 7
    assert parsed.frame["security_name"][0] == "Northolt Instruments PLC"


def test_missing_as_of_hint_raises() -> None:
    with pytest.raises(ValueError, match="no as-of date"):
        EvolveParser().parse(_raw(as_of=None))


def test_unit_contradiction_note_only_when_the_other_unit_fits(tmp_path: Path) -> None:
    header = "TICKER,PORTFOLIO_MWEIGHT,POSITION,SECURITY_NAME,GICS_SECTOR_NAME,COUNTRY\n"
    percent_rows = (
        '"NORL LN EQUITY",60.0,1,"Northolt Instruments PLC",Technology,"Britain"\n'
        '"XQVI US EQUITY",40.0,1,"Xenon Quarry Ventures Inc",Technology,"United States"\n'
    )
    path = tmp_path / "percent.csv"
    path.write_text(header + percent_rows, encoding="utf-8")
    parsed = EvolveParser().parse(_raw(path))
    notes = [note for note in parsed.notes if note.startswith("unit_check=")]
    assert len(notes) == 1
    assert "total matches unit percent" in notes[0]

    short = tmp_path / "short.csv"
    short.write_text(
        header + '"NORL LN EQUITY",0.5,1,"Northolt Instruments PLC",Technology,"Britain"\n',
        encoding="utf-8",
    )
    parsed_short = EvolveParser().parse(_raw(short))
    assert not any(note.startswith("unit_check=") for note in parsed_short.notes)


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
