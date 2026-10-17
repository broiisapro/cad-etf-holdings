"""Parser tests for the synthetic daily formats and their classic bugs."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from etfh.parse.synthetic_daily import PARSERS, SynCsvParser, SynJsonParser, SynXlsxParser
from etfh.schema import ParsedHoldings
from etfh.types import RawFile
from tests._parse_helpers import (
    RAW_SYNTHETIC,
    find_raw,
    load_manifest,
    make_raw,
    weight_for,
    weight_sum,
)


def _parser_for(raw: RawFile) -> SynCsvParser | SynXlsxParser | SynJsonParser:
    for parser in PARSERS:
        if parser.can_parse(raw):
            return parser
    raise AssertionError(f"no parser handles {raw.path}")


def test_weights_percent_vs_fraction_normalized() -> None:
    """SYNA's percent weights and SYNB's fraction weights agree with the manifest."""
    manifest = load_manifest()
    assert manifest["percent_fraction_pair"]["unit_a"] == "percent"
    assert manifest["percent_fraction_pair"]["unit_b"] == "fraction"
    expected = manifest["expected_overlap"]
    as_of = date.fromisoformat(expected["asof"])
    common = expected["common"][0]
    isin = common["security_id"].split(":", 1)[1]

    raw_a = find_raw("SYNA", as_of=as_of)
    raw_b = find_raw("SYNB", as_of=as_of)
    parsed_a = _parser_for(raw_a).parse(raw_a)
    parsed_b = _parser_for(raw_b).parse(raw_b)

    assert weight_for(parsed_a, isin=isin) == pytest.approx(float(common["weight_a"]), abs=1e-9)
    assert weight_for(parsed_b, isin=isin) == pytest.approx(float(common["weight_b"]), abs=1e-9)
    assert "declared_unit=percent" in parsed_a.notes
    assert "declared_unit=fraction" in parsed_b.notes


def test_percent_declared_file_whose_weights_sum_to_one_gets_unit_check(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lookalike.csv"
    path.write_text(
        "Synthetic Lookalike Fund\n"
        "Fund Holdings as of,October 29, 2026\n"
        "Number of Holdings,2\n"
        "\n"
        "Ticker,Name,ISIN,Asset Class,Sector,Country,Currency,Exchange,"
        "Weight (%),Shares,Market Value\n"
        "AAA,Alpha Corp.,CA000000AA00,Equity,Energy,CA,CAD,XTSE,0.50,10,1000.00\n"
        "BBB,Beta Corp.,CA000000AA11,Equity,Energy,CA,CAD,XTSE,0.50,10,1000.00\n",
        encoding="utf-8",
    )
    raw = make_raw(
        path,
        source_id="syn_csv",
        fund_ticker="SYNA",
        as_of_hint=date(2026, 10, 29),
    )
    parsed = SynCsvParser().parse(raw)
    assert parsed.notes[0] == "declared_unit=percent"
    assert any(note.startswith("unit_check=") for note in parsed.notes)
    assert weight_sum(parsed) == pytest.approx(0.01)


def test_as_of_date_comes_from_file_not_directory(tmp_path: Path) -> None:
    fixture = find_raw("SYNA", as_of=date(2026, 10, 29))
    raw = make_raw(
        fixture.path,
        source_id="syn_csv",
        fund_ticker="SYNA",
        as_of_hint=date(1999, 1, 1),
    )
    parsed = SynCsvParser().parse(raw)
    assert parsed.as_of_date == date(2026, 10, 29)


def test_cash_rows_use_cash_asset_class() -> None:
    raw = find_raw("SYNA", as_of=date(2026, 10, 29))
    parsed = SynCsvParser().parse(raw)
    cash = parsed.frame.filter(pl.col("asset_class") == "cash")
    assert cash.height == 1
    assert cash["sector"][0] == "Unknown"
    assert cash["country"][0] == "Unknown"
    assert cash["security_ticker"][0] == "CASH"


def test_xlsx_header_block_and_date_are_detected() -> None:
    raw = find_raw("SYNB", as_of=date(2026, 10, 29))
    parsed = SynXlsxParser().parse(raw)
    ParsedHoldings.validate(parsed.frame)
    assert parsed.as_of_date == date(2026, 10, 29)
    assert "declared_unit=fraction" in parsed.notes
    assert parsed.frame.height == 21
    assert set(parsed.frame.columns) == {
        "fund_ticker",
        "as_of_date",
        "security_name",
        "security_ticker",
        "exchange",
        "isin",
        "cusip",
        "asset_class",
        "sector",
        "country",
        "currency",
        "weight",
        "shares",
        "market_value",
        "source_id",
        "raw_file_sha256",
    }


def test_xlsx_openpyxl_fallback_parses_the_same_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import etfh.parse.synthetic_daily as synthetic_daily

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("calamine unavailable")

    monkeypatch.setattr(synthetic_daily.pl, "read_excel", boom)
    raw = find_raw("SYNB", as_of=date(2026, 10, 29))
    parsed = SynXlsxParser().parse(raw)
    ParsedHoldings.validate(parsed.frame)
    assert parsed.frame.height == 21
    assert parsed.as_of_date == date(2026, 10, 29)


def test_json_portfolio_date_and_holdings() -> None:
    raw = find_raw("SYNE", as_of=date(2026, 10, 29))
    parsed = SynJsonParser().parse(raw)
    ParsedHoldings.validate(parsed.frame)
    assert parsed.as_of_date == date(2026, 10, 29)
    assert parsed.published_at is None
    assert parsed.frame.height == 22
    assert "declared_unit=fraction" in parsed.notes


def test_injected_bad_files_still_parse() -> None:
    manifest = load_manifest()
    assert {item["failing_check"] for item in manifest["bad_files"]} == {
        "weight_sum",
        "duplicate_security",
    }
    for bad in manifest["bad_files"]:
        fund = bad["fund_ticker"]
        path = RAW_SYNTHETIC / bad["path"]
        source_id = manifest["funds"][fund]["source_id"]
        raw = make_raw(path, source_id=source_id, fund_ticker=fund)
        parsed = _parser_for(raw).parse(raw)
        ParsedHoldings.validate(parsed.frame)
        if bad["failing_check"] == "weight_sum":
            assert weight_sum(parsed) == pytest.approx(0.85)
        if bad["failing_check"] == "duplicate_security":
            names = parsed.frame["security_name"].to_list()
            assert names.count("Northwind Materials Inc.") == 2
