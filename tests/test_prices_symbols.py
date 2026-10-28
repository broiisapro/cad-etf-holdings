"""symbol_for: US-only mapping from a security row to a price-source symbol."""

from __future__ import annotations

import pytest

from etfh.prices import symbol_for


@pytest.mark.parametrize(
    ("security_id", "security_ticker", "exchange", "expected"),
    [
        (None, None, None, None),
        ("ISIN:CA0000000001", None, None, None),
        ("CUSIP:000000001", None, None, None),
        (None, "AAPL", "XNAS", "AAPL"),
        ("TKR:XNAS:AAPL", None, None, "AAPL"),
        (None, "BRK.B", "XNYS", "BRK-B"),
        (None, "BRK-B", "XNYS", "BRK-B"),
        ("TKR:XNYS:BRK.B", None, None, "BRK-B"),
        (None, "aapl", "NASDAQ GS", "AAPL"),
        (None, "SPY", "NYSE ARCA", "SPY"),
        (None, "IBM", "NYSE", "IBM"),
        (None, "AAPL", "XASE", "AAPL"),
        (None, "AAPL", "BATS", "AAPL"),
        (None, "AAPL", "TSX", None),
        (None, "AAPL", "XTSE", None),
        ("TKR:XTSE:RY", None, None, None),
        ("TKR:XTSE:RY", None, "XTSE", None),
        (None, "AAPL", "US", None),
        (None, "AAPL", None, None),
        (None, None, "XNAS", None),
        ("ISIN:CA0000000001", "AAPL", "TSX", None),
    ],
)
def test_symbol_for_is_us_only_and_total(
    security_id: str | None, security_ticker: str | None, exchange: str | None, expected: str | None
) -> None:
    assert symbol_for(security_id, security_ticker, exchange) == expected
