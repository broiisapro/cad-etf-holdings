"""Number, date and timestamp formatting for the static digest pages.

Every helper returns the em dash ``—`` for a missing value, right-aligns
nothing itself (the tables do that with CSS) and never uses the locale, so the
same input always produces the same bytes on every machine.

Weights and turnover are stored as fractions (D21) and shown as percentages
with two decimals; estimated flows are CAD amounts with thousands separators
and an explicit sign; counts use thousands separators too.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

__all__ = [
    "EM_DASH",
    "format_count",
    "format_known_by",
    "format_percent",
    "format_signed_money",
    "format_signed_percent",
    "format_week_range",
    "iso_timestamp",
]

EM_DASH = "\u2014"


def format_count(value: int) -> str:
    """``1234`` → ``1,234``."""
    return f"{value:,}"


def format_percent(value: float | None) -> str:
    """A fraction as a percentage: ``0.0312`` → ``3.12%``; ``None`` → ``—``."""
    if value is None:
        return EM_DASH
    return f"{_clean(value) * 100:,.2f}%"


def format_signed_percent(value: float | None) -> str:
    """A fraction with an explicit sign: ``0.012`` → ``+1.20%``.

    An exact zero keeps the percentage without a sign, so a flat row does not
    read as a positive move.
    """
    if value is None:
        return EM_DASH
    cleaned = _clean(value)
    if cleaned == 0:
        return "0.00%"
    return f"{cleaned * 100:+,.2f}%"


def format_signed_money(value: float | None) -> str:
    """A CAD amount: ``1250000.0`` → ``+1,250,000``; zero has no sign."""
    if value is None:
        return EM_DASH
    rounded = round(_clean(value))
    if rounded == 0:
        return "0"
    sign = "+" if rounded > 0 else "-"
    return f"{sign}{abs(rounded):,}"


def format_week_range(week_start: date, week_end: date) -> str:
    """``2026-10-23 to 2026-10-30`` (ISO dates are unambiguous for readers)."""
    return f"{week_start.isoformat()} to {week_end.isoformat()}"


def format_known_by(known_by: datetime) -> str:
    """A UTC timestamp: ``2026-10-31 04:00 UTC``."""
    return known_by.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def iso_timestamp(known_by: datetime) -> str:
    """The machine-readable value for ``<time datetime="…">``."""
    return known_by.isoformat()


def _clean(value: float) -> float:
    """Fold ``-0.0`` into ``0.0`` so it never prints as ``-0.00%``."""
    return 0.0 if value == 0 else value
