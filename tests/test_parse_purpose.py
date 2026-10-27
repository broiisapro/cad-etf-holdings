"""Tests for the Purpose daily holdings parser."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from etfh.parse.purpose import PARSERS, PurposeParser
from etfh.parse.registry import find_parser
from etfh.registry import load_sources
from etfh.schema import ParsedHoldings
from etfh.types import RawFile
from tests._parse_helpers import make_raw

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "issuers" / "purpose"
FIXTURE = FIXTURE_DIR / "lookalike.json"
OPTIONS = FIXTURE_DIR / "options.json"
PARTIAL = FIXTURE_DIR / "partial.json"
TOP_TEN = FIXTURE_DIR / "top_ten.json"
COMMODITY = FIXTURE_DIR / "commodity.json"
COVERED_CALL = FIXTURE_DIR / "covered_call.json"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _raw(path: Path = FIXTURE, fund_ticker: str = "PURA") -> RawFile:
    return make_raw(
        path,
        source_id="purpose_daily",
        fund_ticker=fund_ticker,
        as_of_hint=date(2026, 10, 16),
    )


def _payload(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _write(tmp_path: Path, payload: dict[str, object], name: str = "payload.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_parses_lookalike_holdings() -> None:
    parsed = PurposeParser().parse(_raw())
    assert parsed.as_of_date == date(2026, 10, 16)
    assert parsed.published_at is None
    assert parsed.notes[0] == "declared_unit=percent"
    assert "weight_decimals=2" in parsed.notes
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


def test_weight_decimals_note_tracks_the_printed_precision(tmp_path: Path) -> None:
    payload = _payload(FIXTURE)
    portfolio = payload["portfolio"]
    assert isinstance(portfolio, dict)
    portfolio["Level4"]["holdings"][0]["weight"] = "40.125%"  # type: ignore[index]
    portfolio["Level4"]["holdings"][1]["weight"] = "29.875%"  # type: ignore[index]
    portfolio["Level4"]["holdings"][2]["weight"] = "30.000%"  # type: ignore[index]
    path = _write(tmp_path, payload, "precision.json")
    parsed = PurposeParser().parse(_raw(path))
    assert "weight_decimals=3" in parsed.notes
    assert float(parsed.frame["weight"].sum()) == pytest.approx(1.0, abs=1e-12)


def test_physical_commodity_quantity_parses_as_one_holding() -> None:
    parsed = PurposeParser().parse(_raw(COMMODITY, "PURC"))
    assert parsed.as_of_date == date(2026, 10, 20)
    assert "commodity_quantity=128,450 oz" in parsed.notes
    assert "weight_decimals=2" in parsed.notes
    assert "partial_list=true" not in parsed.notes
    assert not any(note.startswith("unit_check=") for note in parsed.notes)
    frame = parsed.frame
    ParsedHoldings.validate(frame)
    assert frame.height == 1
    row = frame.to_dicts()[0]
    assert row["security_name"] == "Gold Bars"
    assert row["asset_class"] == "commodity"
    assert row["weight"] == 1.0
    # A physical holding has no security identifier and no fabricated ticker.
    assert row["security_ticker"] is None
    assert row["isin"] is None
    assert row["cusip"] is None
    assert row["exchange"] is None
    assert row["shares"] is None
    assert row["market_value"] is None
    assert float(frame["weight"].sum()) == 1.0


def test_commodity_branch_joins_repeated_lines_of_one_commodity(tmp_path: Path) -> None:
    payload = _payload(COMMODITY)
    portfolio = payload["portfolio"]
    assert isinstance(portfolio, dict)
    portfolio["Level4"]["holdings"].append({"name": "Gold Bars", "weight": "10,000 oz"})  # type: ignore[index]
    path = _write(tmp_path, payload, "two_bars.json")
    parsed = PurposeParser().parse(_raw(path, "PURC"))
    assert parsed.frame.height == 1
    assert "commodity_quantity=128,450 oz; 10,000 oz" in parsed.notes


def test_a_weighted_row_keeps_the_file_out_of_the_commodity_branch(tmp_path: Path) -> None:
    payload = _payload(COMMODITY)
    portfolio = payload["portfolio"]
    assert isinstance(portfolio, dict)
    portfolio["Level4"]["holdings"].append(  # type: ignore[index]
        {
            "name": "Northvale Banking Corp.",
            "geo": "Canada",
            "sector": "Financials",
            "weight": "5.00%",
        }
    )
    path = _write(tmp_path, payload, "mixed.json")
    parsed = PurposeParser().parse(_raw(path, "PURC"))
    assert parsed.frame.height == 1
    assert parsed.frame["security_name"][0] == "Northvale Banking Corp."
    assert parsed.frame["weight"][0] == pytest.approx(0.05, abs=1e-12)
    assert not any(note.startswith("commodity_quantity=") for note in parsed.notes)
    # The quantity line is not a holding and the shortfall stays visible.
    assert not any(note.startswith("partial_list") for note in parsed.notes)


def test_mixed_physical_names_or_unreadable_rows_still_fail(tmp_path: Path) -> None:
    payload = _payload(COMMODITY)
    portfolio = payload["portfolio"]
    assert isinstance(portfolio, dict)
    portfolio["Level4"]["holdings"].append({"name": "Silver Bars", "weight": "9,000 oz"})  # type: ignore[index]
    distinct = _write(tmp_path, payload, "distinct.json")
    with pytest.raises(ValueError, match="no holdings rows found"):
        PurposeParser().parse(_raw(distinct, "PURC"))

    payload = _payload(COMMODITY)
    portfolio = payload["portfolio"]
    assert isinstance(portfolio, dict)
    portfolio["Level4"]["holdings"].append({"name": "Suspense Account", "weight": "-"})  # type: ignore[index]
    unreadable = _write(tmp_path, payload, "unreadable.json")
    with pytest.raises(ValueError, match="no holdings rows found"):
        PurposeParser().parse(_raw(unreadable, "PURC"))


def test_parsed_frame_validates_against_schema() -> None:
    parsed = PurposeParser().parse(_raw())
    ParsedHoldings.validate(parsed.frame)
    assert parsed.frame["shares"].null_count() == parsed.frame.height
    assert parsed.frame["market_value"].null_count() == parsed.frame.height


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


def test_exchange_labels_are_normalized_to_mics(tmp_path: Path) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["portfolio"]["Level4"]["holdings"][0]["exchange"] = "TSX"
    path = tmp_path / "with_exchange.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    parsed = PurposeParser().parse(_raw(path))
    exchange = parsed.frame.filter(pl.col("security_name") == "Northvale Banking Corp.")
    assert exchange["exchange"][0] == "XTSE"


def test_discovered_by_the_parser_registry() -> None:
    source = load_sources(CONFIG_DIR / "sources.yaml")["purpose_daily"]
    parser = find_parser(_raw(), source)
    assert isinstance(parser, PurposeParser)


def test_uses_fund_level_asset_class_for_non_cash_rows() -> None:
    parsed = PurposeParser().parse(_raw())
    assert set(parsed.frame["asset_class"].to_list()) == {"equity", "cash"}
    assert parsed.frame["fund_ticker"].unique().to_list() == ["PURA"]
    assert parsed.frame["source_id"].unique().to_list() == ["purpose_daily"]


def test_option_future_and_fx_rows_are_classified() -> None:
    parsed = PurposeParser().parse(_raw(OPTIONS, "PURO"))
    frame = parsed.frame
    ParsedHoldings.validate(frame)
    assert frame.height == 7
    assert float(frame["weight"].sum()) == pytest.approx(1.0, abs=1e-12)
    assert "partial_list=true" not in parsed.notes
    assert not any(note.startswith("unit_check=") for note in parsed.notes)
    classes = {row["security_name"]: row["asset_class"] for row in frame.to_dicts()}
    assert classes["Northvale Banking Corp. Call 120 11/20/26"] == "derivative"
    assert classes["Pinehurst Utilities Inc. Put 88 12/18/26"] == "derivative"
    assert classes["Maple Ridge 10-Year Note Future 03/20/27"] == "derivative"
    assert classes["Sterling Creek FX Forward 12/15/26"] == "fx"
    assert classes["Cedar Hollow Cash Account"] == "cash"
    assert classes["Northvale Banking Corp."] == "equity"
    derivative = frame.filter(pl.col("asset_class") == "derivative")
    assert set(derivative["security_name"].to_list()) == {
        "Northvale Banking Corp. Call 120 11/20/26",
        "Pinehurst Utilities Inc. Put 88 12/18/26",
        "Maple Ridge 10-Year Note Future 03/20/27",
    }
    call = derivative.filter(pl.col("security_name").str.contains("Call"))
    assert call["weight"][0] == pytest.approx(-0.0025, abs=1e-12)
    assert call["security_ticker"][0] is None


def test_partial_list_flagged_when_holdings_omit_cash() -> None:
    parsed = PurposeParser().parse(_raw(PARTIAL, "PURP"))
    assert "partial_list=true" in parsed.notes
    assert not any(note.startswith("unit_check=") for note in parsed.notes)
    assert float(parsed.frame["weight"].sum()) == pytest.approx(0.9667, abs=1e-12)
    assert parsed.frame.height == 3


def test_covered_call_total_above_one_is_partial_and_keeps_its_weights() -> None:
    """A covered-call list can print a total just above 100% as published.

    The document's own breakdown is complete (100%), while the listed rows add
    up to 100.64%: ten equities (100.66%) plus nine written calls at 0.00% /
    -0.01% and no cash row. That is the same unpublished cash line that makes a
    cash-holding day fall short, so the file is partial (D52) — but the weights
    stay exactly as published; the parser never renormalizes them.
    """
    parsed = PurposeParser().parse(_raw(COVERED_CALL, "PURF"))
    assert parsed.as_of_date == date(2026, 10, 22)
    assert "partial_list=true" in parsed.notes
    assert "weight_decimals=2" in parsed.notes
    assert not any(note.startswith("unit_check=") for note in parsed.notes)
    frame = parsed.frame
    ParsedHoldings.validate(frame)
    assert frame.height == 19
    assert float(frame["weight"].sum()) == pytest.approx(1.0064, abs=1e-12)
    assert float(frame.filter(pl.col("asset_class") == "equity")["weight"].sum()) == pytest.approx(
        1.0066, abs=1e-12
    )
    assert float(
        frame.filter(pl.col("asset_class") == "derivative")["weight"].sum()
    ) == pytest.approx(-0.0002, abs=1e-12)


def test_total_above_one_without_breakdown_evidence_is_not_partial(tmp_path: Path) -> None:
    payload = _payload(COVERED_CALL)
    portfolio = payload["portfolio"]
    assert isinstance(portfolio, dict)
    portfolio.pop("Level1")
    portfolio.pop("Level2")
    path = _write(tmp_path, payload, "no_breakdown.json")
    parsed = PurposeParser().parse(_raw(path, "PURF"))
    assert not any(note.startswith("partial_list") for note in parsed.notes)
    assert float(parsed.frame["weight"].sum()) == pytest.approx(1.0064, abs=1e-12)


def test_file_that_reconciles_with_its_own_total_is_not_partial(tmp_path: Path) -> None:
    payload = _payload(FIXTURE)
    portfolio = payload["portfolio"]
    assert isinstance(portfolio, dict)
    # A breakdown that states 100% and rows that add up to it: no note.
    path = _write(tmp_path, payload, "reconciled.json")
    parsed = PurposeParser().parse(_raw(path))
    assert "partial_list=true" not in parsed.notes


def test_top_ten_list_is_partial_and_maps_fund_label() -> None:
    parsed = PurposeParser().parse(_raw(TOP_TEN, "PURB"))
    assert "partial_list=true" in parsed.notes
    assert parsed.as_of_date == date(2026, 9, 30)
    frame = parsed.frame
    assert frame.height == 10
    assert set(frame["asset_class"].to_list()) == {"fixed_income"}
    assert float(frame["weight"].sum()) == pytest.approx(0.0915, abs=1e-12)


def test_short_list_without_breakdown_evidence_is_not_partial(tmp_path: Path) -> None:
    payload = _payload(FIXTURE)
    portfolio = payload["portfolio"]
    assert isinstance(portfolio, dict)
    portfolio.pop("Level1")
    holdings = portfolio["Level4"]["holdings"]  # type: ignore[index]
    del holdings[2]
    del holdings[-1]
    path = _write(tmp_path, payload)
    parsed = PurposeParser().parse(_raw(path))
    assert float(parsed.frame["weight"].sum()) == pytest.approx(0.70, abs=1e-12)
    assert "partial_list=true" not in parsed.notes
    assert not any(note.startswith("unit_check=") for note in parsed.notes)


def test_unparseable_weight_is_not_hidden_by_partial_list(tmp_path: Path) -> None:
    payload = _payload(PARTIAL)
    portfolio = payload["portfolio"]
    assert isinstance(portfolio, dict)
    holdings = portfolio["Level4"]["holdings"]  # type: ignore[index]
    holdings.append({"name": "Westgate Energy Corp.", "weight": "-"})
    path = _write(tmp_path, payload)
    parsed = PurposeParser().parse(_raw(path))
    assert parsed.frame.height == 3
    assert "partial_list=true" not in parsed.notes


def test_unit_contradiction_is_still_reported(tmp_path: Path) -> None:
    payload = _payload(FIXTURE)
    portfolio = payload["portfolio"]
    assert isinstance(portfolio, dict)
    for holding in portfolio["Level4"]["holdings"]:  # type: ignore[index]
        weight = holding["weight"]
        if weight != "100.00%":
            holding["weight"] = str(float(weight.rstrip("%")) / 100.0)
    path = _write(tmp_path, payload)
    parsed = PurposeParser().parse(_raw(path))
    notes = [note for note in parsed.notes if note.startswith("unit_check=")]
    assert len(notes) == 1
    assert "total matches unit fraction" in notes[0]
    assert "partial_list=true" not in parsed.notes
