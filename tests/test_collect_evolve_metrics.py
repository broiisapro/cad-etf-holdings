"""Tests for the Evolve product-page metrics collector (offline except smoke)."""

from __future__ import annotations

import copy
import os
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from etfh.collect.evolve_metrics import (
    COLLECTORS,
    EvolveMetricsCollector,
    metrics_page_url,
    nav_date_from_html,
)
from etfh.collect.registry import CollectError, collector_for
from etfh.http import PoliteClient
from etfh.parse.evolve_metrics import EvolveMetricsParser
from etfh.registry import Fund, Source, TermsNotAllowedError, load_sources
from etfh.storage import RawStore
from etfh.types import Decision
from tests._metrics_helpers import EVOLVE_PAGE
from tests.conftest import FakeClock

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
USER_AGENT = "cad-etf-holdings-tests (https://example.com)"
HTML_BYTES = EVOLVE_PAGE.read_bytes()


def _source() -> Source:
    return load_sources(CONFIG_DIR / "sources.yaml")["evolve_metrics"]


def _collector(source: Source | None = None) -> EvolveMetricsCollector:
    collector = EvolveMetricsCollector()
    collector.source = source
    return collector


def _fund(**overrides: object) -> Fund:
    values: dict[str, object] = {
        "ticker": "EVMF",
        "issuer": "evolve",
        "name": "Evolve Lookalike Enhanced Yield Fund",
        "asset_class": "equity",
        "source_ids": ("evolve_metrics",),
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
    html_bytes: bytes,
    *,
    requests: list[httpx.Request] | None = None,
    etag: str | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if requests is not None:
            requests.append(request)
        if etag is not None and request.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        headers = {"content-type": "text/html; charset=UTF-8"}
        if etag is not None:
            headers["etag"] = etag
        return httpx.Response(200, content=html_bytes, headers=headers)

    return handler


def test_page_url_uses_lower_case_ticker() -> None:
    assert metrics_page_url(_fund(ticker="EVMF")) == "https://evolveetfs.com/product/evmf"


def test_nav_date_reads_the_first_as_at_date() -> None:
    assert nav_date_from_html(EVOLVE_PAGE.read_text()) == date(2026, 10, 22)


def test_fetch_stores_html_and_dates_it(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    requests: list[httpx.Request] = []
    handler = _handler(HTML_BYTES, requests=requests, etag='"e1"')
    with _client(handler, fake_clock) as client:
        raws = _collector(_source()).fetch(_fund(), date(2026, 10, 23), client, store)
    assert len(raws) == 1
    raw = raws[0]
    assert raw.as_of_hint == date(2026, 10, 22)
    assert raw.ext == "html"
    assert raw.path.read_bytes() == HTML_BYTES
    meta = store.read_meta(raw.path.parent / f"{raw.path.stem}.meta.json")
    assert meta["etag"] == '"e1"'
    parsed = EvolveMetricsParser().parse(raw)
    assert parsed.as_of_date == date(2026, 10, 22)
    assert parsed.frame["nav_per_unit"][0] == pytest.approx(31.42)
    assert requests[0].url.path == "/product/evmf"


def test_unchanged_page_gets_304(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    handler = _handler(HTML_BYTES, etag='"e1"')
    with _client(handler, fake_clock) as client:
        first = _collector(_source()).fetch(_fund(), date(2026, 10, 23), client, store)
        second = _collector(_source()).fetch(_fund(), date(2026, 10, 23), client, store)
    assert len(first) == 1
    assert second == []
    assert len(store.iter_raw()) == 1


def test_missing_metrics_cards_is_a_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    html = EVOLVE_PAGE.read_text().replace('data-key="aum"', 'data-key="missing"')

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=UTF-8"})

    with (
        _client(handler, fake_clock) as client,
        pytest.raises(CollectError, match="no NAV/AUM cards"),
    ):
        _collector(_source()).fetch(_fund(), date(2026, 10, 23), client, RawStore(tmp_path))


def test_wrong_content_type_is_a_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})

    with (
        _client(handler, fake_clock) as client,
        pytest.raises(CollectError, match="expected HTML"),
    ):
        _collector(_source()).fetch(_fund(), date(2026, 10, 23), client, RawStore(tmp_path))


def test_gate_refuses_not_allowed_source_without_requests(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    source = _source()
    terms = source.terms.model_copy(update={"decision": Decision.not_allowed, "conditions": []})
    refused = source.model_copy(update={"terms": terms})
    requests: list[httpx.Request] = []
    with (
        _client(_handler(HTML_BYTES, requests=requests), fake_clock) as client,
        pytest.raises(TermsNotAllowedError),
    ):
        _collector(refused).fetch(_fund(), date(2026, 10, 23), client, RawStore(tmp_path))
    assert requests == []


def test_registry_discovers_and_binds_the_collector() -> None:
    collector = collector_for(_source())
    assert isinstance(collector, EvolveMetricsCollector)
    assert collector.source_id == "evolve_metrics"
    assert collector.source is not None
    assert COLLECTORS[0].source is None


def test_copy_binding_does_not_leak() -> None:
    first = copy.copy(COLLECTORS[0])
    first.source = _source()
    assert COLLECTORS[0].source is None


@pytest.mark.network
def test_live_evolve_page_fetch_and_parse(tmp_path: Path) -> None:
    if not os.environ.get("ETFH_USER_AGENT"):
        pytest.skip("ETFH_USER_AGENT is not set")
    fund = _fund(ticker="ESPX", name="Evolve S&P 500 Enhanced Yield Fund")
    store = RawStore(tmp_path / "raw")
    with PoliteClient() as client:
        raws = _collector(_source()).fetch(fund, datetime.now(UTC).date(), client, store)
    assert len(raws) == 1
    parsed = EvolveMetricsParser().parse(raws[0])
    assert parsed.frame["nav_per_unit"][0] > 0
