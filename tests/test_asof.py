"""Tests for the America/Toronto as-of cutoff (D30)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from etfh.asof import cutoff_utc


def test_cutoff_is_midnight_next_day_in_toronto_before_dst_fallback() -> None:
    assert cutoff_utc(date(2026, 10, 31)) == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)


def test_cutoff_is_midnight_next_day_in_toronto_after_dst_fallback() -> None:
    assert cutoff_utc(date(2026, 11, 2)) == datetime(2026, 11, 3, 5, 0, tzinfo=UTC)


def test_cutoff_summer_midnight_is_four_hours_behind_utc() -> None:
    assert cutoff_utc(date(2026, 7, 1)) == datetime(2026, 7, 2, 4, 0, tzinfo=UTC)


def test_cutoff_winter_midnight_is_five_hours_behind_utc() -> None:
    assert cutoff_utc(date(2026, 1, 15)) == datetime(2026, 1, 16, 5, 0, tzinfo=UTC)


def test_cutoff_is_always_utc_and_at_midnight() -> None:
    cutoff = cutoff_utc(date(2026, 10, 30))
    assert cutoff.tzinfo is UTC
    assert (cutoff.hour, cutoff.minute, cutoff.second) == (4, 0, 0)


def test_dst_fallback_makes_consecutive_cutoffs_more_than_a_day_apart() -> None:
    before = cutoff_utc(date(2026, 10, 31))
    after = cutoff_utc(date(2026, 11, 1))
    assert after - before == timedelta(days=1, hours=1)


@pytest.mark.parametrize(
    ("day", "expected_utc_hour"),
    [
        (date(2026, 3, 7), 5),
        (date(2026, 3, 8), 4),
        (date(2026, 3, 9), 4),
    ],
)
def test_spring_forward_cutoffs(day: date, expected_utc_hour: int) -> None:
    cutoff = cutoff_utc(day)
    expected = datetime(day.year, day.month, day.day, expected_utc_hour, 0, tzinfo=UTC) + timedelta(
        days=1
    )
    assert cutoff == expected
    assert cutoff.tzinfo is UTC
