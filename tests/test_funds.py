"""Tests for the committed real fund universe (``config/funds.csv``, FR-1.2)."""

from __future__ import annotations

from pathlib import Path

from etfh.registry import Fund, Source, is_collectable, load_funds, load_sources
from etfh.types import Decision, SourceKind

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

EXPECTED_ISSUERS = frozenset({"bmo", "purpose", "evolve", "vanguard_ca", "globalx_ca"})

EXPECTED_SOURCE_IDS: dict[str, frozenset[str]] = {
    "bmo": frozenset({"bmo_daily", "bmo_qpd"}),
    "purpose": frozenset({"purpose_daily", "purpose_qpd"}),
    "evolve": frozenset({"evolve_daily", "evolve_qpd"}),
    "vanguard_ca": frozenset({"vanguard_ca_qpd"}),
    "globalx_ca": frozenset({"globalx_ca_qpd"}),
}

SPOT_CHECK_NAMES = {
    "ZSP": "BMO S&P 500 Index ETF",
    "ZCN": "BMO S&P/TSX Capped Composite Index ETF",
    "ZAG": "BMO Aggregate Bond Index ETF",
    "PDF": "Purpose Core Dividend Fund",
    "BND": "Purpose Global Bond Fund",
    "ESPX": "Evolve S&P 500® Enhanced Yield Fund",
    "VFV": "Vanguard S&P 500 Index ETF",
    "VAB": "Vanguard Canadian Aggregate Bond Index ETF",
    "HXT": "Global X S&P/TSX 60 Index Corporate Class ETF",
    "CASH": "Global X High Interest Savings ETF",
}


def _load() -> tuple[dict[str, Source], list[Fund]]:
    sources = load_sources(CONFIG_DIR / "sources.yaml")
    funds = load_funds(CONFIG_DIR / "funds.csv", sources)
    return sources, funds


def _real(funds: list[Fund]) -> list[Fund]:
    return [fund for fund in funds if fund.issuer != "synthetic"]


def test_committed_config_loads() -> None:
    sources, funds = _load()
    assert sources
    assert funds


def test_synthetic_rows_are_kept() -> None:
    _, funds = _load()
    synthetic = [fund for fund in funds if fund.issuer == "synthetic"]
    assert [fund.ticker for fund in synthetic] == [
        "SYNA",
        "SYNB",
        "SYNC",
        "SYND",
        "SYNE",
        "SYNF",
        "SYNX",
    ]


def test_real_universe_size_issuers_and_unique_tickers() -> None:
    _, funds = _load()
    real = _real(funds)
    assert len(real) >= 40
    assert len({fund.issuer for fund in real}) >= 3
    assert len({fund.ticker for fund in funds}) == len(funds)


def test_expected_issuers_are_covered() -> None:
    _, funds = _load()
    assert {fund.issuer for fund in _real(funds)} >= EXPECTED_ISSUERS


def test_at_least_25_real_funds_have_a_daily_source() -> None:
    sources, funds = _load()
    daily = [
        fund
        for fund in _real(funds)
        if any(
            sources[source_id].kind is SourceKind.daily_holdings for source_id in fund.source_ids
        )
    ]
    assert len(daily) >= 25


def test_real_funds_use_collectable_sources_of_their_issuer() -> None:
    sources, funds = _load()
    for fund in _real(funds):
        assert fund.exchange in {"XTSE", "NEOE"}, fund.ticker
        assert set(fund.source_ids) == EXPECTED_SOURCE_IDS[fund.issuer], fund.ticker
        for source_id in fund.source_ids:
            source = sources[source_id]
            assert is_collectable(source), (fund.ticker, source_id)
            assert source.issuer == fund.issuer, (fund.ticker, source_id)


def test_every_source_id_exists_and_is_collectable_or_not_allowed() -> None:
    sources, funds = _load()
    for fund in funds:
        for source_id in fund.source_ids:
            assert source_id in sources, (fund.ticker, source_id)
            source = sources[source_id]
            assert is_collectable(source) or source.terms.decision is Decision.not_allowed, (
                fund.ticker,
                source_id,
            )


def test_purpose_rows_carry_a_slug() -> None:
    _, funds = _load()
    purpose = [fund for fund in funds if fund.issuer == "purpose"]
    assert purpose
    for fund in purpose:
        slug = fund.params.get("purpose_slug")
        assert slug, fund.ticker
        assert slug.startswith("purpose-"), fund.ticker


def test_asset_class_coverage() -> None:
    _, funds = _load()
    classes = {fund.asset_class for fund in _real(funds)}
    assert {"equity", "fixed_income"} <= classes
    assert len(classes) >= 3


def test_tickers_and_names_spot_check() -> None:
    _, funds = _load()
    by_ticker = {fund.ticker: fund for fund in funds}
    for ticker, name in SPOT_CHECK_NAMES.items():
        assert ticker in by_ticker, ticker
        assert by_ticker[ticker].name == name, ticker
