"""HTML page assembly for the static digest site.

Pages are plain strings built from semantic elements: one ``<h1>``, sections
with ``aria-labelledby`` headings, tables with ``<caption>`` and ``scope`` on
every header cell, and a scroll wrapper so wide tables never break the layout
at 360 px. No JavaScript and nothing loaded from another origin: the
stylesheet is inlined and all links are relative except the optional
canonical URL derived from ``base_url``.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import date

from etfh.site.format import (
    EM_DASH,
    format_count,
    format_known_by,
    format_percent,
    format_signed_money,
    format_signed_percent,
    format_week_range,
    iso_timestamp,
)
from etfh.site.model import Fund, SecurityChange, WeeklyDigest
from etfh.site.style import STYLESHEET

__all__ = ["DISCLAIMER", "DEMO_BANNER", "render_archive", "render_index", "render_week_page"]

DISCLAIMER = "Derived data \u00b7 exploratory \u00b7 not investment advice"
DEMO_BANNER = "Demo data \u2014 synthetic funds only"

_STATUS_LABELS = {
    "ok": "OK",
    "new_in_window": "New in window",
    "no_change_data": "No change data",
}
_CHANGE_LABELS = {
    "add": "Add",
    "drop": "Drop",
    "weight_change": "Weight change",
}

_LEDE = (
    "What moved in tracked Canadian ETF portfolios, from the holdings files each issuer "
    "published. Only funds whose terms allow naming individual securities list them here."
)


def render_index(
    digest: WeeklyDigest,
    *,
    site_title: str,
    base_url: str | None = None,
    older: date | None = None,
) -> str:
    """The latest week's page, also served as ``index.html``."""
    body = _week_content(digest, prefix="", latest=True, older=older, newer=None)
    return _document(
        title=site_title,
        description=(
            "Weekly changes in Canadian ETF holdings. Latest week: "
            f"{format_week_range(digest.week_start, digest.week_end)}."
        ),
        canonical=base_url,
        prefix="",
        site_title=site_title,
        current="index",
        body=body,
    )


def render_week_page(
    digest: WeeklyDigest,
    *,
    site_title: str,
    base_url: str | None = None,
    prefix: str = "../",
    older: date | None = None,
    newer: date | None = None,
) -> str:
    """One archived week; ``prefix`` is the relative path back to the site root."""
    canonical = (
        f"{base_url}weeks/{digest.week_end.isoformat()}.html" if base_url is not None else None
    )
    return _document(
        title=f"Week ending {digest.week_end.isoformat()} \u2014 {site_title}",
        description=(
            f"Portfolio changes and overlap shifts for the week ending "
            f"{digest.week_end.isoformat()}."
        ),
        canonical=canonical,
        prefix=prefix,
        site_title=site_title,
        current="",
        body=_week_content(digest, prefix=prefix, latest=False, older=older, newer=newer),
    )


def render_archive(
    digests: Sequence[WeeklyDigest], *, site_title: str, base_url: str | None = None
) -> str:
    """The archive page; ``digests`` must already be sorted newest first."""
    canonical = f"{base_url}archive.html" if base_url is not None else None
    rows: list[str] = []
    for digest in digests:
        week_end = digest.week_end.isoformat()
        demo = ' <span class="tag">Demo</span>' if digest.demo else ""
        rows.append(
            "<tr>"
            f'<th scope="row"><a href="weeks/{week_end}.html">{week_end}</a>{demo}</th>'
            f"<td>{_esc(format_week_range(digest.week_start, digest.week_end))}</td>"
            f'<td class="num">{format_count(len(digest.funds))}</td>'
            f'<td><a href="data/{week_end}.json">JSON</a></td>'
            "</tr>"
        )
    table = [
        '<div class="table-wrap" role="region" aria-labelledby="weeks-heading" tabindex="0">',
        '<table id="weeks">',
        "<caption>Weekly digests, newest first</caption>",
        "<thead>",
        "<tr>"
        '<th scope="col">Week ending</th>'
        '<th scope="col">Week range</th>'
        '<th scope="col" class="num">Funds</th>'
        '<th scope="col">Data</th>'
        "</tr>",
        "</thead>",
        "<tbody>",
        *rows,
        "</tbody>",
        "</table>",
        "</div>",
    ]
    body = [
        "<h1>Archive</h1>",
        '<p class="lede">Every weekly digest, newest first.</p>',
        '<h2 id="weeks-heading">Weeks</h2>',
        *table,
        '<p class="note">Each week also links its underlying digest document as JSON; the '
        "whole site is rebuilt from those documents.</p>",
    ]
    return _document(
        title=f"Archive \u2014 {site_title}",
        description="Every weekly digest of Canadian ETF holdings changes, newest first.",
        canonical=canonical,
        prefix="",
        site_title=site_title,
        current="archive",
        body=body,
    )


def _week_content(
    digest: WeeklyDigest, *, prefix: str, latest: bool, older: date | None, newer: date | None
) -> list[str]:
    week_end = digest.week_end.isoformat()
    week_range = _esc(format_week_range(digest.week_start, digest.week_end))
    parts = []
    if digest.demo:
        parts.append(f'<p class="banner">{DEMO_BANNER}</p>')
    parts.extend(
        [
            f"<h1>Week ending {week_end}</h1>",
            f'<p class="lede">{_LEDE}</p>',
            '<p class="week-meta">'
            f"Week {week_range} \u00b7 "
            f'known by <time datetime="{_esc(iso_timestamp(digest.known_by))}">'
            f"{_esc(format_known_by(digest.known_by))}</time>"
            "</p>",
        ]
    )
    if latest:
        parts.append(
            '<p class="note">Latest week \u00b7 <a href="archive.html">All weeks</a> \u00b7 '
            '<a href="feed.xml">Atom feed</a></p>'
        )
    parts.extend(_funds_section(digest, week_end))
    parts.extend(_security_section(digest))
    parts.extend(_overlap_section(digest))
    parts.extend(_quality_section(digest, prefix, week_end))
    parts.extend(_pager(prefix, older, newer))
    return parts


def _funds_section(digest: WeeklyDigest, week_end: str) -> list[str]:
    rows = ["<tbody>"]
    for fund in digest.funds:
        rows.append(
            "<tr>"
            f'<th scope="row">{_esc(fund.ticker)}</th>'
            f"<td>{_esc(fund.name)}</td>"
            f"<td>{_esc(_STATUS_LABELS[fund.status])}</td>"
            f'<td class="num">{format_count(fund.adds)}</td>'
            f'<td class="num">{format_count(fund.drops)}</td>'
            f'<td class="num">{format_count(fund.weight_changes)}</td>'
            f'<td class="num">{format_percent(fund.turnover)}</td>'
            f'<td class="num">{format_percent(fund.top10_weight_end)}</td>'
            f'<td class="num">{format_signed_money(fund.flow_estimate_sum)}</td>'
            "</tr>"
        )
    rows.append("</tbody>")
    return [
        '<section id="funds" aria-labelledby="funds-heading">',
        '<h2 id="funds-heading">Funds</h2>',
        '<div class="table-wrap" role="region" aria-labelledby="funds-heading" tabindex="0">',
        '<table id="changes">',
        f"<caption>Portfolio changes by fund, week ending {week_end}</caption>",
        "<thead>",
        "<tr>"
        '<th scope="col">Ticker</th>'
        '<th scope="col">Fund</th>'
        '<th scope="col">Status</th>'
        '<th scope="col" class="num">Adds</th>'
        '<th scope="col" class="num">Drops</th>'
        '<th scope="col" class="num">Weight changes</th>'
        '<th scope="col" class="num">Turnover</th>'
        '<th scope="col" class="num">Top-10 weight (end)</th>'
        '<th scope="col" class="num">Est. flow (CAD)</th>'
        "</tr>",
        "</thead>",
        *rows,
        "</table>",
        "</div>",
        '<p class="note">Turnover is half the sum of absolute weight changes over the week. '
        "The estimated flow is \u0394shares \u00d7 NAV summed over the days with share counts, "
        "in CAD; it ignores distributions and in-kind activity, and a dash means no estimate "
        "was possible. \u201cNew in window\u201d funds have no portfolio at the start of the "
        "week; \u201cno change data\u201d funds have a single snapshot.</p>",
        "</section>",
    ]


def _security_section(digest: WeeklyDigest) -> list[str]:
    disclosed = [fund for fund in digest.funds if fund.securities_disclosed and fund.changes]
    parts = [
        '<section id="security-changes" aria-labelledby="security-changes-heading">',
        '<h2 id="security-changes-heading">Security changes</h2>',
    ]
    if not disclosed:
        parts.append(
            '<p class="note">No security-level changes are published for this week; only '
            "aggregate counts are shown.</p>"
        )
        parts.append("</section>")
        return parts
    parts.append(
        '<p class="note">Security-level lines appear only for funds whose sources allow it '
        "(D79). Identifiers are the dataset's own security ids.</p>"
    )
    for fund in disclosed:
        parts.extend(_fund_securities(fund))
    parts.append("</section>")
    return parts


def _fund_securities(fund: Fund) -> list[str]:
    rows = ["<tbody>"]
    for change in fund.changes:
        rows.append(_security_row(change))
    rows.append("</tbody>")
    return [
        '<div class="fund-block">',
        f'<h3><span class="ticker">{_esc(fund.ticker)}</span>{_esc(fund.name)}</h3>',
        f'<div class="table-wrap" role="region" aria-label="Security changes for '
        f'{_esc(fund.ticker)}" tabindex="0">',
        "<table>",
        f"<caption>Security changes for {_esc(fund.ticker)} \u2014 {_esc(fund.name)}</caption>",
        "<thead>",
        "<tr>"
        '<th scope="col">Change</th>'
        '<th scope="col">Security</th>'
        '<th scope="col">Identifier</th>'
        '<th scope="col" class="num">Weight before</th>'
        '<th scope="col" class="num">Weight after</th>'
        '<th scope="col" class="num">Change</th>'
        "</tr>",
        "</thead>",
        *rows,
        "</table>",
        "</div>",
        "</div>",
    ]


def _security_row(change: SecurityChange) -> str:
    security_id = _esc(change.security_id) if change.security_id is not None else EM_DASH
    return (
        "<tr>"
        f'<th scope="row">{_esc(_CHANGE_LABELS[change.change_type])}</th>'
        f"<td>{_esc(change.security_name)}</td>"
        f'<td class="id">{security_id}</td>'
        f'<td class="num">{format_percent(change.weight_before)}</td>'
        f'<td class="num">{format_percent(change.weight_after)}</td>'
        f'<td class="num">{format_signed_percent(change.delta)}</td>'
        "</tr>"
    )


def _overlap_section(digest: WeeklyDigest) -> list[str]:
    parts = [
        '<section id="overlap" aria-labelledby="overlap-heading">',
        '<h2 id="overlap-heading">Overlap shifts</h2>',
    ]
    if not digest.overlap_shifts:
        parts.append(
            '<p class="note">No fund pair\u2019s overlap moved beyond the reporting '
            "threshold this week.</p>"
        )
        parts.append("</section>")
        return parts
    rows = ["<tbody>"]
    for shift in digest.overlap_shifts:
        rows.append(
            "<tr>"
            f"<td>{_esc(shift.fund_a)}</td>"
            f"<td>{_esc(shift.fund_b)}</td>"
            f'<td class="num">{format_percent(shift.overlap_start)}</td>'
            f'<td class="num">{format_percent(shift.overlap_end)}</td>'
            f'<td class="num">{format_signed_percent(shift.delta)}</td>'
            "</tr>"
        )
    rows.append("</tbody>")
    parts.extend(
        [
            '<div class="table-wrap" role="region" aria-labelledby="overlap-heading" tabindex="0">',
            '<table id="overlap-shifts">',
            "<caption>Fund pairs whose overlap moved beyond the reporting threshold</caption>",
            "<thead>",
            "<tr>"
            '<th scope="col">Fund A</th>'
            '<th scope="col">Fund B</th>'
            '<th scope="col" class="num">Overlap start</th>'
            '<th scope="col" class="num">Overlap end</th>'
            '<th scope="col" class="num">Change</th>'
            "</tr>",
            "</thead>",
            *rows,
            "</table>",
            "</div>",
            '<p class="note">Overlap is the share of weight two funds hold in the same '
            "securities; only pairs that moved by at least the reporting threshold are "
            "listed.</p>",
            "</section>",
        ]
    )
    return parts


def _quality_section(digest: WeeklyDigest, prefix: str, week_end: str) -> list[str]:
    quality = digest.quality
    included_word = "file" if quality.files_included == 1 else "files"
    if quality.funds_without_data:
        without = ", ".join(f"<code>{_esc(ticker)}</code>" for ticker in quality.funds_without_data)
        missing = f"no data this week for {without}"
    else:
        missing = "every tracked fund had data this week"
    return [
        '<section id="quality" aria-labelledby="quality-heading">',
        '<h2 id="quality-heading">Data quality</h2>',
        "<p>"
        f"{format_count(quality.files_included)} {included_word} included, "
        f"{format_count(quality.files_excluded)} excluded; {missing}. "
        f'<a href="{prefix}data/{week_end}.json">Week data (JSON)</a>.'
        "</p>",
        "</section>",
    ]


def _pager(prefix: str, older: date | None, newer: date | None) -> list[str]:
    links: list[str] = []
    if older is not None:
        links.append(
            f'<a rel="prev" href="{prefix}weeks/{older.isoformat()}.html">'
            f"&larr; Older: week ending {older.isoformat()}</a>"
        )
    if newer is not None:
        links.append(
            f'<a rel="next" href="{prefix}weeks/{newer.isoformat()}.html">'
            f"Newer: week ending {newer.isoformat()} &rarr;</a>"
        )
    if not links:
        return []
    return [f'<nav class="pager" aria-label="Weekly pages">{"".join(links)}</nav>']


def _document(
    *,
    title: str,
    description: str,
    canonical: str | None,
    prefix: str,
    site_title: str,
    current: str,
    body: Sequence[str],
) -> str:
    head = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(title)}</title>",
        f'<meta name="description" content="{_esc(description)}">',
        '<meta name="color-scheme" content="light dark">',
    ]
    if canonical is not None:
        head.append(f'<link rel="canonical" href="{_esc(canonical)}">')
    head.extend(["<style>", STYLESHEET.rstrip("\n"), "</style>", "</head>", "<body>"])
    lines = [
        *head,
        '<a class="skip-link" href="#main">Skip to content</a>',
        _header(prefix, site_title, current),
        '<main id="main" tabindex="-1">',
        *body,
        "</main>",
        _footer(),
        "</body>",
        "</html>",
    ]
    return "\n".join(line for line in lines if line) + "\n"


def _header(prefix: str, site_title: str, current: str) -> str:
    def item(href: str, label: str, key: str) -> str:
        current_attr = ' aria-current="page"' if key == current else ""
        return f'<li><a href="{prefix}{href}"{current_attr}>{label}</a></li>'

    return "\n".join(
        [
            '<header class="site-header">',
            f'<p class="wordmark"><a href="{prefix}index.html">{_esc(site_title)}</a></p>',
            '<nav aria-label="Site">',
            "<ul>",
            item("index.html", "This week", "index"),
            item("archive.html", "Archive", "archive"),
            f'<li><a href="{prefix}feed.xml">Feed</a></li>',
            "</ul>",
            "</nav>",
            "</header>",
        ]
    )


def _footer() -> str:
    return "\n".join(
        [
            '<footer class="site-footer">',
            f"<p>{DISCLAIMER}.</p>",
            "<p>Compiled from issuer-published holdings files at their recorded cutoffs; "
            "figures can change when an issuer republishes a file.</p>",
            "</footer>",
        ]
    )


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)
