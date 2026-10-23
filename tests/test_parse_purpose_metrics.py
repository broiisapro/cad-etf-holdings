"""Tests for the Purpose fund-metrics parser (offline, from stored daily JSON)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from etfh.parse.purpose_metrics import (
    METRICS_PARSERS,
    PurposeMetricsParser,
    metrics_from_payload,
)
from etfh.parse.registry import find_metrics_parsers
from etfh.schema import ParsedFundMetrics
from etfh.types import RawFile
from tests._metrics_helpers import FIXTURES
from tests._parse_helpers import make_raw

FIXTURE = FIXTURES / "purpose_metrics" / "fund.json"


def _payload() -> dict[str, Any]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _raw(path: Path = FIXTURE, *, ticker: str = "PMTF") -> RawFile:
    return make_raw(path, source_id="purpose_daily", fund_ticker=ticker, as_of_hint=None)


def _write_payload(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "fund.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_parses_nav_history_and_attaches_aum_to_the_nav_date() -> None:
    parsed = PurposeMetricsParser().parse(_raw())
    ParsedFundMetrics.validate(parsed.frame)
    assert parsed.as_of_date == date(2026, 10, 22)
    assert parsed.published_at is None
    assert parsed.frame.height == 3
    assert parsed.frame["as_of_date"].to_list() == [
        date(2026, 10, 20),
        date(2026, 10, 21),
        date(2026, 10, 22),
    ]
    assert parsed.frame["nav_per_unit"].to_list() == pytest.approx([10.0, 10.2, 10.4])
    assert parsed.frame["aum"].to_list() == [None, None, 125_600_000.0]
    assert parsed.frame["shares_outstanding"].to_list()[0:2] == [None, None]
    assert parsed.frame["shares_outstanding"][2] == pytest.approx(125_600_000.0 / 10.4)
    row = parsed.frame.row(0, named=True)
    assert row["fund_ticker"] == "PMTF"
    assert row["currency"] == "CAD"
    assert row["source_id"] == "purpose_metrics"
    assert row["raw_file_sha256"] == parsed.raw.sha256
    assert "aum_scope=fund" in parsed.notes
    assert "shares_derived=fund_aum/etf_nav" in parsed.notes


def test_one_row_per_date_even_when_details_repeats_the_aum_date() -> None:
    parsed = PurposeMetricsParser().parse(_raw())
    assert parsed.frame["as_of_date"].n_unique() == parsed.frame.height


def test_billions_are_expanded_to_currency_units() -> None:
    payload = _payload()
    payload["details"]["ETF"][2]["val"] = "$4.4B"
    _, rows, _, _ = metrics_from_payload(payload)
    assert rows[date(2026, 10, 22)][2] == pytest.approx(4_400_000_000.0)


def test_zero_aum_is_zero_not_null() -> None:
    payload = _payload()
    payload["details"]["ETF"][2]["val"] = "$0"
    _, rows, _, _ = metrics_from_payload(payload)
    assert rows[date(2026, 10, 22)][2] == 0.0
    assert rows[date(2026, 10, 22)][1] == 0.0


def test_missing_aum_leaves_aum_and_shares_null(tmp_path: Path) -> None:
    payload = _payload()
    payload["details"]["ETF"] = [
        item for item in payload["details"]["ETF"] if item["name"] != "aum"
    ]
    parsed = PurposeMetricsParser().parse(_raw(_write_payload(tmp_path, payload)))
    assert parsed.frame["aum"].null_count() == parsed.frame.height
    assert parsed.frame["shares_outstanding"].null_count() == parsed.frame.height


def test_missing_nav_table_uses_details_nav(tmp_path: Path) -> None:
    payload = _payload()
    del payload["nav_table"]
    parsed = PurposeMetricsParser().parse(_raw(_write_payload(tmp_path, payload)))
    assert parsed.frame.height == 1
    assert parsed.frame["as_of_date"][0] == date(2026, 10, 22)
    assert parsed.frame["nav_per_unit"][0] == pytest.approx(10.40)
    assert parsed.frame["aum"][0] == pytest.approx(125_600_000.0)
    assert parsed.frame["shares_outstanding"][0] == pytest.approx(125_600_000.0 / 10.40)


def test_no_nav_source_is_an_error() -> None:
    payload = _payload()
    del payload["nav_table"]
    payload["details"]["ETF"] = [
        item for item in payload["details"]["ETF"] if item["name"] != "nav"
    ]
    with pytest.raises(ValueError, match="no nav_table rows"):
        metrics_from_payload(payload)


def test_purpose_file_without_metrics_section_is_empty_not_an_error() -> None:
    path = FIXTURES / "purpose" / "lookalike.json"
    parsed = PurposeMetricsParser().parse(_raw(path, ticker="PDIV"))
    ParsedFundMetrics.validate(parsed.frame)
    assert parsed.frame.height == 0
    assert parsed.frame.columns == [
        "fund_ticker",
        "as_of_date",
        "nav_per_unit",
        "shares_outstanding",
        "aum",
        "currency",
        "source_id",
        "raw_file_sha256",
    ]
    assert parsed.notes == ["metrics_present=false"]


def test_can_parse_only_purpose_daily_json() -> None:
    parser = PurposeMetricsParser()
    assert parser.can_parse(_raw()) is True
    other = make_raw(FIXTURE, source_id="bmo_metrics", fund_ticker="PMTF")
    assert parser.can_parse(other) is False


def test_metrics_registry_discovers_the_parser() -> None:
    raw = _raw()
    parsers = find_metrics_parsers(raw, source=None)  # type: ignore[arg-type]
    assert [type(parser) for parser in parsers] == [PurposeMetricsParser]
    assert [parsers[0]] == METRICS_PARSERS
