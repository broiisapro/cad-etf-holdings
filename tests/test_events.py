"""Tests for point-in-time large-addition events."""

from __future__ import annotations

from datetime import date

import pytest

from etfh.events import large_additions

from ._events_helpers import (
    D0,
    D1,
    K0,
    K1,
    holding,
    make_con,
    portfolio,
)

# --------------------------------------------------------------------------- #
# Add, increase and thresholds
# --------------------------------------------------------------------------- #


def test_add_is_absent_from_the_previous_portfolio() -> None:
    con = make_con(
        [
            portfolio("XFUND", D0, K0, [holding("ISIN:A", 0.6), holding("ISIN:B", 0.4)]),
            portfolio(
                "XFUND",
                D1,
                K1,
                [
                    holding("ISIN:A", 0.5, name="Alpha"),
                    holding("ISIN:B", 0.3),
                    holding("ISIN:C", 0.2),
                ],
            ),
        ]
    )
    try:
        events = large_additions(con)
    finally:
        con.close()

    assert events.height == 1
    row = events.row(0, named=True)
    assert row["fund_ticker"] == "XFUND"
    assert row["security_id"] == "ISIN:C"
    assert row["event_type"] == "add"
    assert row["portfolio_date"] == D1
    assert row["known_date"] == date(2026, 10, 2)
    assert row["weight_before"] == 0.0
    assert row["weight_after"] == 0.2
    assert row["delta"] == pytest.approx(0.2)
    assert row["source_kind"] == "daily"


def test_increase_needs_the_weight_change_threshold() -> None:
    con = make_con(
        [
            portfolio("XFUND", D0, K0, [holding("ISIN:A", 0.2), holding("ISIN:B", 0.8)]),
            portfolio("XFUND", D1, K1, [holding("ISIN:A", 0.3), holding("ISIN:B", 0.7)]),
        ]
    )
    try:
        events = large_additions(con)
    finally:
        con.close()

    assert events.height == 1
    row = events.row(0, named=True)
    assert row["event_type"] == "increase"
    assert row["security_id"] == "ISIN:A"
    assert row["weight_before"] == pytest.approx(0.2)
    assert row["weight_after"] == pytest.approx(0.3)
    assert row["delta"] == pytest.approx(0.1)


def test_rows_below_the_thresholds_never_become_events() -> None:
    con = make_con(
        [
            # A new security below min_weight.
            portfolio("BELOWADD", D0, K0, [holding("ISIN:A", 1.0)]),
            portfolio(
                "BELOWADD",
                D1,
                K1,
                [holding("ISIN:A", 0.995), holding("ISIN:SMALL", 0.005)],
            ),
            # A change below min_increase.
            portfolio("BELOWINC", D0, K0, [holding("ISIN:A", 0.2), holding("ISIN:B", 0.8)]),
            portfolio("BELOWINC", D1, K1, [holding("ISIN:A", 0.204), holding("ISIN:B", 0.796)]),
            # A change above min_increase but weight_after below min_weight.
            portfolio("BELOWAFTER", D0, K0, [holding("ISIN:A", 0.004), holding("ISIN:B", 0.996)]),
            portfolio("BELOWAFTER", D1, K1, [holding("ISIN:A", 0.009), holding("ISIN:B", 0.991)]),
        ]
    )
    try:
        events = large_additions(con)
    finally:
        con.close()

    assert events.is_empty()


def test_thresholds_are_inclusive() -> None:
    con = make_con(
        [
            portfolio("XFUND", D0, K0, [holding("ISIN:A", 0.5), holding("ISIN:B", 0.495)]),
            portfolio(
                "XFUND",
                D1,
                K1,
                [holding("ISIN:A", 0.505), holding("ISIN:B", 0.495), holding("ISIN:C", 0.01)],
            ),
        ]
    )
    try:
        events = large_additions(con, min_weight=0.01, min_increase=0.005)
    finally:
        con.close()

    by_type = {(row["security_id"], row["event_type"]) for row in events.iter_rows(named=True)}
    assert by_type == {("ISIN:A", "increase"), ("ISIN:C", "add")}
