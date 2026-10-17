"""Tests for the source/fund registry and the terms gate."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError

from etfh.http import PoliteClient
from etfh.registry import (
    Fund,
    Source,
    TermsNotAllowedError,
    TermsRecord,
    is_collectable,
    load_funds,
    load_sources,
    require_collectable,
)
from etfh.types import Decision, FileFormat, SourceKind
from tests.conftest import FakeClock

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


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


def _write_funds(path: Path, rows: list[dict[str, str]]) -> Path:
    header = "ticker,issuer,name,asset_class,source_id,exchange\n"
    body = "".join(
        ",".join(
            row[column]
            for column in ("ticker", "issuer", "name", "asset_class", "source_id", "exchange")
        )
        + "\n"
        for row in rows
    )
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
        path = _write_funds(
            tmp_path / "funds.csv",
            [
                {
                    "ticker": "AAA",
                    "issuer": "x",
                    "name": "X",
                    "asset_class": "equity",
                    "source_id": "nope",
                    "exchange": "XTSE",
                }
            ],
        )
        with pytest.raises(ValueError, match="unknown source id"):
            load_funds(path, sources)

    def test_duplicate_ticker_fails(self, tmp_path: Path) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        row = {
            "ticker": "AAA",
            "issuer": "x",
            "name": "X",
            "asset_class": "equity",
            "source_id": "syn_csv",
            "exchange": "XTSE",
        }
        path = _write_funds(tmp_path / "funds.csv", [row, row])
        with pytest.raises(ValueError, match="duplicate fund ticker"):
            load_funds(path, sources)

    def test_bad_asset_class_fails(self, tmp_path: Path) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        row = {
            "ticker": "AAA",
            "issuer": "x",
            "name": "X",
            "asset_class": "crypto",
            "source_id": "syn_csv",
            "exchange": "XTSE",
        }
        path = _write_funds(tmp_path / "funds.csv", [row])
        with pytest.raises(ValidationError):
            load_funds(path, sources)


class TestCommittedConfig:
    def test_sources_load_and_candidates_are_pending_review(self) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        assert len(sources) == 21
        for source_id, source in sources.items():
            if source.issuer == "synthetic":
                continue
            assert source.terms.decision is Decision.pending_review, source_id
            assert source.terms.reviewer is None, source_id
            assert source.terms.reviewed_on is None, source_id
            assert source.terms.url, source_id

    def test_synthetic_sources_are_gated(self) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        for source_id in ("syn_csv", "syn_xlsx", "syn_json", "syn_pdf"):
            assert is_collectable(sources[source_id]), source_id
        assert not is_collectable(sources["syn_blocked"])
        assert sources["syn_blocked"].terms.decision is Decision.not_allowed

    def test_funds_load_against_sources(self) -> None:
        sources = load_sources(CONFIG_DIR / "sources.yaml")
        funds = load_funds(CONFIG_DIR / "funds.csv", sources)
        assert [fund.ticker for fund in funds] == [
            "SYNA",
            "SYNB",
            "SYNC",
            "SYND",
            "SYNE",
            "SYNF",
            "SYNX",
        ]
        assert all(fund.ticker.startswith("SYN") for fund in funds)


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


def test_fund_default_exchange() -> None:
    fund = Fund(
        ticker="ABC",
        issuer="x",
        name="X",
        asset_class="equity",
        source_id="syn_csv",
    )
    assert fund.exchange == "XTSE"
