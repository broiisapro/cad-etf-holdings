"""Tests for the BMO factsheet metrics collector (offline except the smoke test)."""

from __future__ import annotations

import copy
import os
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from etfh.collect.bmo_metrics import COLLECTORS, BmoMetricsCollector, factsheet_stem, factsheet_url
from etfh.collect.registry import CollectError, collector_for
from etfh.http import PoliteClient
from etfh.parse.bmo_metrics import BmoMetricsParser
from etfh.registry import Fund, Source, TermsNotAllowedError, load_funds, load_sources
from etfh.storage import RawStore
from etfh.types import Decision
from tests._metrics_helpers import bmo_factsheet_pdf
from tests.conftest import FakeClock

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
USER_AGENT = "cad-etf-holdings-tests (https://example.com)"


def _source() -> Source:
    return load_sources(CONFIG_DIR / "sources.yaml")["bmo_metrics"]


def _collector(source: Source | None = None) -> BmoMetricsCollector:
    collector = BmoMetricsCollector()
    collector.source = source
    return collector


def _fund(**overrides: object) -> Fund:
    values: dict[str, object] = {
        "ticker": "BMLK",
        "issuer": "bmo",
        "name": "BMO Lookalike Index ETF",
        "asset_class": "equity",
        "source_ids": ("bmo_metrics",),
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
    pdf_bytes: bytes,
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
        headers = {"content-type": "application/pdf"}
        if etag is not None:
            headers["etag"] = etag
        return httpx.Response(200, content=pdf_bytes, headers=headers)

    return handler


def test_factsheet_url_encodes_name_and_ticker() -> None:
    fund = _fund(name="BMO S&P/TSX Lookalike Index ETF", ticker="BMLK")
    assert factsheet_url(fund).endswith(
        "BMO%20S%26P%2FTSX%20Lookalike%20Index%20ETF_Factsheet_EN_BMLK.pdf"
    )


def test_factsheet_stem_falls_back_to_the_display_name() -> None:
    fund = _fund(name="BMO S&P 500 Lookalike Index ETF")
    assert factsheet_stem(fund) == "BMO S&P 500 Lookalike Index ETF"
    assert factsheet_url(fund).endswith(
        "BMO%20S%26P%20500%20Lookalike%20Index%20ETF_Factsheet_EN_BMLK.pdf"
    )


def test_factsheet_stem_prefers_the_published_name() -> None:
    # BMO still serves ZQQ's factsheet under the pre-rename "NASDAQ" spelling,
    # so the fund records the stem BMO publishes instead of its display name.
    fund = _fund(
        name="BMO Nasq 100 Lookalike Index ETF",
        ticker="BMLQ",
        params={"bmo_factsheet": "BMO NASQ 100 Lookalike Index ETF"},
    )
    assert factsheet_stem(fund) == "BMO NASQ 100 Lookalike Index ETF"
    assert factsheet_url(fund).endswith(
        "BMO%20NASQ%20100%20Lookalike%20Index%20ETF_Factsheet_EN_BMLQ.pdf"
    )
    assert "Nasq%20100" not in factsheet_url(fund)


def test_fetch_requests_the_published_stem(tmp_path: Path, fake_clock: FakeClock) -> None:
    pdf_bytes = bmo_factsheet_pdf(tmp_path).read_bytes()
    store = RawStore(tmp_path / "raw")
    requests: list[httpx.Request] = []
    fund = _fund(
        name="BMO Nasq 100 Lookalike Index ETF",
        ticker="BMLQ",
        params={"bmo_factsheet": "BMO NASQ 100 Lookalike Index ETF"},
    )
    with _client(_handler(pdf_bytes, requests=requests), fake_clock) as client:
        _collector(_source()).fetch(fund, date(2026, 10, 23), client, store)
    assert str(requests[0].url).endswith(
        "/BMO%20NASQ%20100%20Lookalike%20Index%20ETF_Factsheet_EN_BMLQ.pdf"
    )


def test_fetch_stores_pdf_and_dates_it(tmp_path: Path, fake_clock: FakeClock) -> None:
    pdf_bytes = bmo_factsheet_pdf(tmp_path).read_bytes()
    store = RawStore(tmp_path / "raw")
    requests: list[httpx.Request] = []
    handler = _handler(pdf_bytes, requests=requests, etag='"b1"')
    with _client(handler, fake_clock) as client:
        raws = _collector(_source()).fetch(_fund(), date(2026, 10, 23), client, store)
    assert len(raws) == 1
    raw = raws[0]
    assert raw.as_of_hint == date(2026, 9, 30)
    assert raw.ext == "pdf"
    assert raw.path.read_bytes() == pdf_bytes
    meta = store.read_meta(raw.path.parent / f"{raw.path.stem}.meta.json")
    assert meta["etag"] == '"b1"'
    assert meta["as_of_hint"] == "2026-09-30"
    parsed = BmoMetricsParser().parse(raw)
    assert parsed.frame.height == 1
    assert parsed.frame["aum"][0] == pytest.approx(25_592_960_000.0)
    assert requests[0].url.path.endswith("_Factsheet_EN_BMLK.pdf")


def test_unchanged_factsheet_gets_304(tmp_path: Path, fake_clock: FakeClock) -> None:
    pdf_bytes = bmo_factsheet_pdf(tmp_path).read_bytes()
    store = RawStore(tmp_path / "raw")
    handler = _handler(pdf_bytes, etag='"b1"')
    with _client(handler, fake_clock) as client:
        first = _collector(_source()).fetch(_fund(), date(2026, 10, 23), client, store)
        second = _collector(_source()).fetch(_fund(), date(2026, 10, 23), client, store)
    assert len(first) == 1
    assert second == []
    assert len(store.iter_raw()) == 1


def test_missing_factsheet_is_a_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(404)

    with (
        _client(handler, fake_clock) as client,
        pytest.raises(CollectError, match="bmo_factsheet"),
    ):
        _collector(_source()).fetch(_fund(), date(2026, 10, 23), client, RawStore(tmp_path))


def test_non_pdf_is_a_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, text="<html>blocked</html>", headers={"content-type": "text/html"}
        )

    with (
        _client(handler, fake_clock) as client,
        pytest.raises(CollectError, match="expected a PDF"),
    ):
        _collector(_source()).fetch(_fund(), date(2026, 10, 23), client, RawStore(tmp_path))


def test_gate_refuses_not_allowed_source_without_requests(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    source = _source()
    terms = source.terms.model_copy(update={"decision": Decision.not_allowed, "conditions": []})
    refused = source.model_copy(update={"terms": terms})
    requests: list[httpx.Request] = []
    pdf_bytes = bmo_factsheet_pdf(tmp_path).read_bytes()
    with (
        _client(_handler(pdf_bytes, requests=requests), fake_clock) as client,
        pytest.raises(TermsNotAllowedError),
    ):
        _collector(refused).fetch(_fund(), date(2026, 10, 23), client, RawStore(tmp_path))
    assert requests == []


def test_registry_discovers_and_binds_the_collector() -> None:
    collector = collector_for(_source())
    assert isinstance(collector, BmoMetricsCollector)
    assert collector.source_id == "bmo_metrics"
    assert collector.source is not None
    assert COLLECTORS[0].source is None


def test_copy_binding_does_not_leak() -> None:
    first = copy.copy(COLLECTORS[0])
    first.source = _source()
    assert COLLECTORS[0].source is None


@pytest.mark.network
def test_live_bmo_factsheet_fetch_and_parse(tmp_path: Path) -> None:
    if not os.environ.get("ETFH_USER_AGENT"):
        pytest.skip("ETFH_USER_AGENT is not set")
    fund = _fund(ticker="ZSP", name="BMO S&P 500 Index ETF")
    store = RawStore(tmp_path / "raw")
    with PoliteClient() as client:
        raws = _collector(_source()).fetch(fund, datetime.now(UTC).date(), client, store)
    assert len(raws) == 1
    parsed = BmoMetricsParser().parse(raws[0])
    assert parsed.as_of_date == raws[0].as_of_hint
    assert parsed.frame["shares_outstanding"][0] > 0


@pytest.mark.network
def test_every_committed_bmo_fund_has_a_published_factsheet() -> None:
    """The stems in ``funds.csv`` are the ones BMO publishes, not our display names."""
    if not os.environ.get("ETFH_USER_AGENT"):
        pytest.skip("ETFH_USER_AGENT is not set")
    funds = [
        fund
        for fund in load_funds(CONFIG_DIR / "funds.csv", load_sources(CONFIG_DIR / "sources.yaml"))
        if "bmo_metrics" in fund.source_ids
    ]
    source = _source()
    missing: list[str] = []
    with PoliteClient() as client:
        for fund in funds:
            url = factsheet_url(fund)
            try:
                response = client.get(url, source=source)
            except httpx.HTTPError as exc:  # pragma: no cover - live network only
                missing.append(f"{fund.ticker}: {exc}")
                continue
            if response.status_code != 200 or not response.content.startswith(b"%PDF"):
                missing.append(f"{fund.ticker}: HTTP {response.status_code} at {url}")
    assert missing == []
