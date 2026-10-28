"""Write the static site for weekly digest documents (interfaces wave 08 §4).

``render_site`` turns one or more digest documents into a self-contained
directory that GitHub Pages can serve as-is:

- ``index.html`` — the latest week, with the changes table, per-fund security
  changes (only where the fund discloses them), overlap shifts and the
  data-quality line;
- ``weeks/<week_end>.html`` — one page per week with older/newer links;
- ``archive.html`` — every week, newest first;
- ``data/<week_end>.json`` — the source document, ``sort_keys=True, indent=2``;
- ``feed.xml`` — Atom, one entry per week;
- ``.nojekyll`` — so Pages serves the files unchanged.

The site is plain HTML and inline CSS: no JavaScript, no web fonts and no
requests to any origin. Every page carries the exploratory-data disclaimer and
demo documents carry the synthetic-data banner. Output depends only on the
input documents, so two runs over the same documents write byte-identical
files.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from etfh.site.feed import render_feed
from etfh.site.model import parse_digest
from etfh.site.pages import render_archive, render_index, render_week_page

__all__ = ["render_site"]

DEFAULT_SITE_TITLE = "cad-etf-holdings \u2014 weekly changes"


def render_site(
    digests: Sequence[Mapping[str, Any]],
    out_dir: Path,
    *,
    site_title: str = DEFAULT_SITE_TITLE,
    base_url: str | None = None,
) -> list[Path]:
    """Render ``digests`` into ``out_dir`` and return the written paths, sorted.

    Only ``schema_version == 1`` documents are accepted; a malformed document
    raises ``ValueError`` naming the digest and field. ``base_url`` is the
    public root of the site (with or without a trailing slash) and is used for
    canonical links and Atom ids; when omitted, pages use relative links only
    and feed ids fall back to ``urn:etfh:week:<date>``.

    Week pages and data files left over from an earlier run are removed when
    they follow the ``YYYY-MM-DD`` naming and are not part of this run, so a
    shorter digest run does not leave stale weeks on the site.
    """
    entries = [
        (parse_digest(document, label=f"digest {index}"), document)
        for index, document in enumerate(digests)
    ]
    if not entries:
        raise ValueError("render_site needs at least one weekly digest")
    entries.sort(key=lambda entry: entry[0].week_end)
    seen: set[date] = set()
    for digest, _ in entries:
        if digest.week_end in seen:
            raise ValueError(f"duplicate week_end {digest.week_end.isoformat()}")
        seen.add(digest.week_end)
    weeks = [digest for digest, _ in entries]
    root = _normalise_base_url(base_url)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    written.append(_write(out / ".nojekyll", ""))
    written.append(
        _write(
            out / "index.html",
            render_index(
                weeks[-1],
                site_title=site_title,
                base_url=root,
                older=weeks[-2].week_end if len(weeks) > 1 else None,
            ),
        )
    )
    for position, (digest, document) in enumerate(entries):
        week_end = digest.week_end.isoformat()
        written.append(
            _write(
                out / "weeks" / f"{week_end}.html",
                render_week_page(
                    digest,
                    site_title=site_title,
                    base_url=root,
                    older=weeks[position - 1].week_end if position > 0 else None,
                    newer=weeks[position + 1].week_end if position + 1 < len(weeks) else None,
                ),
            )
        )
        written.append(
            _write(
                out / "data" / f"{week_end}.json",
                json.dumps(_sanitise(document), sort_keys=True, indent=2) + "\n",
            )
        )
    newest_first = list(reversed(weeks))
    written.append(
        _write(
            out / "archive.html", render_archive(newest_first, site_title=site_title, base_url=root)
        )
    )
    written.append(
        _write(out / "feed.xml", render_feed(newest_first, site_title=site_title, base_url=root))
    )
    keep = set(written)
    _prune(out / "weeks", keep)
    _prune(out / "data", keep)
    return sorted(written)


def _sanitise(document: Mapping[str, Any]) -> dict[str, Any]:
    """The document as it may be published: no security lines for closed funds.

    For a document that follows the contract this changes nothing (such funds
    already carry an empty ``changes`` list). It exists so that a caller who
    supplies changes for a fund whose ``securities_disclosed`` is false cannot
    publish them through the site's JSON export either (D79).
    """
    sanitised = dict(document)
    funds = document.get("funds")
    if isinstance(funds, list):
        cleaned: list[Any] = []
        for fund in funds:
            if isinstance(fund, Mapping) and fund.get("securities_disclosed") is False:
                entry = dict(fund)
                entry["changes"] = []
                cleaned.append(entry)
            else:
                cleaned.append(fund)
        sanitised["funds"] = cleaned
    return sanitised


def _normalise_base_url(value: str | None) -> str | None:
    """Return the site root with one trailing slash, or ``None``."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError("base_url must be a non-empty URL or None")
    if any(character.isspace() for character in stripped):
        raise ValueError("base_url must not contain whitespace")
    if not stripped.startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    return stripped.rstrip("/") + "/"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _prune(directory: Path, keep: set[Path]) -> None:
    """Remove dated week pages/data files that this run did not write."""
    if not directory.is_dir():
        return
    for path in sorted(directory.iterdir()):
        if path in keep or not _is_week_file(path.name) or not path.is_file():
            continue
        path.unlink()


def _is_week_file(name: str) -> bool:
    stem, dot, suffix = name.partition(".")
    if dot != "." or suffix not in {"html", "json"}:
        return False
    try:
        date.fromisoformat(stem)
    except ValueError:
        return False
    return True
