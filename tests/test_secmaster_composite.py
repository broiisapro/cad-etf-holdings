"""Composite-code tickers and the shared default id space (issue #103, D55, D57).

Parsers for issuers without a separate exchange column hand the security
master a Bloomberg-style ``"<TICKER> <CODE>"`` with a null ``exchange``. A
single-venue code becomes a MIC; a country-composite code (``US``, ``CN``,
...) is used for FIGI lookups only, so the row stays unmatched without one.
Only a trailing *known* code is split: a ticker that legitimately contains
spaces is left whole.
"""

from __future__ import annotations

from datetime import date

import pytest

from etfh.secmaster import Override, SecurityMaster
from tests._secmaster_helpers import holdings_frame

REVIEWED_ON = date(2026, 10, 17)


def _row(
    ticker: str | None,
    *,
    exchange: str | None = None,
    name: str = "Example Holdings Inc.",
    fund_ticker: str = "ESPX",
    country: str = "CA",
    currency: str = "CAD",
) -> dict[str, object]:
    return {
        "fund_ticker": fund_ticker,
        "security_name": name,
        "security_ticker": ticker,
        "exchange": exchange,
        "asset_class": "equity",
        "country": country,
        "currency": currency,
    }


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("NWM CT", "TKR:XTSE:NWM"),
        ("FXN UW", "TKR:XNAS:FXN"),
        ("RY UN", "TKR:XNYS:RY"),
        ("ENB LN", "TKR:XLON:ENB"),
    ],
)
def test_single_exchange_code_suffix_becomes_a_mic(ticker: str, expected: str) -> None:
    assert SecurityMaster().assign(holdings_frame([_row(ticker)]))["security_id"].to_list() == [
        expected
    ]


@pytest.mark.parametrize("ticker", ["AAPL US", "RY CN", "SHOP CN", "7203 JP"])
def test_composite_code_suffix_is_not_a_venue_and_stays_unmatched(ticker: str) -> None:
    assert SecurityMaster().assign(holdings_frame([_row(ticker)]))["security_id"].to_list() == [
        None
    ]


def test_the_same_ticker_with_a_composite_and_a_single_venue_code_is_not_merged() -> None:
    frame = holdings_frame(
        [
            _row("AAPL US", name="Apple Inc.", country="US", currency="USD"),
            _row("AAPL UW", name="Apple Inc.", country="US", currency="USD"),
        ]
    )
    assert SecurityMaster().assign(frame)["security_id"].to_list() == [None, "TKR:XNAS:AAPL"]


@pytest.mark.parametrize(
    "ticker",
    [
        "NORTHWIND MATERIALS",
        "NWM XYZ",
        "VANGUARD FUND A",
        "CASH",
        "US",
    ],
)
def test_a_trailing_unknown_token_is_not_split(ticker: str) -> None:
    assert SecurityMaster().assign(holdings_frame([_row(ticker)]))["security_id"].to_list() == [
        None
    ]


def test_a_separate_exchange_wins_over_a_suffix() -> None:
    """With an exchange column the suffix is redundant, never a second venue."""
    frame = holdings_frame([_row("ABC US", exchange="XTSE")])
    assert SecurityMaster().assign(frame)["security_id"].to_list() == ["TKR:XTSE:ABC"]


def test_a_split_ticker_still_matches_a_ticker_override() -> None:
    override = Override(
        match_type="ticker",
        match_value="XTSE:NWM",
        security_id="ISIN:CA600000AA15",
        reviewer="Reviewer",
        reviewed_on=REVIEWED_ON,
    )
    frame = holdings_frame([_row("NWM CT")])
    assert SecurityMaster([override]).assign(frame)["security_id"].to_list() == [
        "ISIN:CA600000AA15"
    ]


def test_split_tickers_preserve_row_order() -> None:
    master = SecurityMaster()
    frame = holdings_frame([_row("NWM CT"), _row("AAPL US"), _row("FXN UW")])
    forward = master.assign(frame)["security_id"].to_list()
    backward = master.assign(frame.reverse())["security_id"].to_list()
    assert forward == ["TKR:XTSE:NWM", None, "TKR:XNAS:FXN"]
    assert backward == list(reversed(forward))


def test_default_ids_are_identical_to_the_pre_d57_resolution() -> None:
    """The no-FIGI output is pinned to the vector the old resolver produced.

    Every resolution path is present; the expected list was recorded from the
    implementation on ``main`` before D57/D55, so any change to the default
    output fails here.
    """
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNX",
                "security_name": "By ISIN Inc.",
                "isin": "CA600000AA15",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "By CUSIP Corp.",
                "cusip": "037833100",
                "asset_class": "equity",
                "country": "US",
                "currency": "USD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "Foreign CUSIP Ltd.",
                "cusip": "037833100",
                "asset_class": "equity",
                "country": "GB",
                "currency": "GBP",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "By MIC Inc.",
                "security_ticker": "NWM",
                "exchange": "XTSE",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "By Label Inc.",
                "security_ticker": "FXN",
                "exchange": "NASDAQ",
                "asset_class": "equity",
                "country": "US",
                "currency": "USD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "Cash",
                "asset_class": "cash",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "Cash",
                "asset_class": "cash",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "CAD/USD Forward",
                "asset_class": "fx",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "Northwind Call",
                "security_ticker": "NWMX 255",
                "asset_class": "derivative",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "Nowhere Ltd.",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "Composite Code Inc.",
                "security_ticker": "AAPL US",
                "asset_class": "equity",
                "country": "US",
                "currency": "USD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "Unknown Code Inc.",
                "security_ticker": "ABC XYZ",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "Override Name Inc.",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
        ]
    )
    expected = [
        "ISIN:CA600000AA15",
        "ISIN:US0378331005",
        "CUSIP:037833100",
        "TKR:XTSE:NWM",
        "TKR:XNAS:FXN",
        "CASH:CAD",
        "CASH:XXX",
        "DERIV:SYNX:CAD USD FORWARD",
        "DERIV:SYNX:NWMX 255",
        None,
        None,
        None,
        None,
    ]
    assert SecurityMaster().assign(frame)["security_id"].to_list() == expected

    override = Override(
        match_type="name",
        match_value="SYNX|Override Name Inc.",
        security_id="ISIN:CA600000AA99",
        reviewer="Reviewer",
        reviewed_on=REVIEWED_ON,
    )
    with_override = SecurityMaster([override]).assign(frame)["security_id"].to_list()
    assert with_override == [*expected[:-1], "ISIN:CA600000AA99"]
