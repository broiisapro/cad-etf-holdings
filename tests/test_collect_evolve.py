"""Tests for the Evolve collector. Offline except the marked network smoke test."""

from __future__ import annotations

import copy
import os
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from etfh.collect.evolve import COLLECTORS, EvolveCollector, holdings_date_from_page
from etfh.collect.registry import CollectError, collector_for
from etfh.http import PoliteClient
from etfh.parse.evolve import EvolveParser
from etfh.registry import Fund, Source, TermsNotAllowedError, load_sources
from etfh.storage import RawStore
from etfh.types import Decision
from tests.conftest import FakeClock

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "issuers" / "evolve"
CSV_BYTES = (FIXTURE_DIR / "lookalike.csv").read_bytes()
PAGE_BYTES = (FIXTURE_DIR / "lookalike_page.html").read_bytes()
PAGE_PATH = "/product/evlk"
CSV_PATH = "/wp-content/uploads/holdings/EVLK.csv"
USER_AGENT = "cad-etf-holdings-tests (https://example.com)"


def _source() -> Source:
    return load_sources(CONFIG_DIR / "sources.yaml")["evolve_daily"]


def _collector(source: Source | None = None) -> EvolveCollector:
    collector = EvolveCollector()
    collector.source = source
    return collector


def _fund(**overrides: object) -> Fund:
    values: dict[str, object] = {
        "ticker": "EVLK",
        "issuer": "evolve",
        "name": "Evolve Lookalike Innovation Index Fund",
        "asset_class": "equity",
        "source_ids": ("evolve_daily",),
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
    requests: list[httpx.Request] | None = None,
    *,
    csv_etag: str | None = None,
    page_body: bytes = PAGE_BYTES,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if requests is not None:
            requests.append(request)
        if request.url.path == PAGE_PATH:
            return httpx.Response(200, content=page_body, headers={"content-type": "text/html"})
        if request.url.path == CSV_PATH:
            if csv_etag is not None and request.headers.get("If-None-Match") == csv_etag:
                return httpx.Response(304)
            headers = {"content-type": "text/csv; charset=utf-8"}
            if csv_etag is not None:
                headers["etag"] = csv_etag
            return httpx.Response(200, content=CSV_BYTES, headers=headers)
        return httpx.Response(404)

    return handler


def test_fetch_stores_csv_with_page_date(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = _collector(_source())
    requests: list[httpx.Request] = []
    with _client(_handler(requests, csv_etag='"e1"'), fake_clock) as client:
        raws = collector.fetch(_fund(), date(2026, 10, 17), client, store)
    assert len(raws) == 1
    raw = raws[0]
    assert raw.as_of_hint == date(2026, 10, 7)
    assert raw.path.read_bytes() == CSV_BYTES
    assert raw.fund_ticker == "EVLK"
    assert [request.url.path for request in requests] == [PAGE_PATH, CSV_PATH]
    meta = store.read_meta(raw.path.parent / f"{raw.path.stem}.meta.json")
    assert meta["etag"] == '"e1"'
    parsed = EvolveParser().parse(raw)
    assert parsed.as_of_date == date(2026, 10, 7)


def test_unchanged_csv_gets_304_and_writes_nothing(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = _collector(_source())
    requests: list[httpx.Request] = []
    handler = _handler(requests, csv_etag='"e1"')
    with _client(handler, fake_clock) as client:
        first = collector.fetch(_fund(), date(2026, 10, 17), client, store)
        second = collector.fetch(_fund(), date(2026, 10, 17), client, store)
    assert len(first) == 1
    assert second == []
    csv_requests = [request for request in requests if request.url.path == CSV_PATH]
    assert len(csv_requests) == 2
    assert csv_requests[1].headers["If-None-Match"] == '"e1"'
    assert len(store.iter_raw()) == 1


def test_page_without_holdings_date_is_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = _collector(_source())
    html = b"<html><body><h4>Holdings</h4></body></html>"
    with (
        _client(_handler(page_body=html), fake_clock) as client,
        pytest.raises(CollectError, match="no 'Holdings As at' date"),
    ):
        collector.fetch(_fund(), date(2026, 10, 17), client, store)


def test_copy_bound_to_not_allowed_source_makes_no_requests(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    source = _source()
    assert source.terms.decision is Decision.allowed_with_conditions
    terms = source.terms.model_copy(update={"decision": Decision.not_allowed, "conditions": []})
    refused = source.model_copy(update={"terms": terms})
    collector = copy.copy(COLLECTORS[0])
    collector.source = refused
    requests: list[httpx.Request] = []
    store = RawStore(tmp_path / "raw")
    with (
        _client(_handler(requests), fake_clock) as client,
        pytest.raises(TermsNotAllowedError) as excinfo,
    ):
        collector.fetch(_fund(), date(2026, 10, 17), client, store)
    assert str(excinfo.value).startswith("skipped: terms")
    assert requests == []
    assert store.iter_raw() == []


def test_copy_bound_to_the_config_source_fetches(tmp_path: Path, fake_clock: FakeClock) -> None:
    collector = copy.copy(COLLECTORS[0])
    collector.source = _source()
    store = RawStore(tmp_path / "raw")
    with _client(_handler(), fake_clock) as client:
        raws = collector.fetch(_fund(), date(2026, 10, 17), client, store)
    assert len(raws) == 1
    assert len(store.iter_raw()) == 1


def test_unbound_collector_falls_back_to_the_default_config(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    collector = EvolveCollector()
    assert collector.source is None
    store = RawStore(tmp_path / "raw")
    with _client(_handler(), fake_clock) as client:
        raws = collector.fetch(_fund(), date(2026, 10, 17), client, store)
    assert len(raws) == 1
    assert collector.source is None


def test_binding_a_copy_does_not_leak_into_other_copies() -> None:
    first = copy.copy(COLLECTORS[0])
    first.source = _source()
    second = copy.copy(COLLECTORS[0])
    assert second.source is None
    assert COLLECTORS[0].source is None


def test_page_http_error_is_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = _collector(_source())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(500)

    with _client(handler, fake_clock) as client, pytest.raises(CollectError, match="500"):
        collector.fetch(_fund(), date(2026, 10, 17), client, store)


def test_csv_http_error_is_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = _collector(_source())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == PAGE_PATH:
            return httpx.Response(200, content=PAGE_BYTES, headers={"content-type": "text/html"})
        return httpx.Response(500)

    with _client(handler, fake_clock) as client, pytest.raises(CollectError, match="500"):
        collector.fetch(_fund(), date(2026, 10, 17), client, store)


def test_gate_refuses_not_allowed_source_without_requests(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    source = _source()
    terms = source.terms.model_copy(update={"decision": Decision.not_allowed, "conditions": []})
    refused = source.model_copy(update={"terms": terms})
    store = RawStore(tmp_path / "raw")
    collector = _collector(refused)
    requests: list[httpx.Request] = []
    with _client(_handler(requests), fake_clock) as client, pytest.raises(TermsNotAllowedError):
        collector.fetch(_fund(), date(2026, 10, 17), client, store)
    assert requests == []


def test_non_csv_content_type_is_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    collector = _collector(_source())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == PAGE_PATH:
            return httpx.Response(200, content=PAGE_BYTES, headers={"content-type": "text/html"})
        return httpx.Response(
            200, text="<html>blocked</html>", headers={"content-type": "text/html"}
        )

    with _client(handler, fake_clock) as client, pytest.raises(CollectError, match="content-type"):
        collector.fetch(_fund(), date(2026, 10, 17), client, store)


def test_holdings_date_from_page_prefers_the_holdings_date() -> None:
    assert holdings_date_from_page(PAGE_BYTES.decode("utf-8")) == date(2026, 10, 7)
    assert holdings_date_from_page("<p>no dates here</p>") is None


def test_discovered_by_the_collector_registry() -> None:
    collector = collector_for(_source())
    assert isinstance(collector, EvolveCollector)
    assert collector.source_id == "evolve_daily"


def test_collectors_registry_exports_the_collector() -> None:
    assert len(COLLECTORS) == 1
    assert isinstance(COLLECTORS[0], EvolveCollector)


@pytest.mark.network
def test_live_evolve_fetch_and_parse(tmp_path: Path) -> None:
    if not os.environ.get("ETFH_USER_AGENT"):
        pytest.skip("ETFH_USER_AGENT is not set")
    source = _source()
    fund = _fund(
        ticker="CYBR",
        name="Evolve Cyber Security Index Fund",
    )
    store = RawStore(tmp_path / "raw")
    with PoliteClient() as client:
        raws = _collector(source).fetch(fund, datetime.now(UTC).date(), client, store)
    assert len(raws) == 1
    assert raws[0].as_of_hint is not None
    parsed = EvolveParser().parse(raws[0])
    assert parsed.as_of_date == raws[0].as_of_hint
    assert parsed.frame.height > 10
    assert parsed.notes[0] == "declared_unit=fraction"
