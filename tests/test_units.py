"""Tests for weight-unit normalization and numeric parsing."""

from __future__ import annotations

import polars as pl
import pytest
from hypothesis import given
from hypothesis import strategies as st

from etfh.units import WeightUnit, check_unit, normalize_weights, parse_number


class TestParseNumber:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("5.2", 5.2),
            (" 5.2 ", 5.2),
            ("5.2%", 5.2),
            (" 5.2 % ", 5.2),
            ("1,234.56", 1234.56),
            ("-2.5", -2.5),
            ("−2.5", -2.5),
            ("(1.5)", -1.5),
            ("(1,234.5)", -1234.5),
            ("+3", 3.0),
            ("0", 0.0),
            ("0.052", 0.052),
            (None, None),
            ("", None),
            ("   ", None),
            ("-", None),
            ("−", None),
            ("—", None),
            ("N/A", None),
            ("n/a", None),
            ("none", None),
            ("abc", None),
            ("1.2.3", None),
            ("inf", None),
            ("nan", None),
        ],
    )
    def test_cases(self, text: str | None, expected: float | None) -> None:
        assert parse_number(text) == expected

    def test_comma_grouping_round_trip(self) -> None:
        assert parse_number("123,456,789.25") == 123456789.25


@given(st.floats(allow_nan=False, allow_infinity=False, min_value=-1e12, max_value=1e12))
def test_parse_number_round_trips_formatted_floats(value: float) -> None:
    text = format(value, ".12g")
    assert parse_number(text) == float(text)


@given(st.floats(allow_nan=False, allow_infinity=False, min_value=-1e12, max_value=1e12))
def test_parse_number_parentheses_negate(value: float) -> None:
    text = format(value, ".12g")
    assert parse_number(f"({text})") == -float(text)


def test_weights_percent_vs_fraction_normalized() -> None:
    percent = normalize_weights(pl.Series([5.2, 3.0, 91.8]), WeightUnit.percent)
    fraction = normalize_weights(pl.Series([0.052, 0.03, 0.918]), WeightUnit.fraction)
    assert percent.to_list() == pytest.approx(fraction.to_list())
    assert percent.to_list() == pytest.approx([0.052, 0.03, 0.918])


class TestNormalizeWeights:
    def test_fraction_passthrough(self) -> None:
        values = pl.Series([0.5, 0.5])
        assert normalize_weights(values, WeightUnit.fraction).to_list() == [0.5, 0.5]

    def test_none_values_preserved(self) -> None:
        series = normalize_weights([50.0, None], WeightUnit.percent)
        assert series.to_list() == [0.5, None]
        assert series.dtype == pl.Float64


class TestCheckUnit:
    def test_percent_sum_consistent(self) -> None:
        result = check_unit(pl.Series([60.0, 40.0]), WeightUnit.percent)
        assert result.consistent
        assert result.total == pytest.approx(100.0)
        assert result.suggested is None

    def test_fraction_sum_consistent(self) -> None:
        result = check_unit(pl.Series([0.6, 0.4]), WeightUnit.fraction)
        assert result.consistent
        assert result.suggested is None

    def test_percent_declared_summing_to_one_is_contradiction(self) -> None:
        result = check_unit(pl.Series([0.6, 0.4]), WeightUnit.percent)
        assert not result.consistent
        assert result.total == pytest.approx(1.0)
        assert result.suggested is WeightUnit.fraction
        assert "declared unit percent" in result.detail()
        assert "fraction" in result.detail()

    def test_fraction_declared_summing_to_hundred_is_contradiction(self) -> None:
        result = check_unit(pl.Series([60.0, 40.0]), WeightUnit.fraction)
        assert not result.consistent
        assert result.suggested is WeightUnit.percent

    def test_totally_wrong_sum_has_no_suggestion(self) -> None:
        result = check_unit(pl.Series([0.3, 0.3]), WeightUnit.fraction)
        assert not result.consistent
        assert result.suggested is None

    def test_non_positive_tolerance_rejected(self) -> None:
        with pytest.raises(ValueError, match="tolerance must be positive"):
            check_unit(pl.Series([1.0]), WeightUnit.fraction, tolerance=0)

    def test_null_and_non_finite_values_ignored_in_total(self) -> None:
        values = pl.Series([0.6, None, 0.4, float("nan")])
        result = check_unit(values, WeightUnit.fraction)
        assert result.consistent
