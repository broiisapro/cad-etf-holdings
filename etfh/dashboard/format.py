"""Number formatting shared by the dashboard tabs.

Every tab shows the same shapes for the same kinds of number, so the helpers
live in one place: fractions as percentages with two decimals, counts and
amounts with thousands separators, a single hyphen for a missing value.

The signed forms match the static digest pages (``etfh/site/format.py``) so a
weekly figure reads the same in the app and on the published page: a flow or a
weight move carries an explicit ``+``/``-`` while an exact zero keeps no sign,
and negative zero never prints as ``-0.00%``.

The helpers are pure string functions, so the tables can be tested without a
Streamlit runtime.
"""

from __future__ import annotations

__all__ = [
    "MISSING",
    "format_amount",
    "format_count",
    "format_number",
    "percent",
    "signed_money",
    "signed_percent",
]

MISSING = "-"
"""The dashboard's placeholder for a value the data does not carry."""


def percent(value: float | None) -> str:
    """Format a fraction as a percentage: ``0.0312`` → ``3.12%``."""
    return MISSING if value is None else f"{_clean(value):.2%}"


def signed_percent(value: float | None) -> str:
    """Format a fraction with an explicit sign: ``0.012`` → ``+1.20%``.

    An exact zero keeps the percentage without a sign, so a flat row does not
    read as a positive move.
    """
    if value is None:
        return MISSING
    cleaned = _clean(value)
    if cleaned == 0:
        return "0.00%"
    return f"{cleaned * 100:+,.2f}%"


def format_count(value: int | None) -> str:
    """Format a count with thousands separators: ``1234`` → ``1,234``."""
    return MISSING if value is None else f"{value:,}"


def format_number(value: float | None, places: int) -> str:
    """Format a figure with thousands separators, or ``-`` when missing."""
    return MISSING if value is None else f"{value:,.{places}f}"


def format_amount(value: float | None, currency: str | None) -> str:
    """Format an AUM figure with its currency when the source states one."""
    if value is None:
        return MISSING
    text = f"{value:,.0f}"
    return f"{text} {currency}" if currency else text


def signed_money(value: float | None) -> str:
    """Format an estimated amount with an explicit sign: ``1250000.0`` → ``+1,250,000``.

    Zero has no sign. The flow estimate is always a CAD figure (D67), so no
    currency is appended; the column title carries it.
    """
    if value is None:
        return MISSING
    rounded = round(_clean(value))
    if rounded == 0:
        return "0"
    sign = "+" if rounded > 0 else "-"
    return f"{sign}{abs(rounded):,}"


def _clean(value: float) -> float:
    """Fold ``-0.0`` into ``0.0`` so it never prints as ``-0.00%``."""
    return 0.0 if value == 0 else value
