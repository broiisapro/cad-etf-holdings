"""Generic parser for quarterly portfolio disclosure PDFs.

Canadian ETF issuers publish a quarterly "Summary of Investment Portfolio" (or
quarterly portfolio disclosure) as a PDF. Those documents list the fund's top
holdings with a percent-of-NAV weight and state the total weight of the listed
holdings. This module extracts that table into the normalized holdings frame.

The parser is deliberately table-extraction agnostic: it reads positioned
words with pdfplumber and reconstructs rows from horizontal gaps, so it works
for ruled tables, unruled tables, and two side-by-side column layouts. When no
rows are found at all it falls back to pdfplumber's table extraction and then
to camelot 2.x (``stream`` then ``lattice``), as required by D17.

Issuer-specific quirks live in :class:`QpdRules` instances registered in
:data:`QPD_RULES` by source id, so a new issuer format does not require a new
parser class.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pdfplumber
import polars as pl

from etfh.types import FileFormat, ParsedFile, RawFile
from etfh.units import WeightUnit, check_unit, normalize_weights

__all__ = [
    "DEFAULT_RULES",
    "PARSERS",
    "QPD_RULES",
    "QpdPdfError",
    "QpdPdfParser",
    "QpdRules",
    "parse_qpd_date",
]

_TORONTO = ZoneInfo("America/Toronto")
_WHITESPACE = re.compile(r"\s+")
_NBSP = "\u00a0"
_NARROW_NBSP = "\u202f"
_NULL_SEGMENTS = {"-", "\u2212", "\u2013", "\u2014"}

_DATE_FORMATS = (
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
)
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
}
_DATE_TOKEN = r"\d{1,2}\s+\w+\s+\d{4}|\w+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}"


class QpdPdfError(ValueError):
    """Raised when a disclosure PDF cannot be interpreted."""


@dataclass(frozen=True, slots=True)
class QpdRules:
    """Format hints for one issuer's quarterly portfolio disclosure.

    The defaults cover the common "Holding / % of net asset value" layout.
    Issuer rules are allowed to narrow or extend any pattern; the extraction
    core itself never changes.
    """

    name: str = "generic"
    wrapped_names: bool = False
    as_of_patterns: tuple[str, ...] = (
        rf"\bas\s+(?:at|of)\s+({_DATE_TOKEN})",
        rf"\bfor\s+the\s+(?:quarter|period|year|six\s+months)\s+ended\s+({_DATE_TOKEN})",
        rf"\bau\s+({_DATE_TOKEN})",
        rf"\ben\s+date\s+du\s+({_DATE_TOKEN})",
    )
    published_patterns: tuple[str, ...] = (
        rf"\bpublished\s+(?:on\s+)?({_DATE_TOKEN})",
        rf"\bdate\s+(?:of\s+)?publication\s*:?\s*({_DATE_TOKEN})",
        rf"\bpublication\s+date\s*:?\s*({_DATE_TOKEN})",
        rf"\bpubli[ée]\s+le\s+({_DATE_TOKEN})",
    )
    total_patterns: tuple[str, ...] = (
        r"total\s+of\s+the\s+holdings\s+listed\s+above\s*:?\s*"
        r"([<>]?\s*-?\d[\d\s.,]*\s*%)",
        r"total\s+(?:investments?|portfolio|holdings)\s*:?\s*([<>]?\s*-?\d[\d\s.,]*\s*%)",
        r"total\s*:?\s*([<>]?\s*-?\d[\d\s.,]*\s*%)",
        r"total\s+des\s+titres\s+[ée]num[ée]r[ée]s\s+ci-dessus\s*:?\s*"
        r"([<>]?\s*-?\d[\d\s.,]*\s*%)",
    )
    header_patterns: tuple[str, ...] = (
        r"^(?:holding|holdings|security|securities|description|investment|name)\b"
        r".*(?:%|weight|value|nav|net\s+asset)",
        r"^(?:%|percentage)\s+(?:of|weight)",
        r"^(?:weight|weighting|market\s+value|shares?|quantity)\b",
        r"^(?:name|description)\s+(?:%|weight)",
    )
    skip_patterns: tuple[str, ...] = (
        r"^(?:sub-?total|grand\s+total)\b",
        r"^total\s*:?\s*%?$",
        r"^total\s+(?:of\s+the\s+holdings|investments?|portfolio|net\s+assets?)\b",
        r"^page\s+\d+\s*(?:of\s+\d+)?$",
        r"^\d+\s*$",
        r"^(?:sector|industry|asset\s+class)\s*:",
    )
    sector_grouping: bool = True
    column_gap: float = 10.0
    two_column: bool = False
    column_scope: str = "both"
    column_split: float = 0.0
    x_tolerance: float = 3.0
    orphan_weight_join: bool = False
    section_start_patterns: tuple[str, ...] = ()
    section_end_patterns: tuple[str, ...] = ()
    ignored_segment_patterns: tuple[str, ...] = ()
    name_cleanup_patterns: tuple[str, ...] = ()
    partial_section_pattern: str = ""
    stated_total_applies_to_partial: bool = True
    table_settings: dict[str, Any] | None = None


DEFAULT_RULES = QpdRules()

QPD_RULES: dict[str, QpdRules] = {}

# Vanguard Canada's disclosure is a two-column page: sector allocation on the
# left, the Top 25 Holdings on the right. The document's font kerning makes
# pdfplumber merge adjacent words at the default tolerance, so the rules lower
# it to 1. The sum of the listed holdings is printed unlabelled at the bottom
# of the right column, just above "Total net asset value".
VANGUARD_RULES = replace(
    DEFAULT_RULES,
    name="vanguard_ca",
    x_tolerance=1.0,
    column_scope="right",
    total_patterns=(
        r"([<>]?\s*-?\d[\d.,]*)\s*\n\s*total\s+net\s+asset\s+value",
        r"total\s+investments?\s*:?\s*([<>]?\s*-?\d[\d\s.,]*\s*%)",
        *DEFAULT_RULES.total_patterns,
    ),
)

GLOBALX_RULES = replace(
    DEFAULT_RULES,
    name="globalx_ca",
    section_start_patterns=(r"^top\s+(?:\d+\s+)?holdings\b",),
    partial_section_pattern=r"^top\s+\d+\s+holdings\b",
    stated_total_applies_to_partial=False,
    section_end_patterns=(
        r"^top\s+\d+\s+securities\b",
        r"^the\s+summary\s+of\s+investment\b",
    ),
    ignored_segment_patterns=(
        r"^\$\s*[\d,]+",
        r"^\(notional\b",
    ),
    name_cleanup_patterns=(
        r"\s*\(notional\s+value\s*$",
        r"\s*\(notional\s+value\s+[^)]*\)(?:\(\d+\))?",
    ),
    total_patterns=(
        r"\$\s*[\d,]+\s+([<>]?\s*-?\d[\d\s.,]*\s*%)",
        *DEFAULT_RULES.total_patterns,
    ),
)

BMO_RULES = replace(
    DEFAULT_RULES,
    name="bmo",
    total_patterns=(
        r"total\s+investments?\s*:?\s*([<>]?\s*-?\d[\d\s.,]*\s*%)",
        *DEFAULT_RULES.total_patterns,
    ),
    ignored_segment_patterns=(
        r"^\$.*",
        r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$",
        r"^\(\d{1,3}(?:,\d{3})+(?:\.\d+)?\)$",
    ),
)

# Purpose puts the Top 25 Holdings down the right column and the sector and
# asset mixes down the left. The listed holdings have no stated total, so the
# table is scoped to "TOP 25 HOLDINGS" and treated as a partial list. Long
# bond and option names wrap over several lines and can push the weight onto
# its own line; the wrapped-name pass joins those fragments by column position
# so a strike price printed inside the name never becomes a weight.
PURPOSE_RULES = replace(
    DEFAULT_RULES,
    name="purpose",
    column_scope="right",
    orphan_weight_join=True,
    wrapped_names=True,
    sector_grouping=False,
    section_start_patterns=(r"^top\s*25\s+holdings\b",),
    partial_section_pattern=r"^top\s*25\s+holdings\b",
    stated_total_applies_to_partial=False,
    skip_patterns=(
        r"^asset\s+value$",
        r"^top\s+holdings\s+table\s+includes\b",
        r"^transactions\s+of\s+the\s+investment\b",
        r"^it\s+may\s+also\s+be\s+obtained\b",
        r"^and\s+cash\s+equivalents\b",
        r"^information\s+is\s+available\b",
        r"^quarterly\s+portfolio\s+disclosure$",
        *DEFAULT_RULES.skip_patterns,
    ),
)

# Evolve prints the Top 25 Positions on the left and a small sector mix on
# the right; the listed holdings' total ("Total 97.46%") is only printed at
# the foot of the left column, so the total pattern anchors at a line start
# and the extraction is scoped to the left column.
EVOLVE_RULES = replace(
    DEFAULT_RULES,
    name="evolve",
    column_scope="left",
    column_split=348.0,
    skip_patterns=(
        r"^value$",
        r"^top\s+25\s+positions\b",
        *DEFAULT_RULES.skip_patterns,
    ),
    total_patterns=(
        r"(?m)^total\s*:?\s*([<>]?\s*-?\d[\d.,]*\s*%)",
        *DEFAULT_RULES.total_patterns,
    ),
)

QPD_RULES["vanguard_ca_qpd"] = VANGUARD_RULES
QPD_RULES["globalx_ca_qpd"] = GLOBALX_RULES
QPD_RULES["bmo_qpd"] = BMO_RULES
QPD_RULES["purpose_qpd"] = PURPOSE_RULES
QPD_RULES["evolve_qpd"] = EVOLVE_RULES


@dataclass(frozen=True, slots=True)
class _Row:
    """One holding row before unit conversion (weight still in percent)."""

    name: str
    weight: float
    sector: str
    decimals: int | None = None


@dataclass(frozen=True, slots=True)
class _Segment:
    """One horizontal run of words in a PDF line, with its position.

    ``top`` and ``x1`` are None for rows rebuilt from a table extractor, where
    no coordinates are available and the text-only heuristics apply.
    """

    text: str
    top: float | None = None
    x1: float | None = None


@dataclass(slots=True)
class _Extraction:
    """Rows found in a document plus whether they came from a partial section."""

    rows: list[_Row]
    partial_section: bool = False


def _clean(value: str) -> str:
    return _WHITESPACE.sub(" ", value.replace(_NBSP, " ").replace(_NARROW_NBSP, " ")).strip()


def _number_value(raw: str) -> float | None:
    """Parse a display number, treating a lone comma as a decimal separator.

    Canadian disclosures mix English (``12,345.6``) and French (``12 345,6``)
    conventions. A comma followed by one or two digits in a value below 1000 is
    read as a decimal comma; otherwise it is a thousands separator.
    """
    text = _clean(raw)
    if not text:
        return None
    text = text.replace("\u2212", "-")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    text = text.replace(" ", "")
    if not text or not any(character.isdigit() for character in text):
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        left, _, right = text.partition(",")
        text = f"{left}.{right}" if 1 <= len(right) <= 2 else text.replace(",", "")
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return -value if negative else value


_NUMERIC_SEGMENT = re.compile(r"^\(?\s*-?\d[\d\s.,]*\s*%?\)?$")
_TRAILING_WEIGHT = re.compile(r"^(?P<name>.+?)\s+(?P<value>\(?\s*-?\d[\d\s.,]*\s*%?\)?)$")


def _float_text(text: str) -> str:
    """Normalize a display number to the form ``_number_value`` parses."""
    value = _clean(text).replace("\u2212", "-")
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    if value.endswith("%"):
        value = value[:-1].strip()
    return value.replace(" ", "")


def _decimal_places(text: str) -> int:
    """Number of decimals printed in a display number, 0 when it has none.

    A comma followed by one or two digits is a decimal comma; other commas are
    thousands separators (the same reading :func:`_number_value` uses).
    """
    value = _float_text(text).lstrip("-")
    if "." in value:
        return len(value.rsplit(".", 1)[1])
    if "," in value and not re.search(r",\d{3}(?:,|$)", value):
        return len(value.rsplit(",", 1)[1])
    return 0


def _weight_value(text: str) -> float | None:
    """Return a plausible percent weight from text, else None."""
    if not _NUMERIC_SEGMENT.match(text):
        return None
    value = _number_value(text)
    if value is None or abs(value) > 100.0:
        return None
    return value


def _split_trailing_weight(text: str) -> tuple[str, float | None]:
    match = _TRAILING_WEIGHT.match(text)
    if not match:
        return text, None
    value = _weight_value(match.group("value"))
    if value is None:
        return text, None
    name = match.group("name").strip()
    if not name:
        return text, None
    return name, value


def parse_qpd_date(text: str) -> date | None:
    """Parse a date written in the formats disclosures use."""
    candidate = _clean(text).strip(" .,;:")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    parts = candidate.replace(",", " ").split()
    if len(parts) == 3:
        day: int | None = None
        month: int | None = None
        year: int | None = None
        for part in parts:
            if part.isdigit() and len(part) == 4:
                year = int(part)
            elif part.isdigit():
                day = int(part)
            else:
                month = _MONTHS.get(part.lower().rstrip("."))
        if day is not None and month is not None and year is not None:
            try:
                return date(year, month, day)
            except ValueError:
                return None
    return None


def _first_match(text: str, patterns: Sequence[str]) -> re.Match[str] | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match
    return None


def _stated_total(text: str, rules: QpdRules) -> float | None:
    """Return the last stated total on the page set, in percent.

    Patterns are tried in order and the first pattern that matches wins, so an
    issuer rule can put the exact total-line wording ahead of the generic
    fallbacks.
    """
    for pattern in rules.total_patterns:
        matches = [
            _number_value(match.group(1)) for match in re.finditer(pattern, text, re.IGNORECASE)
        ]
        values = [value for value in matches if value is not None]
        if values:
            return values[-1]
    return None


def _date_from_text(text: str, patterns: Sequence[str]) -> date | None:
    remaining = text
    for _ in range(2):
        match = _first_match(remaining, patterns)
        if match is None:
            return None
        parsed = parse_qpd_date(match.group(1))
        if parsed is not None:
            return parsed
        remaining = remaining[match.end() :]
    return None


def _published_at(text: str, patterns: Sequence[str]) -> datetime | None:
    stated = _date_from_text(text, patterns)
    if stated is None:
        return None
    return datetime.combine(stated, time.min, tzinfo=_TORONTO).astimezone(UTC)


def _is_header(text: str, rules: QpdRules) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in rules.header_patterns)


def _is_skippable(text: str, rules: QpdRules) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in rules.skip_patterns)


def _is_total_line(text: str, rules: QpdRules) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in rules.total_patterns)


def _sector_candidate(text: str) -> bool:
    if not text or len(text) > 80:
        return False
    if any(character.isdigit() for character in text):
        return False
    if text.endswith((".", ",", ":", ";")):
        return False
    if re.search(
        r"\b(?:page|source|notes?|fund|portfolio|summary|published|report|quarter|"
        r"annual|management|as\s+at|as\s+of|net\s+asset)\b",
        text,
        re.IGNORECASE,
    ):
        return False
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z &'()/.,-]*", text))


def _asset_class(name: str) -> str:
    lowered = name.lower()
    if re.match(r"^cash\b", lowered):
        return "cash"
    if re.match(r"^(?:other\s+net\s+assets?|net\s+other\s+assets?|other\s+assets?\b)", lowered):
        return "other"
    if re.search(r"\b(?:futures?|forwards?|swaps?|options?|warrants?|derivatives?)\b", lowered):
        return "derivative"
    if re.match(r"^(?:foreign\s+currenc|fx\b|currenc(?:y|ies)\s+(?:forward|contract))", lowered):
        return "fx"
    return "equity"


def _is_subtotal(name: str, current_sector: str | None) -> bool:
    lowered = _clean(name).lower()
    if lowered in {"total", "subtotal", "sub-total", "grand total"}:
        return True
    match = re.match(r"^(?:sub-?)?total\s+(.+)$", lowered)
    if match and current_sector is not None:
        remainder = match.group(1).strip()
        sector = current_sector.strip().lower()
        return remainder == sector
    return False


def _clean_name(name: str, rules: QpdRules) -> str:
    value = _clean(name)
    for pattern in rules.name_cleanup_patterns:
        value = re.sub(pattern, "", value, flags=re.IGNORECASE)
    return _clean(value)


def _ignored_segment(text: str, rules: QpdRules) -> bool:
    return any(
        re.search(pattern, text, re.IGNORECASE) for pattern in rules.ignored_segment_patterns
    )


def _segment_text(segment: _Segment | str) -> str:
    return segment.text if isinstance(segment, _Segment) else segment


def _line_text(segments: Sequence[_Segment]) -> str:
    return _clean(" ".join(segment.text for segment in segments))


def _line_top(segments: Sequence[_Segment]) -> float | None:
    tops = [segment.top for segment in segments if segment.top is not None]
    return min(tops) if tops else None


def _entries_from_segments(
    segments: Sequence[_Segment | str], sector: str, rules: QpdRules
) -> list[_Row]:
    """Parse name/weight segments left-to-right into rows.

    A segment that is only a number closes the accumulated name; a segment
    with a trailing number closes it too. Extra non-numeric columns (a ticker,
    a currency) stay part of the name, and two side-by-side lists on the same
    line naturally produce two rows.
    """
    rows: list[_Row] = []
    name_parts: list[str] = []
    for raw_segment in segments:
        segment = _clean(_segment_text(raw_segment))
        if not segment or segment in _NULL_SEGMENTS or _ignored_segment(segment, rules):
            continue
        if name_parts:
            standalone = _weight_value(segment)
            if standalone is not None:
                rows.append(
                    _Row(
                        name=_clean_name(" ".join(name_parts), rules),
                        weight=standalone,
                        sector=sector,
                        decimals=_decimal_places(segment),
                    )
                )
                name_parts = []
                continue
        name, trailing = _split_trailing_weight(segment)
        if trailing is not None:
            if name:
                name_parts.append(name)
            rows.append(
                _Row(
                    name=_clean_name(" ".join(name_parts), rules),
                    weight=trailing,
                    sector=sector,
                    decimals=_decimal_places(segment),
                )
            )
            name_parts = []
        else:
            name_parts.append(segment)
    return rows


def _segments_from_words(words: Sequence[dict[str, Any]], gap: float) -> list[_Segment]:
    segments: list[_Segment] = []
    current: list[dict[str, Any]] = []
    previous_x1: float | None = None
    for word in sorted(words, key=lambda item: float(item["x0"])):
        x0 = float(word["x0"])
        if current and previous_x1 is not None and x0 - previous_x1 > gap:
            segments.append(_segment_from_words(current))
            current = []
        current.append(word)
        previous_x1 = float(word["x1"])
    if current:
        segments.append(_segment_from_words(current))
    return segments


def _segment_from_words(words: Sequence[dict[str, Any]]) -> _Segment:
    return _Segment(
        text=" ".join(str(word["text"]) for word in words),
        top=min(float(word["top"]) for word in words),
        x1=max(float(word["x1"]) for word in words),
    )


def _group_page_lines(page: Any, gap: float) -> list[list[_Segment]]:
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    return _group_words(words, gap)


def _group_words(words: Sequence[dict[str, Any]], gap: float) -> list[list[_Segment]]:
    """Group positioned words into lines and split each line into segments."""
    lines: dict[float, list[dict[str, Any]]] = {}
    for word in words:
        top = round(float(word["top"]), 1)
        lines.setdefault(top, []).append(word)
    grouped: list[list[_Segment]] = []
    for top in sorted(lines):
        grouped.append(_segments_from_words(lines[top], gap))
    return grouped


def _lines_from_page(page: Any, rules: QpdRules) -> list[list[_Segment]]:
    words = page.extract_words(
        use_text_flow=False,
        keep_blank_chars=False,
        x_tolerance=rules.x_tolerance,
    )
    if rules.column_scope in {"left", "right"}:
        split = rules.column_split if rules.column_split > 0 else float(page.width) / 2.0
        want_right = rules.column_scope == "right"
        selected = [word for word in words if (float(word["x0"]) >= split) is want_right]
        return _group_words(selected, rules.column_gap)
    if not rules.two_column:
        return _group_words(words, rules.column_gap)
    if not words:
        return []
    midpoint = float(page.width) / 2.0
    grouped: list[list[_Segment]] = []
    for side in (False, True):
        selected = [word for word in words if (float(word["x0"]) >= midpoint) is side]
        grouped.extend(_group_words(selected, rules.column_gap))
    return grouped


def _ends_with_weight(segments: Sequence[_Segment], rules: QpdRules) -> bool:
    cleaned = [_clean(segment.text) for segment in segments if _clean(segment.text)]
    if not cleaned:
        return False
    last = cleaned[-1]
    if _weight_value(last) is not None:
        return True
    _, trailing = _split_trailing_weight(last)
    return trailing is not None


def _join_orphan_weights(
    lines: Sequence[Sequence[_Segment]], rules: QpdRules
) -> list[list[_Segment]]:
    """Attach a weight printed alone on its own line to the name above it.

    Some issuers wrap a long security name so that the weight lands on its own
    line between the name and its continuation (Purpose bond funds). Merging
    that line back keeps the row and its weight instead of dropping both. The
    segment keeps its position so the wrapped-name pass can still tell a
    weight cell from a printed strike price.
    """
    merged: list[list[_Segment]] = []
    for segments in lines:
        cleaned = [segment for segment in segments if _clean(segment.text)]
        if (
            rules.orphan_weight_join
            and len(cleaned) == 1
            and _weight_value(cleaned[0].text) is not None
            and merged
            and not _ends_with_weight(merged[-1], rules)
        ):
            merged[-1] = [*merged[-1], cleaned[0]]
            continue
        merged.append(cleaned)
    return merged


def _weight_column_x1(lines: Sequence[Sequence[_Segment]]) -> float | None:
    """Right edge of the weight cell column on one page, when it is printed.

    Weight cells are right-aligned, so the rightmost numeric segment that is a
    plausible weight marks the column. Continuation lines print strike prices
    and coupons inside the name column, well to the left of it.
    """
    edges = [
        segment.x1
        for line in lines
        for segment in line
        if segment.x1 is not None and _weight_value(segment.text) is not None
    ]
    return max(edges) if edges else None


def _is_weight_cell(segment: _Segment, column_x1: float) -> bool:
    return (
        segment.x1 is not None
        and segment.x1 >= column_x1 - 1.5
        and _weight_value(segment.text) is not None
    )


def _line_has_weight(segments: Sequence[_Segment], column_x1: float) -> bool:
    return any(_is_weight_cell(segment, column_x1) for segment in segments)


def _weight_line_tops(lines: Sequence[Sequence[_Segment]], column_x1: float) -> list[float]:
    """Top positions of the weight cells themselves.

    An orphan weight that was merged back into its name line keeps the top it
    was printed at, which is what decides whether a later bare line is the
    tail of that row or the head of the next one.
    """
    tops: list[float] = []
    for segments in lines:
        tops.extend(
            segment.top
            for segment in segments
            if segment.top is not None and _is_weight_cell(segment, column_x1)
        )
    return sorted(tops)


_SENTENCE_BREAK = re.compile(r"[.!?]\s+\w")


def _is_name_fragment(text: str) -> bool:
    """True when a bare line can be part of a security name.

    Wrapped names are short fragments. A sentence (two sentences on one line,
    or a long run of prose) is a footer or a note, not a name, and must never
    be glued to the row above it.
    """
    words = text.split()
    if _SENTENCE_BREAK.search(text) and len(words) > 4:
        return False
    return len(words) <= 12 or any(character.isdigit() for character in text)


def _nearest_weight_is_below(top: float, weight_tops: Sequence[float]) -> bool:
    """True when the closest weight cell belongs to the next row.

    A line without a weight cell is either the first part of a wrapped name
    (the weight follows on a later line) or the tail of the previous row (the
    weight was printed before it). The vertical distance to the adjacent weight
    cells decides; a tie counts as a tail, which keeps a stray continuation
    attached to the row it follows.
    """
    below = [candidate for candidate in weight_tops if candidate > top]
    above = [candidate for candidate in weight_tops if candidate < top]
    if not below:
        return False
    if not above:
        return True
    return min(below) - top < top - max(above)


def _prepend_text(segments: Sequence[_Segment], prefix: str) -> list[_Segment]:
    if not segments:
        return []
    first, *rest = segments
    return [_Segment(f"{prefix} {first.text}".strip(), first.top, first.x1), *rest]


def _rows_from_lines(lines: Sequence[Sequence[_Segment]], rules: QpdRules) -> _Extraction:
    rows: list[_Row] = []
    sector: str | None = None
    partial_section = False
    active = not rules.section_start_patterns
    if rules.orphan_weight_join:
        lines = _join_orphan_weights(lines, rules)
    weight_column = _weight_column_x1(lines) if rules.wrapped_names else None
    weight_tops = _weight_line_tops(lines, weight_column) if weight_column is not None else []
    pending_parts: list[str] = []
    for index, segments in enumerate(lines):
        text = _line_text(segments)
        if not text:
            continue
        if rules.section_start_patterns and any(
            re.search(pattern, text, re.IGNORECASE) for pattern in rules.section_start_patterns
        ):
            active = True
            sector = None
            pending_parts = []
            if rules.partial_section_pattern and re.search(
                rules.partial_section_pattern, text, re.IGNORECASE
            ):
                partial_section = True
            continue
        if active and any(
            re.search(pattern, text, re.IGNORECASE) for pattern in rules.section_end_patterns
        ):
            active = False
            pending_parts = []
            continue
        if not active or _is_header(text, rules) or _is_skippable(text, rules):
            continue
        if _is_total_line(text, rules):
            continue
        if weight_column is not None and not _line_has_weight(segments, weight_column):
            if rules.sector_grouping and _sector_candidate(text):
                has_following_holding = any(
                    _line_has_weight(following, weight_column)
                    for following in lines[index + 1 : index + 3]
                )
                if has_following_holding:
                    sector = text
                    continue
            top = _line_top(segments)
            if not _is_name_fragment(text):
                continue
            if top is not None and _nearest_weight_is_below(top, weight_tops):
                pending_parts.append(text)
            elif rows:
                rows[-1] = replace(rows[-1], name=_clean_name(f"{rows[-1].name} {text}", rules))
            continue
        if pending_parts:
            segments = _prepend_text(segments, " ".join(pending_parts))
            pending_parts = []
        parsed = _entries_from_segments(segments, sector or "Unknown", rules)
        if parsed:
            for row in parsed:
                if _is_subtotal(row.name, sector):
                    continue
                rows.append(replace(row, sector=sector or "Unknown"))
            continue
        if rules.sector_grouping and _sector_candidate(text):
            has_following_holding = any(
                _entries_from_segments(following, "Unknown", rules)
                for following in lines[index + 1 : index + 3]
            )
            if has_following_holding:
                sector = text
    return _Extraction(rows=rows, partial_section=partial_section)


def _rows_from_table(table: Sequence[Sequence[str | None]], rules: QpdRules) -> _Extraction:
    lines: list[list[_Segment]] = []
    for raw_row in table:
        segments = [_Segment(_clean(cell)) for cell in raw_row if cell is not None and _clean(cell)]
        if segments:
            lines.append(segments)
    return _rows_from_lines(lines, rules)


def _pdfplumber_rows(path: Any, rules: QpdRules) -> tuple[list[_Row], str, bool]:
    rows: list[_Row] = []
    texts: list[str] = []
    partial = False
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text(x_tolerance=rules.x_tolerance) or ""
            texts.append(page_text)
            extraction = _rows_from_lines(_lines_from_page(page, rules), rules)
            rows.extend(extraction.rows)
            partial = partial or extraction.partial_section
        if not rows:
            for page in pdf.pages:
                for table in page.extract_tables(table_settings=rules.table_settings):
                    extraction = _rows_from_table(table, rules)
                    rows.extend(extraction.rows)
                    partial = partial or extraction.partial_section
    return rows, "\n".join(texts), partial


def _camelot_rows(path: Any) -> tuple[list[_Row], bool]:
    from camelot.io import read_pdf as camelot_read_pdf

    for flavor in ("stream", "lattice"):
        try:
            tables = camelot_read_pdf(str(path), pages="all", flavor=flavor, suppress_stdout=True)
        except Exception:
            continue
        rows: list[_Row] = []
        partial = False
        for table in tables:
            extraction = _rows_from_table(table.data, DEFAULT_RULES)
            rows.extend(extraction.rows)
            partial = partial or extraction.partial_section
        if rows:
            return rows, partial
    return [], False


class QpdPdfParser:
    """Parser for quarterly portfolio disclosure PDFs."""

    formats: frozenset[FileFormat] = frozenset({FileFormat.pdf})

    def __init__(
        self,
        rules: QpdRules | None = None,
        source_ids: frozenset[str] | None = None,
    ) -> None:
        self._rules = rules
        self._source_ids = source_ids

    def can_parse(self, raw: RawFile) -> bool:
        if raw.ext.lower() != FileFormat.pdf.value:
            return False
        return self._source_ids is None or raw.source_id in self._source_ids

    def rules_for(self, raw: RawFile) -> QpdRules:
        if self._rules is not None:
            return self._rules
        return QPD_RULES.get(raw.source_id, DEFAULT_RULES)

    def parse(self, raw: RawFile) -> ParsedFile:
        rules = self.rules_for(raw)
        if raw.path.suffix.lower() != ".pdf":
            raise QpdPdfError(f"{raw.path}: not a PDF")
        rows, text, partial = _pdfplumber_rows(raw.path, rules)
        if not rows:
            rows, partial = _camelot_rows(raw.path)
        if not rows:
            raise QpdPdfError(f"{raw.path}: no holdings table found")
        as_of_date = _date_from_text(text, rules.as_of_patterns)
        if as_of_date is None:
            raise QpdPdfError(f"{raw.path}: no portfolio date found")
        stated_total = _stated_total(text, rules)
        omit_stated_total = partial and not rules.stated_total_applies_to_partial
        if stated_total is None and not omit_stated_total:
            raise QpdPdfError(f"{raw.path}: no stated total found")

        names: list[str] = []
        raw_weights: list[float] = []
        sectors: list[str] = []
        classes: list[str] = []
        for row in rows:
            names.append(row.name)
            raw_weights.append(row.weight)
            sectors.append(row.sector)
            classes.append(_asset_class(row.name))
        weights = normalize_weights(raw_weights, WeightUnit.percent)
        count = len(names)
        frame = pl.DataFrame(
            {
                "fund_ticker": [raw.fund_ticker] * count,
                "as_of_date": [as_of_date] * count,
                "security_name": names,
                "security_ticker": [None] * count,
                "exchange": [None] * count,
                "isin": [None] * count,
                "cusip": [None] * count,
                "asset_class": classes,
                "sector": sectors,
                "country": ["Unknown"] * count,
                "currency": [None] * count,
                "weight": weights.to_list(),
                "shares": [None] * count,
                "market_value": [None] * count,
                "source_id": [raw.source_id] * count,
                "raw_file_sha256": [raw.sha256] * count,
            },
            schema={
                "fund_ticker": pl.String,
                "as_of_date": pl.Date,
                "security_name": pl.String,
                "security_ticker": pl.String,
                "exchange": pl.String,
                "isin": pl.String,
                "cusip": pl.String,
                "asset_class": pl.String,
                "sector": pl.String,
                "country": pl.String,
                "currency": pl.String,
                "weight": pl.Float64,
                "shares": pl.Float64,
                "market_value": pl.Float64,
                "source_id": pl.String,
                "raw_file_sha256": pl.String,
            },
        )
        notes = ["declared_unit=percent"]
        decimals = max(
            (row.decimals for row in rows if row.decimals is not None),
            default=2,
        )
        notes.append(f"weight_decimals={decimals}")
        notes.append("partial_list=true")
        if stated_total is not None and not omit_stated_total:
            stated_total_fraction = stated_total / 100.0
            notes.append(f"stated_total_fraction={stated_total_fraction:.10g}")
        unit_check = check_unit(raw_weights, WeightUnit.percent)
        if not unit_check.consistent and unit_check.suggested is not None:
            notes.append(f"unit_check={unit_check.detail()}")
        return ParsedFile(
            raw=raw,
            as_of_date=as_of_date,
            published_at=_published_at(text, rules.published_patterns),
            frame=frame,
            notes=notes,
        )


PARSERS: list[QpdPdfParser] = [QpdPdfParser()]
