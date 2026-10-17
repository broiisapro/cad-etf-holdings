"""Collector tests for BMO quarterly disclosures."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from etfh.collect.bmo_qpd import COLLECTORS, BmoQpdCollector, listing_links
from etfh.collect.registry import CollectError
from etfh.http import PoliteClient
from etfh.registry import TermsNotAllowedError
from etfh.storage import RawStore
from etfh.types import Decision
from tests._qpd_helpers import qpd_fund, qpd_source, write_bmo_lookalike
from tests.conftest import FakeClock

TICKER = "ZSP"
OTHER_TICKER = "ZAG"
PDF_URL = "https://bmo.bynder.com/m/0123456789abcdef/original/bmoZSP_qpd-Eng.pdf"
OTHER_URL = "https://bmo.bynder.com/m/fedcba9876543210/original/bmoZAG_qpd-Eng.pdf"


def _listing_html() -> str:
    decoys = [
        (
            f'<a href="https://bmo.bynder.com/m/{index:016x}/original/'
            f'bmoZ{index % 90:02d}Q_qpd-Eng.pdf">Fund Z{index % 90:02d}Q</a>'
        )
        for index in range(300)
    ]
    decoys.extend(
        [
            '<a href="https://bmo.bynder.com/m/aaaa/original/bmoZSP_qpd-Fr.pdf">ZSP French</a>',
            '<a href="https://bmo.bynder.com/m/bbbb/original/bmoZSPx_qpd-Eng.pdf">ZSPx English</a>',
            f'<a href="{PDF_URL}">BMO S&amp;P 500 Index ETF</a>',
            f'<a href="{OTHER_URL}">BMO Aggregate Bond Index ETF</a>',
        ]
    )
    return "<html><body>" + "\n".join(decoys) + "</body></html>"


def _source() -> object:
    return qpd_source("bmo_qpd", issuer="bmo")


def _client(handler: httpx.MockTransport, clock: FakeClock | None = None) -> PoliteClient:
    return PoliteClient(
        "etfh-tests (test@example.com)",
        client=httpx.Client(transport=handler, follow_redirects=True),
        clock=clock.time if clock is not None else None,
        sleep=clock.sleep if clock is not None else None,
    )


def _handler(pdf: bytes, *, listing: str | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path.endswith("legal-and-regulatory-documents/"):
            return httpx.Response(200, text=listing if listing is not None else _listing_html())
        if request.url.host == "bmo.bynder.com":
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_collector_is_registered() -> None:
    assert [collector.source_id for collector in COLLECTORS] == ["bmo_qpd"]


def test_listing_links_are_absolute_pdfs() -> None:
    links = listing_links(_listing_html())
    assert PDF_URL in links
    assert OTHER_URL in links
    assert len(links) == 304


def test_exact_ticker_and_english_name_win_over_decoys() -> None:
    collector = BmoQpdCollector(source=_source())
    collector._listing = listing_links(_listing_html())
    assert collector.disclosure_url(TICKER) == PDF_URL
    assert collector.disclosure_url(OTHER_TICKER) == OTHER_URL


def test_fetch_loads_listing_once_and_stores_pdfs(tmp_path: Path, fake_clock: FakeClock) -> None:
    pdf = write_bmo_lookalike(tmp_path / "bmo.pdf").read_bytes()
    listing_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path.endswith("legal-and-regulatory-documents/"):
            listing_requests.append(request.url.path)
            return httpx.Response(200, text=_listing_html())
        if request.url.host == "bmo.bynder.com":
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    collector = BmoQpdCollector(source=_source())
    store = RawStore(tmp_path / "raw")
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        first = collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, store)
        second = collector.fetch(
            qpd_fund(OTHER_TICKER, source_id="bmo_qpd"), date(2026, 7, 1), client, store
        )
    finally:
        client.close()
    assert len(listing_requests) == 1
    assert first[0].url == PDF_URL
    assert second[0].url == OTHER_URL
    assert first[0].path.read_bytes() == pdf
    assert second[0].path.read_bytes() == pdf


def test_gate_refusal_does_not_touch_the_network(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nAllow: /")

    collector = BmoQpdCollector(source=qpd_source("bmo_qpd", decision=Decision.not_allowed))
    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(TermsNotAllowedError, match="skipped: terms"):
            collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()
    assert calls == []


def test_missing_ticker_is_a_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    collector = BmoQpdCollector(source=_source())
    client = _client(_handler(b"%PDF-1.4"), fake_clock)
    try:
        with pytest.raises(CollectError, match="no bmoNOPE_qpd-Eng.pdf"):
            collector.fetch(qpd_fund("NOPE"), date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()


def test_robots_refusal_on_listing_is_a_collect_error(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(403, text="Access Denied")
        return httpx.Response(200, text=_listing_html())

    collector = BmoQpdCollector(source=_source())
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        with pytest.raises(CollectError, match="robots.txt disallows"):
            collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()


def test_non_pdf_response_is_a_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    collector = BmoQpdCollector(source=_source())
    client = _client(_handler(b"<html>challenge</html>"), fake_clock)
    try:
        with pytest.raises(CollectError, match="not a PDF"):
            collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()


def test_not_modified_returns_no_new_file(tmp_path: Path, fake_clock: FakeClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path.endswith("legal-and-regulatory-documents/"):
            return httpx.Response(200, text=_listing_html())
        return httpx.Response(304)

    collector = BmoQpdCollector(source=_source())
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        assert collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, RawStore(tmp_path)) == []
    finally:
        client.close()
