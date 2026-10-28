"""Unit tests for the site's formatting and digest checking (issue #184).

These cover the two places where a rendering bug would be invisible in a
snapshot: number formatting (percentages, signed CAD amounts, em dashes for
missing values) and the input check that also enforces the D79 non-disclosure
rule before anything reaches a page.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

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
from etfh.site.model import parse_digest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "digest"


def load_documents() -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(FIXTURE_DIR.glob("*.json"))
    ]


def demo_document() -> dict[str, Any]:
    return next(document for document in load_documents() if document["demo"])


def test_percent_formatting() -> None:
    assert format_percent(0.0312) == "3.12%"
    assert format_percent(0.43) == "43.00%"
    assert format_percent(1.0) == "100.00%"
    assert format_percent(0.0) == "0.00%"
    assert format_percent(-0.0) == "0.00%"
    assert format_percent(None) == EM_DASH


def test_signed_percent_formatting() -> None:
    assert format_signed_percent(0.012) == "+1.20%"
    assert format_signed_percent(-0.009) == "-0.90%"
    assert format_signed_percent(0.0) == "0.00%"
    assert format_signed_percent(-0.0) == "0.00%"
    assert format_signed_percent(None) == EM_DASH


def test_signed_money_formatting() -> None:
    assert format_signed_money(1_250_000.0) == "+1,250,000"
    assert format_signed_money(-340_000.0) == "-340,000"
    assert format_signed_money(0.0) == "0"
    assert format_signed_money(12.4) == "+12"
    assert format_signed_money(-12.6) == "-13"
    assert format_signed_money(None) == EM_DASH


def test_count_formatting() -> None:
    assert format_count(0) == "0"
    assert format_count(9) == "9"
    assert format_count(1234) == "1,234"
    assert format_count(1_234_567) == "1,234,567"


def test_dates_and_timestamps() -> None:
    assert format_week_range(date(2026, 10, 23), date(2026, 10, 30)) == "2026-10-23 to 2026-10-30"
    known_by = datetime(2026, 10, 31, 4, 0, tzinfo=UTC)
    assert format_known_by(known_by) == "2026-10-31 04:00 UTC"
    assert iso_timestamp(known_by) == "2026-10-31T04:00:00+00:00"
    offset = timezone(timedelta(hours=5))
    assert format_known_by(datetime(2026, 10, 31, 9, 0, tzinfo=offset)) == "2026-10-31 04:00 UTC"


def test_parses_the_fixture_weeks() -> None:
    for label, document in enumerate(load_documents()):
        digest = parse_digest(document, label=f"digest {label}")
        assert digest.week_end == date.fromisoformat(document["week_end"])
        assert digest.week_start == date.fromisoformat(document["week_start"])
        assert digest.demo is document["demo"]
        assert [fund.ticker for fund in digest.funds] == [
            fund["ticker"] for fund in document["funds"]
        ]


def test_sorts_funds_by_ticker() -> None:
    document = copy.deepcopy(demo_document())
    document["funds"].reverse()
    digest = parse_digest(document)
    assert [fund.ticker for fund in digest.funds] == ["SYNA", "SYNB", "SYNC", "SYNE"]


def test_keeps_security_changes_for_disclosed_funds() -> None:
    digest = parse_digest(demo_document())
    syna = next(fund for fund in digest.funds if fund.ticker == "SYNA")
    assert syna.securities_disclosed is True
    assert len(syna.changes) == 6
    assert syna.changes[0].change_type == "add"
    assert syna.changes[0].security_id == "TKR:XTSE:AA"
    assert syna.changes[0].delta == pytest.approx(0.012)


def test_drops_changes_for_non_disclosed_funds() -> None:
    document = copy.deepcopy(load_documents()[0])
    fund = next(entry for entry in document["funds"] if entry["ticker"] == "RLEQ")
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
    digest = parse_digest(document)
    assert next(entry for entry in digest.funds if entry.ticker == "RLEQ").changes == ()


def test_keeps_nulls_and_parses_overlap() -> None:
    digest = parse_digest(demo_document())
    synb = next(fund for fund in digest.funds if fund.ticker == "SYNB")
    assert synb.top10_weight_end == pytest.approx(0.52)
    assert synb.flow_estimate_sum is None
    assert digest.overlap_shifts[0].fund_a == "SYNA"
    assert digest.overlap_shifts[0].delta == pytest.approx(0.0127)


@pytest.mark.parametrize("version", [2, 0, True, "1", None])
def test_rejects_wrong_schema_version(version: object) -> None:
    document = copy.deepcopy(demo_document())
    document["schema_version"] = version
    with pytest.raises(ValueError, match="schema_version"):
        parse_digest(document)


def test_rejects_duplicate_tickers() -> None:
    document = copy.deepcopy(demo_document())
    document["funds"][1]["ticker"] = document["funds"][0]["ticker"]
    with pytest.raises(ValueError, match="duplicate tickers SYNA"):
        parse_digest(document)


def test_rejects_a_week_that_ends_before_it_starts() -> None:
    document = copy.deepcopy(demo_document())
    document["week_start"] = document["week_end"]
    with pytest.raises(ValueError, match="before week_end"):
        parse_digest(document)


def test_rejects_missing_sections() -> None:
    document = copy.deepcopy(demo_document())
    del document["quality"]
    with pytest.raises(ValueError, match="quality: is missing"):
        parse_digest(document)
