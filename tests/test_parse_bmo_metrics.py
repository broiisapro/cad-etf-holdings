"""Tests for the BMO ETF factsheet metrics parser (offline)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from etfh.parse.bmo_metrics import METRICS_PARSERS, BmoMetricsParser, stale_days
from etfh.parse.registry import find_metrics_parsers
from etfh.schema import ParsedFundMetrics
from etfh.types import RawFile
from tests._metrics_helpers import bmo_factsheet_pdf
from tests._parse_helpers import make_raw


def _raw(
    path: Path,
    *,
    ticker: str = "BMLK",
    fetched_at: datetime = datetime(2026, 10, 29, 22, 0, tzinfo=UTC),
) -> RawFile:
    return make_raw(
        path,
        source_id="bmo_metrics",
        fund_ticker=ticker,
        as_of_hint=date(2026, 9, 30),
        fetched_at=fetched_at,
    )


def test_parses_factsheet_into_currency_units(tmp_path: Path) -> None:
    path = bmo_factsheet_pdf(tmp_path)
    parsed = BmoMetricsParser().parse(_raw(path))
    ParsedFundMetrics.validate(parsed.frame)
    assert parsed.as_of_date == date(2026, 9, 30)
    assert parsed.published_at is None
    row = parsed.frame.row(0, named=True)
    assert row["fund_ticker"] == "BMLK"
    assert row["aum"] == pytest.approx(25_592_960_000.0)
    assert row["shares_outstanding"] == pytest.approx(214_703_140.0)
    assert row["nav_per_unit"] == pytest.approx(25_592_960_000.0 / 214_703_140.0)
    assert row["currency"] == "CAD"
    assert row["source_id"] == "bmo_metrics"
    assert row["raw_file_sha256"] == parsed.raw.sha256
    assert "aum_units=millions->currency_units" in parsed.notes
    assert "nav_derived=aum/shares" in parsed.notes
    assert "stale_days=29" in parsed.notes


def test_stale_days_counts_from_the_statement_date(tmp_path: Path) -> None:
    path = bmo_factsheet_pdf(tmp_path)
    fetched = datetime(2026, 10, 27, 13, 47, tzinfo=UTC)
    parsed = BmoMetricsParser().parse(_raw(path, fetched_at=fetched))
    assert "stale_days=27" in parsed.notes


def test_stale_days_uses_the_toronto_calendar_date(tmp_path: Path) -> None:
    path = bmo_factsheet_pdf(tmp_path)
    # 02:00 UTC on Oct 1 is still Sep 30 in Toronto, so the fetch is same-day.
    fetched = datetime(2026, 10, 1, 2, 0, tzinfo=UTC)
    parsed = BmoMetricsParser().parse(_raw(path, fetched_at=fetched))
    assert "stale_days=0" in parsed.notes


def test_stale_days_helper_is_signed() -> None:
    assert stale_days(date(2026, 9, 30), datetime(2026, 11, 2, 12, 0, tzinfo=UTC)) == 33
    assert stale_days(date(2026, 9, 30), datetime(2026, 9, 29, 12, 0, tzinfo=UTC)) == -1


def test_currency_falls_back_to_cad(tmp_path: Path) -> None:
    path = bmo_factsheet_pdf(
        tmp_path,
        lines=[
            "BMLK BMO Lookalike Index ETF DATA AS OF SEP 30, 2026",
            "Net Assets (MM) $1,000.00",
            "Shares Outstanding 100,000",
        ],
    )
    parsed = BmoMetricsParser().parse(_raw(path))
    assert parsed.frame["currency"][0] == "CAD"


def test_missing_net_assets_leaves_aum_and_nav_null(tmp_path: Path) -> None:
    path = bmo_factsheet_pdf(
        tmp_path,
        lines=[
            "BMLK BMO Lookalike Index ETF DATA AS OF SEP 30, 2026",
            "Shares Outstanding 100,000",
        ],
    )
    parsed = BmoMetricsParser().parse(_raw(path))
    row = parsed.frame.row(0, named=True)
    assert row["aum"] is None
    assert row["nav_per_unit"] is None
    assert row["shares_outstanding"] == pytest.approx(100_000.0)
    assert "nav_derived=aum/shares" not in parsed.notes


def test_missing_shares_leaves_shares_and_nav_null(tmp_path: Path) -> None:
    path = bmo_factsheet_pdf(
        tmp_path,
        lines=[
            "BMLK BMO Lookalike Index ETF DATA AS OF SEP 30, 2026",
            "Net Assets (MM) $1,000.00",
        ],
    )
    parsed = BmoMetricsParser().parse(_raw(path))
    row = parsed.frame.row(0, named=True)
    assert row["aum"] == pytest.approx(1_000_000_000.0)
    assert row["shares_outstanding"] is None
    assert row["nav_per_unit"] is None


def test_no_metrics_lines_is_an_error(tmp_path: Path) -> None:
    path = bmo_factsheet_pdf(
        tmp_path, lines=["BMLK BMO Lookalike Index ETF DATA AS OF SEP 30, 2026"]
    )
    with pytest.raises(ValueError, match="neither"):
        BmoMetricsParser().parse(_raw(path))


def test_missing_date_is_an_error(tmp_path: Path) -> None:
    path = bmo_factsheet_pdf(
        tmp_path,
        lines=["Net Assets (MM) $1,000.00", "Shares Outstanding 100,000"],
    )
    with pytest.raises(ValueError, match="DATA AS OF"):
        BmoMetricsParser().parse(_raw(path))


def test_can_parse_only_its_source_and_pdf() -> None:
    parser = BmoMetricsParser()
    assert (
        parser.can_parse(
            make_raw(
                Path(__file__),
                source_id="bmo_metrics",
                fund_ticker="BMLK",
            )
        )
        is False
    )  # ext is .py, not pdf
    assert (
        parser.can_parse(make_raw(Path(__file__), source_id="bmo_daily", fund_ticker="BMLK"))
        is False
    )


def test_metrics_registry_discovers_the_parser(tmp_path: Path) -> None:
    path = bmo_factsheet_pdf(tmp_path)
    raw = make_raw(path, source_id="bmo_metrics", fund_ticker="BMLK")
    parsers = find_metrics_parsers(raw, source=None)  # type: ignore[arg-type]
    assert [type(parser) for parser in parsers] == [BmoMetricsParser]
    assert [parsers[0]] == METRICS_PARSERS
