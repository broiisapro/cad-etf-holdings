"""Week windows and canonical date strings for the digest document.

A week runs Friday to Friday (interfaces-08 §3): ``week_end`` is the Friday the
digest is built for and ``week_start`` is seven days earlier. ``known_by`` is
the D30 cutoff for ``week_end`` expressed as an ISO-8601 UTC timestamp, so a
reader can tell exactly which file versions the document could see.
"""

from __future__ import annotations

from datetime import date, timedelta

from etfh.asof import cutoff_utc

__all__ = ["FRIDAY", "default_week_end", "iso_date", "known_by", "week_start"]

#: ``date.weekday()`` of a Friday, the day every digest week ends on.
FRIDAY = 4


def default_week_end(today: date) -> date:
    """Return the most recent Friday on or before ``today`` (interfaces-08 §3)."""
    return today - timedelta(days=(today.weekday() - FRIDAY) % 7)


def week_start(week_end: date) -> date:
    """Return the first day of the week that ends on ``week_end``."""
    return week_end - timedelta(days=7)


def iso_date(day: date) -> str:
    """Return ``day`` as ``YYYY-MM-DD``, the only date form the schema accepts."""
    return day.isoformat()


def known_by(week_end: date) -> str:
    """Return :func:`etfh.asof.cutoff_utc` for ``week_end`` as an ISO timestamp."""
    return cutoff_utc(week_end).isoformat()
