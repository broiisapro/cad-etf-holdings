"""Every committed synthetic daily fixture is covered by a parser test."""

from __future__ import annotations

from datetime import date

import pytest

from etfh.parse.synthetic_daily import PARSERS, SynCsvParser, SynJsonParser, SynXlsxParser
from etfh.schema import ParsedHoldings
from etfh.types import RawFile
from tests._parse_helpers import (
    RAW_SYNTHETIC,
    load_manifest,
    raw_synthetic_files,
    weight_sum,
)

_DAILY_SOURCES = {"syn_csv", "syn_xlsx", "syn_json"}


def _parser(raw: RawFile) -> SynCsvParser | SynXlsxParser | SynJsonParser:
    for parser in PARSERS:
        if parser.can_parse(raw):
            return parser
    raise AssertionError(f"no parser handles {raw.path}")


def test_every_daily_fixture_file_parses_and_validates() -> None:
    raws = [raw for raw in raw_synthetic_files() if raw.source_id in _DAILY_SOURCES]
    assert len(raws) == 14
    seen: set[tuple[str, date]] = set()
    for raw in raws:
        parsed = _parser(raw).parse(raw)
        ParsedHoldings.validate(parsed.frame)
        assert parsed.frame.height >= 2, raw.path
        assert parsed.notes[0].startswith("declared_unit="), raw.path
        assert parsed.as_of_date == raw.as_of_hint, raw.path
        assert parsed.frame["source_id"].unique().to_list() == [raw.source_id]
        assert parsed.frame["raw_file_sha256"].unique().to_list() == [raw.sha256]
        seen.add((raw.fund_ticker, parsed.as_of_date))
    assert {ticker for ticker, _ in seen} == {"SYNA", "SYNB", "SYNC", "SYND", "SYNE"}


def test_non_bad_daily_files_sum_to_one() -> None:
    manifest = load_manifest()
    bad_paths = {item["path"] for item in manifest["bad_files"]}
    raws = [raw for raw in raw_synthetic_files() if raw.source_id in _DAILY_SOURCES]
    checked = 0
    for raw in raws:
        relative = raw.path.relative_to(RAW_SYNTHETIC).as_posix()
        if relative in bad_paths:
            continue
        parsed = _parser(raw).parse(raw)
        assert weight_sum(parsed) == pytest.approx(1.0, abs=1e-9), relative
        checked += 1
    assert checked == 12


def test_bad_daily_files_are_covered_by_the_manifest() -> None:
    manifest = load_manifest()
    bad_paths = {item["path"] for item in manifest["bad_files"]}
    raws = [raw for raw in raw_synthetic_files() if raw.source_id in _DAILY_SOURCES]
    paths = {raw.path.relative_to(RAW_SYNTHETIC).as_posix() for raw in raws}
    assert bad_paths <= paths


def test_parsers_ignore_other_sources() -> None:
    others = [raw for raw in raw_synthetic_files() if raw.source_id not in _DAILY_SOURCES]
    assert others and {raw.source_id for raw in others} == {"syn_pdf"}
    for raw in others:
        for parser in PARSERS:
            assert not parser.can_parse(raw)
