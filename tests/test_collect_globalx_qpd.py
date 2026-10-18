"""Collector tests for Global X Canada quarterly disclosures."""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path

import httpx
import pytest

from etfh.collect.globalx_qpd import COLLECTORS, GlobalXQpdCollector, quarterly_links
from etfh.collect.registry import CollectError
from etfh.http import PoliteClient
from etfh.registry import TermsNotAllowedError
from etfh.storage import RawStore
from etfh.types import Decision
from tests._qpd_helpers import (
    qpd_fund,
    qpd_source,
    write_globalx_lookalike,
)
from tests.conftest import FakeClock

TICKER = "GXF"
PRODUCT_PATH = "/product/GXF"
PDF_2026 = "/wp-content/uploads/2024/04/GXF-GXF.U-SOI-Summary-2026_Q1.pdf"
PDF_2025 = "/wp-content/uploads/2025/12/GXF-GXF.U-SOI-Summary-2025_Q3.pdf"
PDF_FRENCH = "/wp-content/uploads/2024/04/GXF-FR-SOI-Summary-2026_Q1.pdf"

PRODUCT_HTML = f"""
<html><body>
<a href="{PDF_2025}">2025 Q3 Summary</a>
<a href="{PDF_FRENCH}">Résumé 2026 Q1</a>
<a href="{PDF_2026}">2026 Q1 Summary</a>
<a href="/wp-content/uploads/2026/08/GXF-Proxy-2026.pdf">2026 Proxy Vote Summary</a>
<a href="/wp-content/uploads/2026/04/GXF-GXF.U-EN-AR2025.pdf">2025 Annual Report</a>
</body></html>
"""


def _source() -> object:
    return qpd_source(
        "globalx_ca_qpd",
        issuer="globalx_ca",
        min_interval_seconds=30,
    )


def _client(handler: httpx.MockTransport, clock: FakeClock | None = None) -> PoliteClient:
    return PoliteClient(
        "etfh-tests (test@example.com)",
        client=httpx.Client(transport=handler, follow_redirects=True),
        clock=clock.time if clock is not None else None,
        sleep=clock.sleep if clock is not None else None,
    )


def _handler(pdf: bytes, *, product_html: str = PRODUCT_HTML) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /wp-content/uploads/\n")
        if request.url.path == PRODUCT_PATH:
            return httpx.Response(200, text=product_html)
        if request.url.path in {PDF_2026, PDF_2025}:
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_collector_is_registered() -> None:
    assert [collector.source_id for collector in COLLECTORS] == ["globalx_ca_qpd"]


def test_quarterly_links_prefers_newest_english_summary() -> None:
    scored = quarterly_links(PRODUCT_HTML)
    assert sorted(scored) == [
        ((2025, 3), PDF_2025),
        ((2026, 1), PDF_2026),
    ]
    collector = GlobalXQpdCollector(source=_source())
    assert collector.disclosure_url(PRODUCT_HTML, TICKER) == ("https://www.globalx.ca" + PDF_2026)


def test_fetch_reads_product_page_and_stores_latest_pdf(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    pdf = write_globalx_lookalike(tmp_path / "globalx.pdf")
    content = pdf.read_bytes()
    collector = GlobalXQpdCollector(source=_source())
    store = RawStore(tmp_path / "raw")
    client = _client(_handler(content), fake_clock)
    try:
        raw_files = collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, store)
    finally:
        client.close()
    assert len(raw_files) == 1
    raw = raw_files[0]
    assert raw.url == "https://www.globalx.ca" + PDF_2026
    assert raw.path.read_bytes() == content
    assert raw.source_id == "globalx_ca_qpd"
    assert 30.0 in fake_clock.sleeps


def test_gate_refusal_does_not_touch_the_network(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nAllow: /")

    collector = GlobalXQpdCollector(
        source=qpd_source("globalx_ca_qpd", decision=Decision.not_allowed)
    )
    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(TermsNotAllowedError, match="skipped: terms"):
            collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()
    assert calls == []


def test_missing_summary_link_is_a_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    collector = GlobalXQpdCollector(source=_source())
    client = _client(
        _handler(b"%PDF-1.4", product_html="<html><body>no links</body></html>"),
        fake_clock,
    )
    try:
        with pytest.raises(CollectError, match="no quarterly Summary"):
            collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()


def test_missing_product_page_is_a_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(404)

    collector = GlobalXQpdCollector(source=_source())
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        with pytest.raises(CollectError, match="HTTP 404"):
            collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()


def test_non_pdf_response_is_a_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    collector = GlobalXQpdCollector(source=_source())
    client = _client(_handler(b"<html>challenge</html>"), fake_clock)
    try:
        with pytest.raises(CollectError, match="not a PDF"):
            collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()


def test_not_modified_returns_no_new_file(tmp_path: Path, fake_clock: FakeClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.path == PRODUCT_PATH:
            return httpx.Response(200, text=PRODUCT_HTML)
        return httpx.Response(304)

    collector = GlobalXQpdCollector(source=_source())
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        assert collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, RawStore(tmp_path)) == []
    finally:
        client.close()


def test_source_is_a_class_attribute_bound_by_copy(tmp_path: Path) -> None:
    assert GlobalXQpdCollector.source is None
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nAllow: /")

    collector = copy.copy(GlobalXQpdCollector())
    collector.source = qpd_source(
        "globalx_ca_qpd", issuer="globalx_ca", decision=Decision.not_allowed
    )
    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(TermsNotAllowedError, match="skipped: terms"):
            collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()
    assert calls == []


def test_pdf_request_is_conditional(tmp_path: Path, fake_clock: FakeClock) -> None:
    pdf = write_globalx_lookalike(tmp_path / "globalx.pdf").read_bytes()
    state = {"pdf_calls": 0}
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.path == PRODUCT_PATH:
            return httpx.Response(200, text=PRODUCT_HTML)
        state["pdf_calls"] += 1
        if state["pdf_calls"] == 1:
            return httpx.Response(
                200,
                content=pdf,
                headers={"content-type": "application/pdf", "etag": '"hxt-q1"'},
            )
        seen["if-none-match"] = request.headers.get("If-None-Match")
        return httpx.Response(304)

    collector = GlobalXQpdCollector(source=_source())
    store = RawStore(tmp_path / "raw")
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        first = collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, store)
        second = collector.fetch(qpd_fund(TICKER), date(2026, 7, 2), client, store)
    finally:
        client.close()
    assert len(first) == 1
    assert second == []
    assert seen["if-none-match"] == '"hxt-q1"'
