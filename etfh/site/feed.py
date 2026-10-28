"""Atom feed for the weekly digest site.

One entry per week, newest first. Entry ids are stable whether or not the site
knows its public URL: ``{base_url}weeks/<week_end>.html`` when ``base_url`` is
given, ``urn:etfh:week:<week_end>`` otherwise. The feed's ``updated`` stamp is
the newest week's ``known_by`` (the point-in-time cutoff), never the wall
clock, so rebuilding the same documents produces byte-identical XML.

Entry summaries carry aggregates only (fund count, additions, drops, weight
changes), which the publication policy allows for every fund (D79).
"""

from __future__ import annotations

from collections.abc import Sequence
from xml.sax.saxutils import escape, quoteattr

from etfh.site.format import format_week_range
from etfh.site.model import WeeklyDigest

__all__ = ["render_feed"]

_FEED_ID = "urn:etfh:feed"
_ENTRY_ID = "urn:etfh:week:{}"


def render_feed(
    digests: Sequence[WeeklyDigest], *, site_title: str, base_url: str | None = None
) -> str:
    """Serialise ``digests`` (newest first) as an Atom 1.0 document."""
    updated = max(digest.known_by for digest in digests)
    feed_id = f"{base_url}feed.xml" if base_url is not None else _FEED_ID
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="en">',
        f"<title>{escape(site_title)}</title>",
        f"<id>{escape(feed_id)}</id>",
        f"<updated>{escape(updated.isoformat())}</updated>",
        "<author>",
        "<name>cad-etf-holdings</name>",
        "</author>",
    ]
    if base_url is not None:
        lines.append(f'<link rel="self" type="application/atom+xml" href={quoteattr(feed_id)}/>')
        lines.append(f'<link rel="alternate" type="text/html" href={quoteattr(base_url)}/>')
    for digest in digests:
        lines.extend(_entry(digest, base_url=base_url))
    lines.append("</feed>")
    return "\n".join(lines) + "\n"


def _entry(digest: WeeklyDigest, *, base_url: str | None) -> list[str]:
    week_end = digest.week_end.isoformat()
    entry_id = (
        f"{base_url}weeks/{week_end}.html" if base_url is not None else _ENTRY_ID.format(week_end)
    )
    lines = [
        "<entry>",
        f"<title>Week ending {week_end}</title>",
        f"<id>{escape(entry_id)}</id>",
        f"<updated>{escape(digest.known_by.isoformat())}</updated>",
    ]
    if base_url is not None:
        lines.append(f'<link rel="alternate" type="text/html" href={quoteattr(entry_id)}/>')
    lines.append(f"<summary>{escape(_summary(digest))}</summary>")
    lines.append("</entry>")
    return lines


def _summary(digest: WeeklyDigest) -> str:
    funds = digest.funds
    adds = sum(fund.adds for fund in funds)
    drops = sum(fund.drops for fund in funds)
    weight_changes = sum(fund.weight_changes for fund in funds)
    fund_word = "fund" if len(funds) == 1 else "funds"
    add_word = "addition" if adds == 1 else "additions"
    drop_word = "drop" if drops == 1 else "drops"
    change_word = "weight change" if weight_changes == 1 else "weight changes"
    return (
        f"Week {format_week_range(digest.week_start, digest.week_end)}: "
        f"{len(funds)} {fund_word} tracked, {adds} {add_word}, {drops} {drop_word}, "
        f"{weight_changes} {change_word}."
    )
