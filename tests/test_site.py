"""Tests for the static digest site (issue #184, FR-3.1).

The fixtures under ``tests/fixtures/digest/`` are two hand-written documents in
the interfaces wave-08 §3 shape: a demo week with synthetic funds and a public
week whose real-like fund does not disclose securities. Rendering is checked
for the files it writes, byte determinism, table semantics, escaping, the
non-disclosure rule, the Atom feed and the absence of external requests.
"""

from __future__ import annotations

import copy
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Sequence
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from etfh.site import render_site

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "digest"
SNAPSHOT = FIXTURE_DIR / "site" / "index-demo.html"
PUBLIC_WEEK = "2026-10-23"
DEMO_WEEK = "2026-10-30"
ATOM = "{http://www.w3.org/2005/Atom}"

EXPECTED_FILES = [
    ".nojekyll",
    "archive.html",
    f"data/{PUBLIC_WEEK}.json",
    f"data/{DEMO_WEEK}.json",
    "feed.xml",
    "index.html",
    f"weeks/{PUBLIC_WEEK}.html",
    f"weeks/{DEMO_WEEK}.html",
]


def load_digests() -> list[dict[str, Any]]:
    """Both fixture weeks, oldest first (the file names sort that way)."""
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(FIXTURE_DIR.glob("*.json"))
    ]


def digests_by_week() -> dict[str, dict[str, Any]]:
    return {document["week_end"]: document for document in load_digests()}


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[Path]]:
    out = tmp_path_factory.mktemp("site")
    return out, render_site(load_digests(), out)


def _page(site: tuple[Path, list[Path]], name: str) -> str:
    out, _ = site
    return (out / name).read_text(encoding="utf-8")


class _TableParser(HTMLParser):
    """Collect the caption and cell texts of one table by ``id``."""

    def __init__(self, table_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.table_id = table_id
        self.in_target = False
        self.depth = 0
        self.rows: list[list[str]] = []
        self.caption = ""
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._caption: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table":
            if self.in_target:
                self.depth += 1
            elif attributes.get("id") == self.table_id:
                self.in_target = True
                self.depth = 1
            return
        if not self.in_target:
            return
        if tag == "tr" and self.depth == 1:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
        elif tag == "caption" and self.depth == 1:
            self._caption = []

    def handle_endtag(self, tag: str) -> None:
        if not self.in_target:
            return
        if tag == "table":
            self.depth -= 1
            if self.depth == 0:
                self.in_target = False
        elif tag in {"td", "th"} and self._cell is not None:
            assert self._row is not None
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag == "caption" and self._caption is not None:
            self.caption = "".join(self._caption).strip()
            self._caption = None

    def handle_data(self, data: str) -> None:
        if not self.in_target:
            return
        if self._cell is not None:
            self._cell.append(data)
        elif self._caption is not None:
            self._caption.append(data)


def parse_table(page: str, table_id: str) -> _TableParser:
    parser = _TableParser(table_id)
    parser.feed(page)
    parser.close()
    return parser


def table_slice(page: str, table_id: str) -> str:
    """The raw HTML of one table (the site never nests tables)."""
    start = page.index(f'<table id="{table_id}">')
    end = page.index("</table>", start)
    return page[start:end]


def hidden_cells(page: str) -> list[str]:
    return re.findall(r"<th(?=[\s>])(?![^>]*\bscope=)[^>]*>", page)


def test_writes_the_expected_files_sorted(site: tuple[Path, list[Path]]) -> None:
    out, paths = site
    assert [path.relative_to(out).as_posix() for path in paths] == EXPECTED_FILES
    assert paths == sorted(paths)
    assert all(path.is_file() for path in paths)
    assert (out / ".nojekyll").read_text(encoding="utf-8") == ""


def test_output_is_byte_identical_across_runs(
    site: tuple[Path, list[Path]], tmp_path: Path
) -> None:
    out, _ = site
    again = tmp_path / "again"
    render_site(load_digests(), again)
    first = {path.relative_to(out): path.read_bytes() for path in out.rglob("*") if path.is_file()}
    second = {
        path.relative_to(again): path.read_bytes() for path in again.rglob("*") if path.is_file()
    }
    assert first == second


def test_index_has_one_changes_row_per_fund(site: tuple[Path, list[Path]]) -> None:
    table = parse_table(_page(site, "index.html"), "changes")
    funds = digests_by_week()[DEMO_WEEK]["funds"]
    assert len(table.rows) == len(funds) + 1  # header row plus one row per fund
    assert [row[0] for row in table.rows[1:]] == [fund["ticker"] for fund in funds]


def test_changes_table_is_semantic(site: tuple[Path, list[Path]]) -> None:
    page = _page(site, "index.html")
    table = table_slice(page, "changes")
    assert table.count("<caption>") == 1
    assert hidden_cells(table) == []
    assert table.count('scope="col"') == 9
    assert table.count('scope="row"') == 4
    assert '<td class="num">+1,250,000</td>' in table
    assert '<td class="num">-340,000</td>' in table
    assert '<td class="num">\u2014</td>' in table  # SYNB has no flow estimate
    assert '<td class="num">43.00%</td>' in table
    assert "New in window" in table
    assert "No change data" in table


def test_week_pages_carry_the_same_changes_body(site: tuple[Path, list[Path]]) -> None:
    index_rows = parse_table(_page(site, "index.html"), "changes").rows
    week_rows = parse_table(_page(site, f"weeks/{DEMO_WEEK}.html"), "changes").rows
    assert week_rows == index_rows
    assert (
        f"Portfolio changes by fund, week ending {DEMO_WEEK}"
        in parse_table(_page(site, f"weeks/{DEMO_WEEK}.html"), "changes").caption
    )


def test_security_tables_only_for_disclosed_funds_with_changes(
    site: tuple[Path, list[Path]],
) -> None:
    page = _page(site, "index.html")
    assert page.count("<caption>Security changes for SYNA") == 1
    assert page.count("<caption>Security changes for SYNE") == 1
    assert "<caption>Security changes for SYNB" not in page
    assert "<caption>Security changes for SYNC" not in page
    public_week = _page(site, f"weeks/{PUBLIC_WEEK}.html")
    assert "<caption>Security changes for SYNA" in public_week
    assert "<caption>Security changes for RLEQ" not in public_week


def test_overlap_and_quality_sections(site: tuple[Path, list[Path]]) -> None:
    index = _page(site, "index.html")
    overlap = parse_table(index, "overlap-shifts")
    assert overlap.caption.startswith("Fund pairs whose overlap moved")
    assert overlap.rows[1:] == [
        ["SYNA", "SYNB", "24.84%", "26.11%", "+1.27%"],
        ["SYNA", "SYNE", "50.09%", "51.25%", "+1.16%"],
    ]
    assert "9 files included, 0 excluded; every tracked fund had data this week." in index
    public_week = _page(site, f"weeks/{PUBLIC_WEEK}.html")
    assert "12 files included, 1 excluded; no data this week for <code>SYNC</code>." in public_week


def test_security_names_are_escaped(tmp_path: Path) -> None:
    digests = load_digests()
    digests[-1]["funds"][0]["name"] = '<script>alert("fund")</script>'
    digests[-1]["funds"][0]["changes"][0]["security_name"] = "<script>alert('security')</script>"
    render_site(digests, tmp_path)
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in index
    assert "&lt;script&gt;alert(&#x27;security&#x27;)&lt;/script&gt;" in index
    assert "&lt;script&gt;alert(&quot;fund&quot;)&lt;/script&gt;" in index


def test_demo_banner_only_on_demo_pages(site: tuple[Path, list[Path]]) -> None:
    banner = "Demo data \u2014 synthetic funds only"
    disclaimer = "Derived data \u00b7 exploratory \u00b7 not investment advice"
    index = _page(site, "index.html")
    assert banner in index
    assert banner not in _page(site, f"weeks/{PUBLIC_WEEK}.html")
    assert banner in _page(site, f"weeks/{DEMO_WEEK}.html")
    for name in (
        "index.html",
        "archive.html",
        f"weeks/{PUBLIC_WEEK}.html",
        f"weeks/{DEMO_WEEK}.html",
    ):
        assert disclaimer in _page(site, name)


def test_non_disclosed_fund_never_renders_security_lines(tmp_path: Path) -> None:
    digests = load_digests()
    public_week = next(document for document in digests if document["week_end"] == PUBLIC_WEEK)
    fund = next(entry for entry in public_week["funds"] if entry["ticker"] == "RLEQ")
    assert fund["securities_disclosed"] is False
    fund["changes"] = [
        {
            "change_type": "add",
            "security_id": "ISIN:CA0000000001",
            "security_name": "Confidential Cobalt Corp.",
            "weight_before": 0.0,
            "weight_after": 0.01,
            "delta": 0.01,
        }
    ]
    render_site(digests, tmp_path)
    for name in (
        "index.html",
        "archive.html",
        f"weeks/{PUBLIC_WEEK}.html",
        f"weeks/{DEMO_WEEK}.html",
    ):
        page = (tmp_path / name).read_text(encoding="utf-8")
        assert "Confidential Cobalt Corp." not in page
        assert "ISIN:CA0000000001" not in page
    data = (tmp_path / "data" / f"{PUBLIC_WEEK}.json").read_text(encoding="utf-8")
    assert "Confidential Cobalt Corp." not in data
    assert "ISIN:CA0000000001" not in data
    stored = json.loads(data)
    stored_fund = next(entry for entry in stored["funds"] if entry["ticker"] == "RLEQ")
    assert stored_fund["changes"] == []


def test_data_json_round_trips_the_document(site: tuple[Path, list[Path]]) -> None:
    out, _ = site
    for document in load_digests():
        path = out / "data" / f"{document['week_end']}.json"
        assert json.loads(path.read_text(encoding="utf-8")) == document
        assert (
            path.read_text(encoding="utf-8")
            == json.dumps(document, sort_keys=True, indent=2) + "\n"
        )


def test_atom_feed_parses_with_elementtree(site: tuple[Path, list[Path]]) -> None:
    out, _ = site
    root = ET.fromstring((out / "feed.xml").read_text(encoding="utf-8"))
    assert root.tag == f"{ATOM}feed"
    assert root.findtext(f"{ATOM}id") == "urn:etfh:feed"
    assert root.findtext(f"{ATOM}updated") == "2026-10-31T04:00:00+00:00"
    entries = root.findall(f"{ATOM}entry")
    assert len(entries) == len(load_digests())
    assert [entry.findtext(f"{ATOM}id") for entry in entries] == [
        f"urn:etfh:week:{DEMO_WEEK}",
        f"urn:etfh:week:{PUBLIC_WEEK}",
    ]
    assert [entry.findtext(f"{ATOM}updated") for entry in entries] == [
        "2026-10-31T04:00:00+00:00",
        "2026-10-24T04:00:00+00:00",
    ]
    assert entries[0].findtext(f"{ATOM}title") == f"Week ending {DEMO_WEEK}"
    assert "4 funds tracked, 2 additions, 2 drops, 16 weight changes" in (
        entries[0].findtext(f"{ATOM}summary") or ""
    )
    assert entries[0].find(f"{ATOM}link") is None


def test_feed_updated_is_the_latest_known_by(tmp_path: Path) -> None:
    digests = load_digests()
    digests[0]["known_by"] = "2026-11-02T04:00:00+00:00"  # an older week learned later
    render_site(digests, tmp_path)
    root = ET.fromstring((tmp_path / "feed.xml").read_text(encoding="utf-8"))
    assert root.findtext(f"{ATOM}updated") == "2026-11-02T04:00:00+00:00"


def test_single_week_site_has_no_pager(tmp_path: Path) -> None:
    render_site([load_digests()[-1]], tmp_path)
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert 'class="pager"' not in index
    root = ET.fromstring((tmp_path / "feed.xml").read_text(encoding="utf-8"))
    assert len(root.findall(f"{ATOM}entry")) == 1


def test_base_url_drives_ids_and_canonical_links(tmp_path: Path) -> None:
    base = "https://example.test/digest"
    render_site(load_digests(), tmp_path, base_url=base + "/")
    root = ET.fromstring((tmp_path / "feed.xml").read_text(encoding="utf-8"))
    assert root.findtext(f"{ATOM}id") == f"{base}/feed.xml"
    entries = root.findall(f"{ATOM}entry")
    assert [entry.findtext(f"{ATOM}id") for entry in entries] == [
        f"{base}/weeks/{DEMO_WEEK}.html",
        f"{base}/weeks/{PUBLIC_WEEK}.html",
    ]
    assert entries[0].find(f"{ATOM}link").attrib["href"] == f"{base}/weeks/{DEMO_WEEK}.html"  # type: ignore[union-attr]
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert f'<link rel="canonical" href="{base}/">' in index
    week = (tmp_path / "weeks" / f"{DEMO_WEEK}.html").read_text(encoding="utf-8")
    assert f'<link rel="canonical" href="{base}/weeks/{DEMO_WEEK}.html">' in week


def test_pages_make_no_external_requests(site: tuple[Path, list[Path]]) -> None:
    out, _ = site
    for path in out.glob("**/*.html"):
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text, path
        assert "https://" not in text, path
        assert "<script" not in text.lower(), path
        assert " src=" not in text, path


def test_base_url_is_the_only_external_reference(tmp_path: Path) -> None:
    base = "https://example.test/digest/"
    render_site(load_digests(), tmp_path, base_url=base)
    for path in tmp_path.glob("**/*"):
        if not path.is_file() or path.suffix not in {".html", ".xml"}:
            continue
        text = path.read_text(encoding="utf-8")
        for url in re.findall(r"https?://[^\s\"'<>]+", text):
            if url == "http://www.w3.org/2005/Atom":
                continue
            assert url.startswith(base), (path, url)


def test_archive_lists_weeks_newest_first_with_prev_next_links(
    site: tuple[Path, list[Path]],
) -> None:
    archive = _page(site, "archive.html")
    assert archive.index(f"weeks/{DEMO_WEEK}.html") < archive.index(f"weeks/{PUBLIC_WEEK}.html")
    assert archive.index(f"data/{DEMO_WEEK}.json") < archive.index(f"data/{PUBLIC_WEEK}.json")
    latest = _page(site, f"weeks/{DEMO_WEEK}.html")
    older = _page(site, f"weeks/{PUBLIC_WEEK}.html")
    assert f'href="../weeks/{PUBLIC_WEEK}.html">' in latest
    assert 'rel="next"' not in latest
    assert f'href="../weeks/{DEMO_WEEK}.html">' in older
    assert 'rel="prev"' not in older


def test_index_snapshot_for_the_demo_fixture(site: tuple[Path, list[Path]]) -> None:
    assert SNAPSHOT.is_file(), "snapshot missing: tests/fixtures/digest/site/index-demo.html"
    assert _page(site, "index.html") == SNAPSHOT.read_text(encoding="utf-8")


def test_stale_week_files_are_removed(tmp_path: Path) -> None:
    digests = load_digests()
    render_site(digests, tmp_path)
    assert (tmp_path / "weeks" / f"{PUBLIC_WEEK}.html").is_file()
    render_site([digests[-1]], tmp_path)
    assert not (tmp_path / "weeks" / f"{PUBLIC_WEEK}.html").exists()
    assert not (tmp_path / "data" / f"{PUBLIC_WEEK}.json").exists()
    assert (tmp_path / "weeks" / f"{DEMO_WEEK}.html").is_file()
    keep = tmp_path / "weeks" / "notes.html"
    keep.write_text("hand-written", encoding="utf-8")
    render_site(digests, tmp_path)
    assert keep.is_file()


def test_rejects_other_schema_versions_and_empty_input(tmp_path: Path) -> None:
    document = load_digests()[0]
    with pytest.raises(ValueError, match="schema_version"):
        render_site([{**document, "schema_version": 2}], tmp_path)
    with pytest.raises(ValueError, match="schema_version"):
        render_site([{**document, "schema_version": True}], tmp_path)
    with pytest.raises(ValueError, match="at least one"):
        render_site([], tmp_path)


def test_rejects_duplicate_weeks_and_bad_base_urls(tmp_path: Path) -> None:
    document = load_digests()[0]
    with pytest.raises(ValueError, match="duplicate week_end"):
        render_site([document, copy.deepcopy(document)], tmp_path)
    for bad in ("ftp://example.test", "https://exa mple.test", "   "):
        with pytest.raises(ValueError, match="base_url"):
            render_site([document], tmp_path, base_url=bad)


def _mutations(document: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any], str]]:
    """(label, document, expected error fragment) for the checker tests."""

    def mutate(
        path: Sequence[str | int], value: object, label: str, match: str
    ) -> tuple[str, dict[str, Any], str]:
        changed = copy.deepcopy(document)
        target: Any = changed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        return label, changed, match

    yield mutate(("week_end",), "30-10-2026", "bad week_end", r"week_end")
    yield mutate(("known_by",), "2026-10-31T04:00:00", "naive known_by", r"UTC offset")
    yield mutate(("funds", 0, "status"), "sideways", "unknown status", r"status")
    yield mutate(("funds", 0, "counts", "adds"), True, "bool count", r"counts.adds")
    yield mutate(("funds", 0, "turnover"), "high", "string turnover", r"turnover")
    yield mutate(("funds", 0, "changes", 0, "change_type"), "buy", "unknown change", r"change_type")
    yield mutate(("overlap_shifts", 0, "delta"), None, "null delta", r"delta")
    yield mutate(("quality", "files_included"), "9", "string file count", r"files_included")


@pytest.mark.parametrize(
    ("label", "document", "fragment"),
    [case for case in _mutations(load_digests()[-1])],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_rejects_malformed_fields(
    label: str, document: dict[str, Any], fragment: str, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match=fragment):
        render_site([document], tmp_path)


def test_fixtures_follow_the_interface_shape() -> None:
    for document in load_digests():
        assert document["schema_version"] == 1
        assert document["week_start"] < document["week_end"]
        assert document["known_by"].endswith("+00:00")
        tickers = [fund["ticker"] for fund in document["funds"]]
        assert tickers == sorted(tickers)
        for fund in document["funds"]:
            if not fund["securities_disclosed"]:
                assert fund["changes"] == []
