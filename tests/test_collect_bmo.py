"""Tests for the BMO collector. Offline except the marked network smoke test."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from etfh.collect.bmo import COLLECTORS, BmoCollector, candidate_days
from etfh.collect.registry import CollectError, collector_for
from etfh.http import PoliteClient
from etfh.parse.bmo import BmoParser
from etfh.registry import Fund, Source, TermsNotAllowedError, load_sources
from etfh.storage import RawStore
from etfh.types import Decision
from tests.conftest import FakeClock

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
XLSX_BYTES = (
    Path(__file__).resolve().parent / "fixtures" / "issuers" / "bmo" / "equity.xlsx"
).read_bytes()
BASE = "/assets/static/reports/etf-funds-holdings/Holdings_Extract_en_US_EQLK_"
USER_AGENT = "cad-etf-holdings-tests (https://example.com)"


def _source() -> Source:
    return load_sources(CONFIG_DIR / "sources.yaml")["bmo_daily"]


def _fund(**overrides: object) -> Fund:
    values: dict[str, object] = {
        "ticker": "EQLK",
        "issuer": "bmo",
        "name": "BMO Lookalike Equity Index ETF",
        "asset_class": "equity",
        "source_ids": ("bmo_daily",),
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
    available: set[str],
    requests: list[httpx.Request] | None = None,
    *,
    etag: str | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if requests is not None:
            requests.append(request)
        stamp = request.url.path.removeprefix(BASE).removesuffix(".xlsx")
        if stamp not in available:
            return httpx.Response(404)
        if etag is not None and request.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        headers = {
            "content-type": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        }
        if etag is not None:
            headers["etag"] = etag
        return httpx.Response(200, content=XLSX_BYTES, headers=headers)

    return handler


def test_candidate_days_are_business_days() -> None:
    assert candidate_days(date(2026, 10, 19)) == [
        date(2026, 10, 19),
        date(2026, 10, 16),
        date(2026, 10, 15),
        date(2026, 10, 14),
        date(2026, 10, 13),
        date(2026, 10, 12),
    ]
    assert candidate_days(date(2026, 10, 17))[0] == date(2026, 10, 16)
    assert candidate_days(date(2026, 10, 18))[0] == date(2026, 10, 16)


def test_fetch_steps_back_to_the_latest_file(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = BmoCollector(source=_source())
    requests: list[httpx.Request] = []
    handler = _handler({"20261016"}, requests, etag='"b1"')
    with _client(handler, fake_clock) as client:
        raws = collector.fetch(_fund(), date(2026, 10, 19), client, store)
    assert len(raws) == 1
    raw = raws[0]
    assert raw.as_of_hint == date(2026, 10, 16)
    assert raw.path.read_bytes() == XLSX_BYTES
    stamps = [
        request.url.path.removeprefix(BASE).removesuffix(".xlsx")
        for request in requests
        if request.url.path.startswith(BASE)
    ]
    assert stamps == ["20261019", "20261016"]
    meta = store.read_meta(raw.path.parent / f"{raw.path.stem}.meta.json")
    assert meta["etag"] == '"b1"'
    assert meta["as_of_hint"] == "2026-10-16"
    parsed = BmoParser().parse(raw)
    assert parsed.as_of_date == date(2026, 10, 16)


def test_unchanged_file_gets_304_and_writes_nothing(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = BmoCollector(source=_source())
    requests: list[httpx.Request] = []
    handler = _handler({"20261016"}, requests, etag='"b1"')
    with _client(handler, fake_clock) as client:
        first = collector.fetch(_fund(), date(2026, 10, 16), client, store)
        second = collector.fetch(_fund(), date(2026, 10, 16), client, store)
    assert len(first) == 1
    assert second == []
    file_requests = [request for request in requests if request.url.path.startswith(BASE)]
    assert len(file_requests) == 2
    assert file_requests[1].headers["If-None-Match"] == '"b1"'
    assert len(store.iter_raw()) == 1


def test_newer_file_does_not_send_stale_conditional_headers(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    store = RawStore(tmp_path / "raw")
    collector = BmoCollector(source=_source())
    requests: list[httpx.Request] = []
    handler = _handler({"20261016", "20261019"}, requests, etag='"b1"')
    with _client(handler, fake_clock) as client:
        first = collector.fetch(_fund(), date(2026, 10, 16), client, store)
        second = collector.fetch(_fund(), date(2026, 10, 19), client, store)
    assert first[0].as_of_hint == date(2026, 10, 16)
    assert second[0].as_of_hint == date(2026, 10, 19)
    file_requests = [request for request in requests if request.url.path.startswith(BASE)]
    assert file_requests[-1].url.path.endswith("20261019.xlsx")
    assert file_requests[-1].headers.get("If-None-Match") is None
    assert len(store.iter_raw()) == 2


def test_no_file_in_six_candidates_is_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = BmoCollector(source=_source())
    requests: list[httpx.Request] = []
    with (
        _client(_handler(set(), requests), fake_clock) as client,
        pytest.raises(CollectError, match="no holdings file found"),
    ):
        collector.fetch(_fund(), date(2026, 10, 19), client, store)
    file_requests = [request for request in requests if request.url.path.startswith(BASE)]
    assert len(file_requests) == 6


def test_http_error_is_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = BmoCollector(source=_source())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(500)

    with _client(handler, fake_clock) as client, pytest.raises(CollectError, match="500"):
        collector.fetch(_fund(), date(2026, 10, 19), client, store)


def test_wrong_content_type_is_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = BmoCollector(source=_source())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, text="<html>blocked</html>", headers={"content-type": "text/html"}
        )

    with _client(handler, fake_clock) as client, pytest.raises(CollectError, match="content-type"):
        collector.fetch(_fund(), date(2026, 10, 19), client, store)


def test_non_zip_body_is_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = BmoCollector(source=_source())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            content=b"not an xlsx",
            headers={
                "content-type": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
            },
        )

    with _client(handler, fake_clock) as client, pytest.raises(CollectError, match="archive"):
        collector.fetch(_fund(), date(2026, 10, 19), client, store)


def test_gate_refuses_not_allowed_source_without_requests(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    source = _source()
    terms = source.terms.model_copy(update={"decision": Decision.not_allowed, "conditions": []})
    refused = source.model_copy(update={"terms": terms})
    store = RawStore(tmp_path / "raw")
    collector = BmoCollector(source=refused)
    requests: list[httpx.Request] = []
    with (
        _client(_handler({"20261016"}, requests), fake_clock) as client,
        pytest.raises(TermsNotAllowedError),
    ):
        collector.fetch(_fund(), date(2026, 10, 19), client, store)
    assert requests == []


def test_discovered_by_the_collector_registry() -> None:
    collector = collector_for(_source())
    assert isinstance(collector, BmoCollector)
    assert collector.source_id == "bmo_daily"


def test_collectors_registry_exports_the_collector() -> None:
    assert len(COLLECTORS) == 1
    assert isinstance(COLLECTORS[0], BmoCollector)


@pytest.mark.network
def test_live_bmo_fetch_and_parse(tmp_path: Path) -> None:
    if not os.environ.get("ETFH_USER_AGENT"):
        pytest.skip("ETFH_USER_AGENT is not set")
    source = _source()
    fund = _fund(ticker="ZSP", name="BMO S&P 500 Index ETF")
    store = RawStore(tmp_path / "raw")
    with PoliteClient() as client:
        raws = BmoCollector(source=source).fetch(fund, datetime.now(UTC).date(), client, store)
    assert len(raws) == 1
    assert raws[0].as_of_hint is not None
    parsed = BmoParser().parse(raws[0])
    assert parsed.as_of_date == raws[0].as_of_hint
    assert parsed.frame.height > 100
    assert parsed.notes[0] == "declared_unit=percent"
