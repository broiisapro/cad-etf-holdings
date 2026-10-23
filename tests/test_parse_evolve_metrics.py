"""Tests for the Evolve product-page metrics parser (offline)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from etfh.parse.evolve_metrics import METRICS_PARSERS, EvolveMetricsParser, metrics_from_html
from etfh.parse.registry import find_metrics_parsers
from etfh.schema import ParsedFundMetrics
from etfh.types import RawFile
from tests._metrics_helpers import EVOLVE_PAGE
from tests._parse_helpers import make_raw


def _raw(path: Path = EVOLVE_PAGE, *, ticker: str = "EVMF") -> RawFile:
    return make_raw(path, source_id="evolve_metrics", fund_ticker=ticker, as_of_hint=None)


def test_parses_nav_aum_and_derives_shares() -> None:
    parsed = EvolveMetricsParser().parse(_raw())
    ParsedFundMetrics.validate(parsed.frame)
    assert parsed.as_of_date == date(2026, 10, 22)
    row = parsed.frame.row(0, named=True)
    assert row["fund_ticker"] == "EVMF"
    assert row["nav_per_unit"] == pytest.approx(31.42)
    assert row["aum"] == pytest.approx(125_600_000.0)
    assert row["shares_outstanding"] == pytest.approx(125_600_000.0 / 31.42)
    assert row["currency"] == "CAD"
    assert row["source_id"] == "evolve_metrics"
    assert "shares_derived=aum/nav" in parsed.notes


def test_missing_aum_leaves_aum_and_shares_null() -> None:
    html = EVOLVE_PAGE.read_text().replace('<h2 data-key="aum" class="">$125.60 M</h2>', "")
    as_of, nav, shares, aum = metrics_from_html(html)
    assert as_of == date(2026, 10, 22)
    assert nav == pytest.approx(31.42)
    assert aum is None
    assert shares is None


def test_missing_nav_card_is_an_error() -> None:
    html = EVOLVE_PAGE.read_text().replace('data-key="nav"', 'data-key="missing"')
    with pytest.raises(ValueError, match="NAV card"):
        metrics_from_html(html)


def test_missing_nav_date_uses_the_raw_hint() -> None:
    html = EVOLVE_PAGE.read_text().replace("As at October 22, 2026", "")
    as_of, _, _, _ = metrics_from_html(html, fallback_date=date(2026, 10, 21))
    assert as_of == date(2026, 10, 21)


def test_non_numeric_nav_is_an_error() -> None:
    html = EVOLVE_PAGE.read_text().replace("$31.42", "&mdash;")
    with pytest.raises(ValueError, match="numeric value"):
        metrics_from_html(html)


def test_metrics_registry_discovers_the_parser() -> None:
    raw = _raw()
    parsers = find_metrics_parsers(raw, source=None)  # type: ignore[arg-type]
    assert [type(parser) for parser in parsers] == [EvolveMetricsParser]
    assert [parsers[0]] == METRICS_PARSERS
