"""Tests for point-in-time large-addition events."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from etfh.events import large_additions

from ._events_helpers import (
    D0,
    D1,
    D2,
    K0,
    K1,
    K2,
    K3,
    holding,
    make_con,
    portfolio,
)

_EXPECTED_SCHEMA = {
    "fund_ticker": pl.String,
    "security_id": pl.String,
    "security_name": pl.String,
    "security_ticker": pl.String,
    "exchange": pl.String,
    "event_type": pl.String,
    "portfolio_date": pl.Date,
    "known_date": pl.Date,
    "weight_before": pl.Float64,
    "weight_after": pl.Float64,
    "delta": pl.Float64,
    "source_kind": pl.String,
}

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


# --------------------------------------------------------------------------- #
# Version chains and republished files
# --------------------------------------------------------------------------- #


def test_first_portfolio_has_no_events() -> None:
    con = make_con([portfolio("XFUND", D0, K0, [holding("ISIN:A", 0.6), holding("ISIN:B", 0.4)])])
    try:
        events = large_additions(con)
    finally:
        con.close()

    assert events.is_empty()
    assert events.schema == _EXPECTED_SCHEMA


def test_republished_version_does_not_duplicate_events() -> None:
    original = [holding("ISIN:A", 0.5), holding("ISIN:B", 0.4), holding("ISIN:C", 0.1)]
    con = make_con(
        [
            portfolio("XFUND", D0, K0, [holding("ISIN:A", 0.6), holding("ISIN:B", 0.4)]),
            portfolio("XFUND", D1, K1, original),
            # Same portfolio, republished: C is not an add again.
            portfolio("XFUND", D1, K1 + timedelta(hours=2), original, file_version=2),
            # A later correction raises C: an increase against the version it
            # replaces, not a second add.
            portfolio(
                "XFUND",
                D1,
                K3,
                [holding("ISIN:A", 0.5), holding("ISIN:B", 0.4), holding("ISIN:C", 0.12)],
                file_version=3,
            ),
        ]
    )
    try:
        events = large_additions(con)
    finally:
        con.close()

    assert events.height == 2
    first, second = events.iter_rows(named=True)
    assert (first["security_id"], first["event_type"], first["known_date"]) == (
        "ISIN:C",
        "add",
        date(2026, 10, 2),
    )
    assert (second["security_id"], second["event_type"], second["known_date"]) == (
        "ISIN:C",
        "increase",
        date(2026, 10, 6),
    )
    assert second["weight_before"] == pytest.approx(0.1)
    assert second["weight_after"] == pytest.approx(0.12)


def test_later_portfolio_compares_against_the_republished_version() -> None:
    con = make_con(
        [
            portfolio(
                "XFUND",
                D0,
                K0,
                [holding("ISIN:A", 0.5), holding("ISIN:B", 0.5)],
            ),
            # The original D1 carries C, the republish replaces C with D.
            portfolio(
                "XFUND",
                D1,
                K1,
                [holding("ISIN:A", 0.5), holding("ISIN:C", 0.5)],
            ),
            portfolio(
                "XFUND",
                D1,
                K2,
                [holding("ISIN:A", 0.5), holding("ISIN:D", 0.5)],
                file_version=2,
            ),
            # D2 brings C back: it is new relative to the republished D1.
            portfolio(
                "XFUND",
                D2,
                K3,
                [holding("ISIN:A", 0.5), holding("ISIN:C", 0.5)],
            ),
        ]
    )
    try:
        events = large_additions(con)
    finally:
        con.close()

    by_key = {
        (row["security_id"], row["portfolio_date"]): row for row in events.iter_rows(named=True)
    }
    assert set(by_key) == {("ISIN:C", D1), ("ISIN:D", D1), ("ISIN:C", D2)}
    assert by_key[("ISIN:C", D2)]["event_type"] == "add"


def test_excluded_version_never_produces_events() -> None:
    con = make_con(
        [
            portfolio("XFUND", D0, K0, [holding("ISIN:A", 1.0)]),
            portfolio(
                "XFUND",
                D1,
                K1,
                [holding("ISIN:A", 0.9), holding("ISIN:B", 0.1)],
                status="excluded",
            ),
            portfolio("XFUND", D2, K2, [holding("ISIN:A", 0.9), holding("ISIN:C", 0.1)]),
        ]
    )
    try:
        events = large_additions(con)
    finally:
        con.close()

    assert events["security_id"].to_list() == ["ISIN:C"]
