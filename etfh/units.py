"""Weight-unit normalization and numeric parsing helpers.

Weights are stored as fractions (D6). A parser declares whether its source
column is in percent or fraction; :func:`normalize_weights` converts, and
:func:`check_unit` *reports* a contradiction between the declared unit and the
observed total instead of silently guessing.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

import polars as pl

__all__ = ["UnitCheck", "WeightUnit", "check_unit", "normalize_weights", "parse_number"]

_NULL_TOKENS = {
    "",
    "-",
    "−",
    "–",
    "—",
    "n/a",
    "n.a.",
    "na",
    "none",
    "null",
}
_DASHES = {"−": "-", "–": "-", "—": "-"}


class WeightUnit(StrEnum):
    """Unit a source states weights in."""

    percent = "percent"
    fraction = "fraction"


def parse_number(text: str | None) -> float | None:
    """Parse a display-formatted number, returning None for missing values.

    Handles thousands separators, a trailing percent sign (the sign is *not*
    applied: the unit is declared by the parser), surrounding whitespace,
    parentheses as negative, the unicode minus sign, and ``-``/empty/``N/A``
    style null tokens. Unparseable text and non-finite values return None.
    """
    if text is None:
        return None
    value = text.strip()
    if value.lower() in _NULL_TOKENS:
        return None
    negative = False
    if value.startswith("(") and value.endswith(")"):
        negative = True
        value = value[1:-1].strip()
    for dash, replacement in _DASHES.items():
        value = value.replace(dash, replacement)
    if value.endswith("%"):
        value = value[:-1].strip()
    value = value.replace(",", "")
    if not value or not any(character.isdigit() for character in value):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return -number if negative else number


def normalize_weights(
    values: pl.Series | Iterable[float | None],
    declared: WeightUnit,
) -> pl.Series:
    """Return ``values`` as fractions according to the declared unit."""
    series = pl.Series("weight", values, dtype=pl.Float64)
    if declared is WeightUnit.percent:
        series = series / 100.0
    return series.alias("weight")


@dataclass(frozen=True)
class UnitCheck:
    """Result of comparing a declared weight unit with the observed total."""

    declared: WeightUnit
    total: float
    expected: float
    tolerance: float
    consistent: bool
    suggested: WeightUnit | None

    def detail(self) -> str:
        """Human-readable explanation for a QA event."""
        other = WeightUnit.fraction if self.declared is WeightUnit.percent else WeightUnit.percent
        message = (
            f"declared unit {self.declared.value} but weights total {self.total:.4f} "
            f"(expected ~{self.expected:.2f} within {self.tolerance:.1%})"
        )
        if self.suggested is not None:
            message += f"; total matches unit {other.value}"
        return message


def check_unit(
    values: pl.Series | Iterable[float | None],
    declared: WeightUnit,
    tolerance: float = 0.01,
) -> UnitCheck:
    """Report whether the weights are consistent with the declared unit.

    Consistency means the finite non-null total is within ``tolerance``
    (relative) of 1.0 for ``fraction`` or 100.0 for ``percent``. When it is
    not, the other unit is suggested if the total matches that unit's
    expectation instead. The declared unit is never changed here.
    """
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")
    series = (
        values if isinstance(values, pl.Series) else pl.Series("weight", values, dtype=pl.Float64)
    )
    clean = series.drop_nulls()
    total = float(clean.filter(clean.is_finite()).sum())
    expected = 100.0 if declared is WeightUnit.percent else 1.0
    consistent = abs(total - expected) <= tolerance * expected
    other = WeightUnit.fraction if declared is WeightUnit.percent else WeightUnit.percent
    other_expected = 1.0 if other is WeightUnit.fraction else 100.0
    suggested = (
        other
        if not consistent and abs(total - other_expected) <= tolerance * other_expected
        else None
    )
    return UnitCheck(
        declared=declared,
        total=total,
        expected=expected,
        tolerance=tolerance,
        consistent=consistent,
        suggested=suggested,
    )
