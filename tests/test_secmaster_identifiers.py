"""Tests for identifier primitives: ISIN/CUSIP validation, derivation and MICs."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import openpyxl
import pytest
from hypothesis import given
from hypothesis import strategies as st

from etfh.secmaster.identifiers import (
    cusip_is_valid,
    cusip_to_isin,
    isin_is_valid,
    normalize_exchange,
    normalize_ticker,
)

_CUSIP_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ*@#"
_ISIN_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_FIXTURES = Path(__file__).parent / "fixtures" / "raw_synthetic"


def _cusip_check(base8: str) -> str:
    total = 0
    for index, character in enumerate(base8):
        value = _CUSIP_ALPHABET.index(character)
        if index % 2 == 1:
            value *= 2
        total += value // 10 + value % 10
    return str((10 - total % 10) % 10)


def _isin_check(payload: str) -> str:
    digits = "".join(str(int(character, 36)) for character in payload)
    total = 0
    for index, digit in enumerate(reversed(digits)):
        value = int(digit)
        if index % 2 == 0:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return str((10 - total % 10) % 10)


@pytest.mark.parametrize(
    "cusip",
    [
        "037833100",
        "17275R102",
        "38259P508",
        "594918104",
        "68389X105",
        "600000AA1",
        "600008AA4",
        "200030BA1",
        "600025AA8",
    ],
)
def test_cusip_known_valid(cusip: str) -> None:
    assert cusip_is_valid(cusip)


@pytest.mark.parametrize(
    "cusip",
    [
        "68389X106",
        "0378331000",
        "03783310",
        "03783310A",
        "03783310a",
        "abcdefghi",
        "03783310*",
        "",
    ],
)
def test_cusip_known_invalid(cusip: str) -> None:
    assert not cusip_is_valid(cusip)


@pytest.mark.parametrize(
    "isin",
    ["US0378331005", "US5949181045", "CA600000AA15", "US200030BA15", "GB0002634946"],
)
def test_isin_known_valid(isin: str) -> None:
    assert isin_is_valid(isin)


@pytest.mark.parametrize(
    "isin",
    [
        "US0378331006",
        "US037833100",
        "US03783310055",
        "us0378331005",
        "1S0378331005",
        "US037833100A",
        "",
    ],
)
def test_isin_known_invalid(isin: str) -> None:
    assert not isin_is_valid(isin)


def test_cusip_to_isin_derives_canadian_and_american_isins() -> None:
    assert cusip_to_isin("600000AA1", "CA") == "CA600000AA15"
    assert cusip_to_isin("200030BA1", "US") == "US200030BA15"
    assert cusip_to_isin("600000AA1", "ca") == "CA600000AA15"


@pytest.mark.parametrize("country", ["GB", "DE", "JP", "", "XX"])
def test_cusip_to_isin_rejects_other_countries(country: str) -> None:
    assert cusip_to_isin("600000AA1", country) is None


@pytest.mark.parametrize("cusip", ["68389X106", "600000AA", ""])
def test_cusip_to_isin_rejects_invalid_cusip(cusip: str) -> None:
    assert cusip_to_isin(cusip, "CA") is None


_CUSIP_BASES = st.text(alphabet=_CUSIP_ALPHABET, min_size=8, max_size=8)
_CUSIP_BASES_ISIN = st.text(alphabet=_ISIN_ALPHABET, min_size=8, max_size=8)


@given(_CUSIP_BASES_ISIN)
def test_every_valid_cusip_converts_to_a_valid_isin(base8: str) -> None:
    cusip = base8 + _cusip_check(base8)
    assert cusip_is_valid(cusip)
    for country in ("CA", "US"):
        isin = cusip_to_isin(cusip, country)
        assert isin is not None
        assert isin.startswith(country)
        assert isin[2:11] == cusip
        assert isin_is_valid(isin)


@pytest.mark.parametrize("base8", ["600000*@", "200000#@", "000000*#"])
def test_cusip_with_private_placement_markers_cannot_derive_an_isin(base8: str) -> None:
    cusip = base8 + _cusip_check(base8)
    assert cusip_is_valid(cusip)
    assert cusip_to_isin(cusip, "CA") is None
    assert cusip_to_isin(cusip, "US") is None


@given(_CUSIP_BASES, st.integers(min_value=0, max_value=8))
def test_corrupting_one_cusip_character_invalidates_it(base8: str, position: int) -> None:
    cusip = base8 + _cusip_check(base8)
    replacement = _CUSIP_ALPHABET[(_CUSIP_ALPHABET.index(cusip[position]) + 1) % 39]
    corrupted = cusip[:position] + replacement + cusip[position + 1 :]
    assert not cusip_is_valid(corrupted)


@given(
    st.text(alphabet=_ISIN_ALPHABET, min_size=9, max_size=9),
    st.integers(min_value=0, max_value=11),
)
def test_corrupting_one_isin_character_invalidates_it(nsin: str, position: int) -> None:
    payload = "CA" + nsin
    isin = payload + _isin_check(payload)
    assert isin_is_valid(isin)
    replacement = _ISIN_ALPHABET[(_ISIN_ALPHABET.index(isin[position]) + 1) % 36]
    corrupted = isin[:position] + replacement + isin[position + 1 :]
    assert not isin_is_valid(corrupted)


def _csv_identifiers(path: Path) -> list[tuple[str | None, str | None]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    header_index = next(index for index, row in enumerate(rows) if row and row[0] == "Ticker")
    header = rows[header_index]
    isin_column = header.index("ISIN")
    cusip_column = header.index("CUSIP") if "CUSIP" in header else None
    return [
        (
            row[isin_column] or None,
            None if cusip_column is None else row[cusip_column] or None,
        )
        for row in rows[header_index + 1 :]
        if row
    ]


def _xlsx_identifiers(path: Path) -> list[tuple[str | None, str | None]]:
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    rows = [tuple(row) for row in workbook.worksheets[0].iter_rows(values_only=True)]
    header_index = next(
        index for index, row in enumerate(rows) if row and row[0] == "Security Name"
    )
    header = rows[header_index]
    isin_column = header.index("ISIN")
    cusip_column = header.index("CUSIP")
    return [
        (row[isin_column], row[cusip_column]) for row in rows[header_index + 1 :] if row and row[0]
    ]


def _json_identifiers(path: Path) -> list[tuple[str | None, str | None]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    holdings = payload["portfolio"]["holdings"]
    return [(holding["isin"], holding["cusip"]) for holding in holdings]


def _fixture_identifiers() -> list[tuple[str | None, str | None]]:
    pairs: list[tuple[str | None, str | None]] = []
    for path in sorted(_FIXTURES.rglob("*")):
        if path.suffix == ".csv":
            pairs.extend(_csv_identifiers(path))
        elif path.suffix == ".xlsx":
            pairs.extend(_xlsx_identifiers(path))
        elif path.suffix == ".json" and not path.name.endswith(".meta.json"):
            pairs.extend(_json_identifiers(path))
    return pairs


def test_every_synthetic_fixture_identifier_validates() -> None:
    pairs = _fixture_identifiers()
    identifiers = [value for pair in pairs for value in pair if value]
    assert len(identifiers) >= 40
    for isin, cusip in pairs:
        if isin:
            assert isin_is_valid(isin), isin
        if cusip:
            assert cusip_is_valid(cusip), cusip


def test_synd_duplicate_cusip_derives_the_synd_isin() -> None:
    assert cusip_to_isin("600000AA1", "CA") == "CA600000AA15"


@pytest.mark.parametrize(
    ("label", "mic"),
    [
        ("TSX", "XTSE"),
        ("tsx", "XTSE"),
        ("Toronto Stock Exchange", "XTSE"),
        ("XTSE", "XTSE"),
        ("CT", "XTSE"),
        ("TSE", "XTSE"),
        ("TSXV", "XTSX"),
        ("TSX Venture", "XTSX"),
        ("XTSX", "XTSX"),
        ("CV", "XTSX"),
        ("NEO", "NEOE"),
        ("Cboe Canada", "NEOE"),
        ("NEOE", "NEOE"),
        ("CSE", "XCNQ"),
        ("NYSE", "XNYS"),
        ("New York Stock Exchange", "XNYS"),
        ("XNYS", "XNYS"),
        ("UN", "XNYS"),
        ("NASDAQ", "XNAS"),
        ("XNAS", "XNAS"),
        ("UW", "XNAS"),
        ("UQ", "XNAS"),
        ("NYSE Arca", "ARCX"),
        ("ARCX", "ARCX"),
        ("UP", "ARCX"),
        ("Cboe BZX", "BATS"),
        ("BATS", "BATS"),
        ("LSE", "XLON"),
        ("XLON", "XLON"),
    ],
)
def test_normalize_exchange_known_labels(label: str, mic: str) -> None:
    assert normalize_exchange(label) == mic


@pytest.mark.parametrize(
    "label",
    [None, "", "   ", "Frankfurt Stock Exchange", "XHKG", "CBOE", "TSX 60", "VENTURE"],
)
def test_normalize_exchange_unknown_labels_are_none(label: str | None) -> None:
    assert normalize_exchange(label) is None


def test_normalize_exchange_strips_and_collapses_whitespace() -> None:
    assert normalize_exchange("  toronto   stock exchange ") == "XTSE"


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("ry", "RY"),
        ("  RY  ", "RY"),
        ("RY.TO", "RY"),
        ("RY-T", "RY"),
        ("RY CN", "RY"),
        ("RY US", "RY"),
        ("SHOP.V", "SHOP"),
        ("ABC.NE", "ABC"),
        ("ABC.CN", "ABC"),
        ("ABC-TSX", "ABC"),
        ("BRK-B", "BRK.B"),
        ("BRK.B", "BRK.B"),
        ("BBD.B", "BBD.B"),
        ("RY-B.TO", "RY.B"),
        ("CN", "CN"),
        ("RY.", "RY"),
    ],
)
def test_normalize_ticker(ticker: str, expected: str) -> None:
    assert normalize_ticker(ticker) == expected


@pytest.mark.parametrize("ticker", [None, "", "   ", ".TO", "-T"])
def test_normalize_ticker_empty_or_suffix_only_is_none(ticker: str | None) -> None:
    assert normalize_ticker(ticker) is None
