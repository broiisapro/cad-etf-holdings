"""Tests for the Purpose collector. Offline except the marked network smoke test."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from etfh.collect.purpose import COLLECTORS, CollectError, PurposeCollector
from etfh.http import PoliteClient
from etfh.parse.purpose import PurposeParser
from etfh.registry import Fund, Source, TermsNotAllowedError, load_sources
from etfh.storage import RawStore
from etfh.types import Decision
from tests.conftest import FakeClock

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issuers" / "purpose" / "lookalike.json"
PAYLOAD = FIXTURE.read_bytes()
API_PATH = "/api/funds/purpose-lookalike-income-fund"
USER_AGENT = "cad-etf-holdings-tests (https://example.com)"


def _source() -> Source:
    return load_sources(CONFIG_DIR / "sources.yaml")["purpose_daily"]


def _fund(**overrides: object) -> Fund:
    values: dict[str, object] = {
        "ticker": "PURA",
        "issuer": "purpose",
        "name": "Purpose Lookalike Income Fund",
        "asset_class": "equity",
        "source_ids": ("purpose_daily",),
        "params": {"purpose_slug": "purpose-lookalike-income-fund"},
    }
    values.update(overrides)
    return Fund.model_validate(values)


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    fake_clock: FakeClock,
) -> PoliteClient:
    return PoliteClient(
        user_agent=USER_AGENT,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=fake_clock.time,
        sleep=fake_clock.sleep,
    )


def _handler(
    response: httpx.Response | None = None,
    requests: list[httpx.Request] | None = None,
    *,
    etag: str | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if requests is not None:
            requests.append(request)
        if etag is not None and request.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        assert response is not None
        return response

    return handler


def _json_response(etag: str | None = None) -> httpx.Response:
    headers = {"content-type": "application/json"}
    if etag is not None:
        headers["etag"] = etag
    return httpx.Response(200, content=PAYLOAD, headers=headers)


def test_fetch_stores_document_with_as_of_hint(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = PurposeCollector(source=_source())
    with _client(_handler(_json_response(etag='"v1"')), fake_clock) as client:
        raws = collector.fetch(_fund(), date(2026, 10, 17), client, store)
    assert len(raws) == 1
    raw = raws[0]
    assert raw.as_of_hint == date(2026, 10, 16)
    assert raw.fund_ticker == "PURA"
    assert raw.path.read_bytes() == PAYLOAD
    assert raw.content_type == "application/json"
    meta = store.read_meta(raw.path.parent / f"{raw.path.stem}.meta.json")
    assert meta["etag"] == '"v1"'
    assert meta["http_status"] == 200


def test_unchanged_document_gets_304_and_writes_nothing(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    store = RawStore(tmp_path / "raw")
    collector = PurposeCollector(source=_source())
    requests: list[httpx.Request] = []
    handler = _handler(_json_response(etag='"v1"'), requests, etag='"v1"')
    with _client(handler, fake_clock) as client:
        first = collector.fetch(_fund(), date(2026, 10, 17), client, store)
        second = collector.fetch(_fund(), date(2026, 10, 17), client, store)
    assert len(first) == 1
    assert second == []
    assert len(requests) == 2
    assert requests[1].headers["If-None-Match"] == '"v1"'
    assert len(store.iter_raw()) == 1


def test_identical_bytes_without_304_deduplicate(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = PurposeCollector(source=_source())
    with _client(_handler(_json_response()), fake_clock) as client:
        first = collector.fetch(_fund(), date(2026, 10, 17), client, store)
        second = collector.fetch(_fund(), date(2026, 10, 17), client, store)
    assert len(first) == 1
    assert len(second) == 1
    assert first[0].path == second[0].path
    assert len(store.iter_raw()) == 1


def test_gate_refuses_not_allowed_source_without_requests(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    source = _source()
    terms = source.terms.model_copy(update={"decision": Decision.not_allowed, "conditions": []})
    refused = source.model_copy(update={"terms": terms})
    requests: list[httpx.Request] = []
    store = RawStore(tmp_path / "raw")
    collector = PurposeCollector(source=refused)
    with (
        _client(_handler(_json_response(), requests), fake_clock) as client,
        pytest.raises(TermsNotAllowedError) as excinfo,
    ):
        collector.fetch(_fund(), date(2026, 10, 17), client, store)
    assert str(excinfo.value).startswith("skipped: terms")
    assert requests == []
    assert store.iter_raw() == []


def test_http_error_becomes_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = PurposeCollector(source=_source())
    with (
        _client(_handler(httpx.Response(500)), fake_clock) as client,
        pytest.raises(CollectError) as excinfo,
    ):
        collector.fetch(_fund(), date(2026, 10, 17), client, store)
    assert excinfo.value.source_id == "purpose_daily"
    assert excinfo.value.fund_ticker == "PURA"
    assert "500" in excinfo.value.reason


def test_wrong_content_type_becomes_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = PurposeCollector(source=_source())
    response = httpx.Response(
        200, text="<html>blocked</html>", headers={"content-type": "text/html"}
    )
    with (
        _client(_handler(response), fake_clock) as client,
        pytest.raises(CollectError, match="content-type"),
    ):
        collector.fetch(_fund(), date(2026, 10, 17), client, store)


def test_malformed_json_becomes_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = PurposeCollector(source=_source())
    response = httpx.Response(200, text="not json", headers={"content-type": "application/json"})
    with (
        _client(_handler(response), fake_clock) as client,
        pytest.raises(CollectError, match="malformed"),
    ):
        collector.fetch(_fund(), date(2026, 10, 17), client, store)


def test_missing_slug_raises(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = PurposeCollector(source=_source())
    with (
        _client(_handler(_json_response()), fake_clock) as client,
        pytest.raises(CollectError, match="purpose_slug"),
    ):
        collector.fetch(_fund(params={}), date(2026, 10, 17), client, store)


def test_collectors_registry_exports_the_collector() -> None:
    assert len(COLLECTORS) == 1
    assert isinstance(COLLECTORS[0], PurposeCollector)
    assert COLLECTORS[0].source_id == "purpose_daily"


def test_unknown_fund_document_is_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = PurposeCollector(source=_source())
    response = httpx.Response(
        200,
        content=b'{"portfolio": {"Level4": {"holdings": []}}}',
        headers={"content-type": "application/json"},
    )
    with (
        _client(_handler(response), fake_clock) as client,
        pytest.raises(CollectError, match="malformed"),
    ):
        collector.fetch(_fund(), date(2026, 10, 17), client, store)


@pytest.mark.network
def test_live_purpose_fetch_and_parse(tmp_path: Path) -> None:
    if not os.environ.get("ETFH_USER_AGENT"):
        pytest.skip("ETFH_USER_AGENT is not set")
    source = _source()
    fund = _fund(
        ticker="PDF",
        name="Purpose Core Dividend Fund",
        params={"purpose_slug": "purpose-core-dividend-fund"},
    )
    store = RawStore(tmp_path / "raw")
    with PoliteClient() as client:
        raws = PurposeCollector(source=source).fetch(fund, datetime.now(UTC).date(), client, store)
    assert len(raws) == 1
    assert raws[0].as_of_hint == date.fromisoformat(fund_dt(raws[0].path))
    parsed = PurposeParser().parse(raws[0])
    assert parsed.frame.height > 10
    assert parsed.notes[0] == "declared_unit=percent"


def fund_dt(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return str(payload["portfolio"]["dt"])
