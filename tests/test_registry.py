"""Tests for the source/fund registry and the terms gate."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError

from etfh.collect.registry import all_collectors, collector_for
from etfh.http import PoliteClient
from etfh.registry import (
    Fund,
    Source,
    TermsNotAllowedError,
    TermsRecord,
    funds_for_source,
    is_collectable,
    load_funds,
    load_sources,
    require_collectable,
)
from etfh.types import Decision, FileFormat, SourceKind
from tests.conftest import FakeClock

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

ALLOWED_REAL_SOURCES = {
    "purpose_daily",
    "evolve_daily",
    "bmo_daily",
    "vanguard_ca_qpd",
    "globalx_ca_qpd",
    "purpose_qpd",
    "evolve_qpd",
    "bmo_qpd",
    "purpose_metrics",
    "bmo_metrics",
    "evolve_metrics",
}
UNSUPPORTED_REAL_SOURCES = {
    "vanguard_ca_metrics",
    "globalx_ca_metrics",
}
NOT_ALLOWED_REAL_SOURCES = {
    "ishares_ca_daily",
    "ishares_ca_qpd",
    "vanguard_ca_daily",
    "globalx_ca_daily",
    "harvest_monthly",
    "harvest_qpd",
    "hamilton_qpd",
    "mackenzie_qpd",
}
METRICS_REAL_SOURCES = {
    "purpose_metrics",
    "bmo_metrics",
    "evolve_metrics",
    "vanguard_ca_metrics",
    "globalx_ca_metrics",
}
COMMON_CONDITIONS = [
    "internal research use only",
    "no redistribution of raw holdings",
    "polite rate limit and identifiable User-Agent",
]


def _source_entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": "example_daily",
        "issuer": "example",
        "kind": "daily_holdings",
        "format": "csv",
        "url_patterns": ["https://example.com/<TICKER>.csv"],
        "update_frequency": "daily",
        "supported": True,
        "terms": {
            "url": "https://example.com/terms",
            "reviewer": None,
            "reviewed_on": None,
            "decision": "pending_review",
        },
    }
    entry.update(overrides)
    return entry


def _write_sources(path: Path, entries: list[dict[str, object]]) -> Path:
    path.write_text(yaml.safe_dump({"sources": entries}), encoding="utf-8")
    return path


_FUND_COLUMNS = (
    "ticker",
    "issuer",
    "name",
    "asset_class",
    "source_ids",
    "exchange",
    "params",
)


def _fund_row(**overrides: str) -> dict[str, str]:
    row = {
        "ticker": "AAA",
        "issuer": "x",
        "name": "X",
        "asset_class": "equity",
        "source_ids": "syn_csv",
        "exchange": "XTSE",
        "params": "",
    }
    row.update(overrides)
    return row


def _write_funds(path: Path, rows: list[dict[str, str]]) -> Path:
    header = ",".join(_FUND_COLUMNS) + "\n"
    body = "".join(",".join(row[column] for column in _FUND_COLUMNS) + "\n" for row in rows)
    path.write_text(header + body, encoding="utf-8")
    return path


class TestTermsRecordValidation:
    def test_allowed_with_conditions_requires_conditions(self) -> None:
        with pytest.raises(ValidationError, match="at least one condition"):
            TermsRecord(
                url="https://example.com/terms",
                reviewer="human",
                reviewed_on=date(2026, 10, 16),
                decision=Decision.allowed_with_conditions,
                conditions=[],
            )

    def test_reviewed_decision_requires_reviewer_and_date(self) -> None:
        with pytest.raises(ValidationError, match="reviewer, reviewed_on"):
            TermsRecord(url="https://example.com/terms", decision=Decision.allowed)

    def test_not_allowed_requires_reviewer_and_date(self) -> None:
        with pytest.raises(ValidationError, match="reviewer, reviewed_on"):
            TermsRecord(url="https://example.com/terms", decision=Decision.not_allowed)

    def test_pending_review_needs_no_review_fields(self) -> None:
        record = TermsRecord(decision=Decision.pending_review)
        assert record.reviewer is None
        assert record.url is None

    def test_non_positive_min_interval_rejected(self) -> None:
        with pytest.raises(ValidationError, match="min_interval_seconds must be positive"):
            TermsRecord(decision=Decision.pending_review, min_interval_seconds=0)


class TestLoadSources:
    def test_duplicate_source_ids_fail(self, tmp_path: Path) -> None:
        path = _write_sources(tmp_path / "sources.yaml", [_source_entry(), _source_entry()])
        with pytest.raises(ValueError, match="duplicate source id"):
            load_sources(path)

    def test_unknown_fields_fail(self, tmp_path: Path) -> None:
        entry = _source_entry(unexpected="value")
        path = _write_sources(tmp_path / "sources.yaml", [entry])
        with pytest.raises(ValidationError):
            load_sources(path)

    def test_missing_sources_key_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "sources.yaml"
        path.write_text("other: []\n", encoding="utf-8")
        with pytest.raises(ValueError, match="top-level 'sources' list"):
            load_sources(path)


class TestLoadFunds:
    def test_unknown_source_id_fails(self, tmp_path: Path) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        path = _write_funds(tmp_path / "funds.csv", [_fund_row(source_ids="nope")])
        with pytest.raises(ValueError, match="unknown source id"):
            load_funds(path, sources)

    def test_unknown_source_id_in_list_fails(self, tmp_path: Path) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        path = _write_funds(tmp_path / "funds.csv", [_fund_row(source_ids="syn_csv;nope")])
        with pytest.raises(ValueError, match="unknown source id 'nope'"):
            load_funds(path, sources)

    def test_empty_source_list_fails(self, tmp_path: Path) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        path = _write_funds(tmp_path / "funds.csv", [_fund_row(source_ids="")])
        with pytest.raises(ValueError, match="one or more"):
            load_funds(path, sources)

    def test_duplicate_ticker_fails(self, tmp_path: Path) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        row = _fund_row()
        path = _write_funds(tmp_path / "funds.csv", [row, row])
        with pytest.raises(ValueError, match="duplicate fund ticker"):
            load_funds(path, sources)

    def test_bad_asset_class_fails(self, tmp_path: Path) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        path = _write_funds(tmp_path / "funds.csv", [_fund_row(asset_class="crypto")])
        with pytest.raises(ValidationError):
            load_funds(path, sources)

    def test_multi_source_and_params_parse(self, tmp_path: Path) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        path = _write_funds(
            tmp_path / "funds.csv",
            [
                _fund_row(
                    ticker="AAA",
                    source_ids="syn_csv; syn_xlsx",
                    params="mode=test; label=two words",
                ),
                _fund_row(ticker="BBB", source_ids="syn_json", params=""),
            ],
        )
        funds = load_funds(path, sources)
        assert funds[0].source_ids == ("syn_csv", "syn_xlsx")
        assert funds[0].params == {"mode": "test", "label": "two words"}
        assert funds[1].params == {}

    def test_duplicate_source_id_in_list_fails(self, tmp_path: Path) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        path = _write_funds(tmp_path / "funds.csv", [_fund_row(source_ids="syn_csv;syn_csv")])
        with pytest.raises(ValueError, match="duplicate"):
            load_funds(path, sources)

    def test_malformed_params_fail(self, tmp_path: Path) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        path = _write_funds(tmp_path / "funds.csv", [_fund_row(params="novalue")])
        with pytest.raises(ValueError, match="key=value"):
            load_funds(path, sources)

    def test_duplicate_param_keys_fail(self, tmp_path: Path) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        path = _write_funds(tmp_path / "funds.csv", [_fund_row(params="a=1;a=2")])
        with pytest.raises(ValueError, match="duplicate key"):
            load_funds(path, sources)


class TestFundsForSource:
    def test_returns_funds_listing_the_source(self) -> None:
        def fund(ticker: str, source_ids: tuple[str, ...]) -> Fund:
            return Fund(
                ticker=ticker,
                issuer="x",
                name=ticker,
                asset_class="equity",
                source_ids=source_ids,
            )

        funds = [
            fund("AAA", ("syn_csv",)),
            fund("BBB", ("syn_csv", "syn_xlsx")),
            fund("CCC", ("syn_json",)),
        ]
        assert [item.ticker for item in funds_for_source(funds, "syn_csv")] == ["AAA", "BBB"]
        assert [item.ticker for item in funds_for_source(funds, "syn_xlsx")] == ["BBB"]
        assert funds_for_source(funds, "missing") == []


class TestCommittedConfig:
    def test_sources_load_with_recorded_decisions(self) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        assert len(sources) == 26
        real = {
            source_id: source
            for source_id, source in sources.items()
            if source.issuer != "synthetic"
        }
        assert set(real) == (
            ALLOWED_REAL_SOURCES | UNSUPPORTED_REAL_SOURCES | NOT_ALLOWED_REAL_SOURCES
        )
        for source_id, source in real.items():
            assert source.terms.reviewer == "Moksh Siruvani", source_id
            expected_reviewed_on = (
                date(2026, 10, 23) if source_id in METRICS_REAL_SOURCES else date(2026, 10, 17)
            )
            assert source.terms.reviewed_on == expected_reviewed_on, source_id
            assert source.terms.url, source_id
            if source_id in NOT_ALLOWED_REAL_SOURCES:
                assert source.terms.decision is Decision.not_allowed, source_id
                assert source.terms.conditions == [], source_id
                assert not is_collectable(source), source_id
                continue
            assert source.terms.decision is Decision.allowed_with_conditions, source_id
            missing = [
                condition
                for condition in COMMON_CONDITIONS
                if condition not in source.terms.conditions
            ]
            assert missing == [], source_id
            if source_id in UNSUPPORTED_REAL_SOURCES:
                assert source.supported is False, source_id
                assert not is_collectable(source), source_id
            else:
                assert is_collectable(source), source_id
        assert sources["vanguard_ca_daily"].supported is False
        assert sources["globalx_ca_daily"].supported is False

    def test_every_metrics_source_has_the_fund_metrics_kind(self) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        for source_id in METRICS_REAL_SOURCES:
            assert sources[source_id].kind is SourceKind.fund_metrics, source_id

    def test_metrics_gate_refuses_a_not_allowed_copy(self) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        for source_id in METRICS_REAL_SOURCES:
            source = sources[source_id]
            blocked = source.model_copy(
                update={
                    "terms": source.terms.model_copy(
                        update={
                            "decision": Decision.not_allowed,
                            "conditions": [],
                        }
                    )
                }
            )
            with pytest.raises(TermsNotAllowedError):
                require_collectable(blocked)

    def test_purpose_metrics_has_no_collector_and_that_is_fine(self) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        assert collector_for(sources["purpose_metrics"]) is None
        assert all(collector.source_id != "purpose_metrics" for collector in all_collectors())
        assert is_collectable(sources["purpose_metrics"])

    def test_every_not_allowed_real_source_is_refused(self) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        refused = 0
        for source in sources.values():
            if source.issuer == "synthetic":
                continue
            if source.terms.decision is not Decision.not_allowed:
                continue
            refused += 1
            with pytest.raises(TermsNotAllowedError):
                require_collectable(source)
        assert refused == len(NOT_ALLOWED_REAL_SOURCES)

    def test_synthetic_sources_are_gated(self) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        for source_id in ("syn_csv", "syn_xlsx", "syn_json", "syn_pdf"):
            assert is_collectable(sources[source_id]), source_id
        assert not is_collectable(sources["syn_blocked"])
        assert sources["syn_blocked"].terms.decision is Decision.not_allowed

    def test_funds_load_against_sources(self) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        funds = load_funds(CONFIG_DIR / "funds.csv", sources)
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
        assert funds[0].source_ids == ("syn_csv",)
        assert funds[0].params == {}


class TestTermsGate:
    def test_is_collectable_matrix(self) -> None:
        def source(decision: Decision, supported: bool) -> Source:
            has_conditions = decision is Decision.allowed_with_conditions
            conditions = ["max 1 request/second"] if has_conditions else []
            return Source(
                id="x",
                issuer="x",
                kind=SourceKind.synthetic,
                format=FileFormat.csv,
                url_patterns=["https://example.com/x.csv"],
                update_frequency="daily",
                supported=supported,
                terms=TermsRecord(
                    url="https://example.com/terms",
                    reviewer="human",
                    reviewed_on=date(2026, 10, 16),
                    decision=decision,
                    conditions=conditions,
                ),
            )

        assert is_collectable(source(Decision.allowed, supported=True))
        assert is_collectable(source(Decision.allowed_with_conditions, supported=True))
        assert not is_collectable(source(Decision.allowed, supported=False))
        assert not is_collectable(source(Decision.not_allowed, supported=True))
        assert not is_collectable(source(Decision.pending_review, supported=True))

    def test_require_collectable_message(self, source_factory: Callable[..., Source]) -> None:
        blocked = source_factory(decision=Decision.not_allowed, supported=True)
        with pytest.raises(TermsNotAllowedError) as excinfo:
            require_collectable(blocked)
        message = str(excinfo.value)
        assert message.startswith("skipped: terms")
        assert "syn_csv" in message
        assert "not_allowed" in message


@pytest.mark.parametrize(
    ("decision", "supported"),
    [
        (Decision.not_allowed, True),
        (Decision.pending_review, True),
        (Decision.allowed, False),
    ],
)
def test_collector_refuses_source_without_allowed_terms(
    decision: Decision,
    supported: bool,
    source_factory: Callable[..., Source],
    fake_clock: FakeClock,
) -> None:
    source = source_factory(decision=decision, supported=supported)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    client = PoliteClient(
        user_agent="cad-etf-holdings-tests (https://example.com)",
        client=httpx.Client(transport=transport),
        clock=fake_clock.time,
        sleep=fake_clock.sleep,
    )
    try:
        with pytest.raises(TermsNotAllowedError) as excinfo:
            client.get("https://example.com/holdings.csv", source=source)
    finally:
        client.close()
    assert requests == [], "no request may reach the transport for a non-allowed source"
    assert str(excinfo.value).startswith("skipped: terms")


def test_fund_metrics_kind_and_html_format_values() -> None:
    assert SourceKind.fund_metrics.value == "fund_metrics"
    assert FileFormat.html.value == "html"


def test_fund_default_exchange() -> None:
    fund = Fund(
        ticker="ABC",
        issuer="x",
        name="X",
        asset_class="equity",
        source_ids=("syn_csv",),
    )
    assert fund.exchange == "XTSE"
    assert fund.params == {}
