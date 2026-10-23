"""Tests for the BMO ETF factsheet metrics parser (offline)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from etfh.parse.bmo_metrics import METRICS_PARSERS, BmoMetricsParser
from etfh.parse.registry import find_metrics_parsers
from etfh.schema import ParsedFundMetrics
from etfh.types import RawFile
from tests._metrics_helpers import bmo_factsheet_pdf
from tests._parse_helpers import make_raw


def _raw(path: Path, *, ticker: str = "BMLK") -> RawFile:
    return make_raw(path, source_id="bmo_metrics", fund_ticker=ticker, as_of_hint=date(2026, 9, 30))


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


def test_missing_net_assets_is_an_error(tmp_path: Path) -> None:
    path = bmo_factsheet_pdf(
        tmp_path,
        lines=[
            "BMLK BMO Lookalike Index ETF DATA AS OF SEP 30, 2026",
            "Shares Outstanding 100,000",
        ],
    )
    with pytest.raises(ValueError, match="Net Assets"):
        BmoMetricsParser().parse(_raw(path))  # type: ignore[arg-type]


def test_missing_shares_is_an_error(tmp_path: Path) -> None:
    path = bmo_factsheet_pdf(
        tmp_path,
        lines=[
            "BMLK BMO Lookalike Index ETF DATA AS OF SEP 30, 2026",
            "Net Assets (MM) $1,000.00",
        ],
    )
    with pytest.raises(ValueError, match="Shares Outstanding"):
        BmoMetricsParser().parse(_raw(path))  # type: ignore[arg-type]


def test_missing_date_is_an_error(tmp_path: Path) -> None:
    path = bmo_factsheet_pdf(
        tmp_path,
        lines=["Net Assets (MM) $1,000.00", "Shares Outstanding 100,000"],
    )
    with pytest.raises(ValueError, match="DATA AS OF"):
        BmoMetricsParser().parse(_raw(path))  # type: ignore[arg-type]


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
