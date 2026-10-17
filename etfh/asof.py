"""As-of cutoff helpers.

D30: ``--asof YYYY-MM-DD`` means "known before the start of the next calendar
day in America/Toronto". ``known_at`` is stored as a UTC timestamp, so the
cutoff is converted to UTC before comparison.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

__all__ = ["TORONTO", "cutoff_utc"]

TORONTO = ZoneInfo("America/Toronto")


def cutoff_utc(d: date) -> datetime:
    """Return the start of ``d + 1 day`` in America/Toronto, converted to UTC.

    The conversion honours daylight saving transitions: for 2026-10-31 the
    cutoff is ``2026-11-01T04:00Z`` (EDT) and for 2026-11-02 it is
    ``2026-11-03T05:00Z`` (EST, after the fall-back on 2026-11-01).
    """
    local_midnight = datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=TORONTO)
    return local_midnight.astimezone(UTC)
