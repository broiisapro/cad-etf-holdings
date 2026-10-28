"""Tests for point-in-time large-addition events."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import polars as pl
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from etfh.build import build
from etfh.events import EVENT_COLUMNS, large_additions

from ._events_helpers import (
    D0,
    D1,
    D2,
    K0,
    K1,
    K2,
    K3,
    Q1,
    Q2,
    holding,
    make_con,
    portfolio,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RAW_SYNTHETIC = FIXTURES / "raw_synthetic"

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


# --------------------------------------------------------------------------- #
# Non-security and unmatched rows
# --------------------------------------------------------------------------- #


def test_cash_derivative_balancing_and_unmatched_rows_never_become_events() -> None:
    con = make_con(
        [
            portfolio(
                "XFUND",
                D0,
                K0,
                [
                    holding("ISIN:A", 0.4),
                    holding("CASH:CAD", 0.06, asset_class="cash"),
                    holding("DERIV:OLD", 0.04, asset_class="derivative"),
                    holding(
                        "TKR:XTSE:OTHER",
                        0.05,
                        name="Other Assets less Liabilities",
                        asset_class="other",
                    ),
                    holding(None, 0.05, name="Unmatched Old"),
                    holding("FX:USD", 0.05, asset_class="fx"),
                    holding("ISIN:B", 0.35),
                ],
            ),
            portfolio(
                "XFUND",
                D1,
                K1,
                [
                    holding("ISIN:A", 0.4),
                    holding("CASH:CAD", 0.06, asset_class="cash"),
                    holding("CASH:USD", 0.05, asset_class="cash"),
                    holding("DERIV:NEW", 0.04, asset_class="derivative"),
                    holding(
                        "TKR:XTSE:OTHER",
                        0.05,
                        name="Other Assets less Liabilities",
                        asset_class="other",
                    ),
                    holding(
                        "TKR:XTSE:NETOTHER",
                        0.01,
                        name="Net Other Assets (Liabilities)",
                        asset_class="other",
                    ),
                    holding(None, 0.15, name="Unmatched New"),
                    holding("FX:USD", 0.05, asset_class="fx"),
                    holding("ISIN:B", 0.35),
                    holding("ISIN:E", 0.02),
                ],
            ),
        ]
    )
    try:
        events = large_additions(con)
    finally:
        con.close()

    assert events.height == 1
    assert events["security_id"].to_list() == ["ISIN:E"]


def test_repeated_lines_for_one_security_are_summed_before_comparing() -> None:
    con = make_con(
        [
            portfolio("XFUND", D0, K0, [holding("ISIN:A", 0.97), holding("ISIN:B", 0.03)]),
            # Same identifier on two lines: 0.006 + 0.006 = 0.012 together.
            portfolio(
                "XFUND",
                D1,
                K1,
                [
                    holding("ISIN:A", 0.97),
                    holding("ISIN:B", 0.018),
                    holding("ISIN:C", 0.006),
                    holding("ISIN:C", 0.006),
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
    assert row["security_id"] == "ISIN:C"
    assert row["weight_after"] == pytest.approx(0.012)


# --------------------------------------------------------------------------- #
# Quarterly mode
# --------------------------------------------------------------------------- #


def _quarterly_fixture() -> list[dict[str, object]]:
    return [
        portfolio(
            "QFUND",
            Q1,
            datetime(2026, 7, 2, 15, 0, tzinfo=UTC),
            [holding("ISIN:A", 0.4), holding("ISIN:X", 0.1)],
            source_id="quarterly",
        ),
        portfolio(
            "QFUND",
            Q2,
            datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
            [holding("ISIN:A", 0.5), holding("ISIN:Y", 0.15)],
            source_id="quarterly",
        ),
        portfolio("QFUND", D0, K0, [holding("ISIN:A", 0.5)]),
        portfolio(
            "QFUND",
            D1,
            K1,
            [holding("ISIN:A", 0.45), holding("ISIN:Z", 0.05)],
        ),
    ]


def test_quarterly_versions_are_ignored_by_default() -> None:
    con = make_con(_quarterly_fixture())
    try:
        events = large_additions(con)
    finally:
        con.close()

    assert events["security_id"].to_list() == ["ISIN:Z"]
    assert set(events["source_kind"].to_list()) == {"daily"}


def test_quarterly_mode_adds_only_relative_to_the_previous_quarterly_list() -> None:
    con = make_con(_quarterly_fixture())
    try:
        events = large_additions(con, include_quarterly=True)
    finally:
        con.close()

    quarterly = events.filter(pl.col("source_kind") == "quarterly")
    # Y is new to the Q2 list; X is missing from it (never a drop) and A's
    # weight change between partial lists is never an increase.
    assert quarterly["security_id"].to_list() == ["ISIN:Y"]
    row = quarterly.row(0, named=True)
    assert row["event_type"] == "add"
    assert row["portfolio_date"] == Q2
    assert row["weight_after"] == pytest.approx(0.15)
    # The daily chain is unchanged and never compares against quarterly rows.
    daily = events.filter(pl.col("source_kind") == "daily")
    assert daily["security_id"].to_list() == ["ISIN:Z"]


def test_quarterly_only_fund_has_no_events_without_a_previous_list() -> None:
    con = make_con(
        [
            portfolio(
                "QFUND",
                Q2,
                datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
                [holding("ISIN:A", 0.5), holding("ISIN:Y", 0.15)],
                source_id="quarterly",
            )
        ]
    )
    try:
        assert large_additions(con, include_quarterly=True).is_empty()
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Filters, dates, order and dtypes
# --------------------------------------------------------------------------- #


def test_known_date_uses_toronto_not_utc() -> None:
    con = make_con(
        [
            portfolio("XFUND", D0, K0, [holding("ISIN:A", 1.0)]),
            portfolio(
                "XFUND",
                D1,
                # 02:00Z on 3 October is still 2 October in Toronto.
                datetime(2026, 10, 3, 2, 0, tzinfo=UTC),
                [holding("ISIN:A", 0.9), holding("ISIN:B", 0.1)],
            ),
        ]
    )
    try:
        events = large_additions(con)
    finally:
        con.close()

    assert events["known_date"].to_list() == [date(2026, 10, 2)]


def test_start_and_end_filter_known_date_inclusively() -> None:
    con = make_con(
        [
            portfolio("XFUND", D0, K0, [holding("ISIN:A", 1.0)]),
            portfolio("XFUND", D1, K1, [holding("ISIN:A", 0.9), holding("ISIN:B", 0.1)]),
            portfolio(
                "XFUND",
                D2,
                K2,
                [holding("ISIN:A", 0.9), holding("ISIN:B", 0.05), holding("ISIN:C", 0.05)],
            ),
        ]
    )
    try:
        all_events = large_additions(con)
        after = large_additions(con, start=D2)
        before = large_additions(con, end=D1)
        exact = large_additions(con, start=D1, end=D1)
    finally:
        con.close()

    assert all_events["security_id"].to_list() == ["ISIN:B", "ISIN:C"]
    assert after["security_id"].to_list() == ["ISIN:C"]
    assert before["security_id"].to_list() == ["ISIN:B"]
    assert exact["security_id"].to_list() == ["ISIN:B"]


def test_result_is_sorted_and_typed() -> None:
    con = make_con(
        [
            portfolio("ZFUND", D0, K0, [holding("ISIN:A", 1.0)]),
            portfolio(
                "ZFUND",
                D1,
                K1,
                [holding("ISIN:A", 0.8), holding("ISIN:B", 0.2, ticker="B", exchange="XTSE")],
            ),
            portfolio("AFUND", D0, K0, [holding("ISIN:A", 1.0)]),
            portfolio("AFUND", D1, K1, [holding("ISIN:A", 0.9), holding("ISIN:C", 0.1)]),
        ]
    )
    try:
        events = large_additions(con)
    finally:
        con.close()

    assert events.columns == list(EVENT_COLUMNS)
    assert events.schema == _EXPECTED_SCHEMA
    keys = list(
        zip(
            events["known_date"].to_list(),
            events["fund_ticker"].to_list(),
            events["security_id"].to_list(),
            strict=True,
        )
    )
    assert keys == sorted(keys)
    assert keys == [
        (date(2026, 10, 2), "AFUND", "ISIN:C"),
        (date(2026, 10, 2), "ZFUND", "ISIN:B"),
    ]
    ticker_row = events.filter(pl.col("security_id") == "ISIN:B").row(0, named=True)
    assert ticker_row["security_ticker"] == "B"
    assert ticker_row["exchange"] == "XTSE"


# --------------------------------------------------------------------------- #
# Hypothesis: the event set does not depend on input row order
# --------------------------------------------------------------------------- #


def _order_fixture() -> list[dict[str, object]]:
    return [
        portfolio("XFUND", D0, K0, [holding("ISIN:A", 0.6), holding("ISIN:B", 0.4)]),
        portfolio(
            "XFUND",
            D1,
            K1,
            [
                holding("ISIN:A", 0.5),
                holding("ISIN:C", 0.2),
                holding("ISIN:B", 0.2),
                holding("ISIN:D", 0.05),
                holding("CASH:CAD", 0.05, asset_class="cash"),
            ],
        ),
        portfolio(
            "XFUND",
            D1,
            K3,
            [holding("ISIN:A", 0.55), holding("ISIN:C", 0.2), holding("ISIN:D", 0.1)],
            file_version=2,
        ),
        portfolio("YFUND", D0, K0, [holding("ISIN:A", 1.0)]),
        portfolio(
            "YFUND",
            D2,
            K2,
            [holding("ISIN:A", 0.7), holding("ISIN:E", 0.3)],
        ),
        portfolio(
            "YFUND",
            Q2,
            datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
            [holding("ISIN:E", 0.2)],
            source_id="quarterly",
        ),
    ]


@given(seed=st.integers(min_value=0, max_value=10_000))
@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_events_are_invariant_to_input_row_order(seed: int) -> None:
    baseline = _order_fixture()
    con = make_con(baseline)
    try:
        expected = large_additions(con, include_quarterly=True)
    finally:
        con.close()

    rng = random.Random(seed)
    shuffled = _order_fixture()
    rng.shuffle(shuffled)
    for item in shuffled:
        rng.shuffle(item["rows"])
    con = make_con(shuffled)
    try:
        actual = large_additions(con, include_quarterly=True)
    finally:
        con.close()

    assert actual.equals(expected)


# --------------------------------------------------------------------------- #
# Committed synthetic build
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def synthetic_events(tmp_path_factory: pytest.TempPathFactory) -> pl.DataFrame:
    out_dir = tmp_path_factory.mktemp("events-synthetic")
    result = build(RAW_SYNTHETIC, out_dir)
    con = duckdb.connect(str(result.db_path), read_only=True)
    try:
        return large_additions(con)
    finally:
        con.close()


def test_synthetic_build_event_count(synthetic_events: pl.DataFrame) -> None:
    """Recorded count from the committed synthetic build (update deliberately)."""
    assert synthetic_events.height == 39
    assert synthetic_events.schema == _EXPECTED_SCHEMA
    assert set(synthetic_events["event_type"].to_list()) <= {"add", "increase"}
