"""Collector tests for Vanguard Canada quarterly disclosures."""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path

import httpx
import pytest

from etfh.collect.registry import CollectError
from etfh.collect.vanguard_qpd import COLLECTORS, VanguardQpdCollector
from etfh.http import PoliteClient
from etfh.parse.qpd_pdf import QpdPdfParser
from etfh.registry import TermsNotAllowedError
from etfh.storage import RawStore
from etfh.types import Decision
from tests._qpd_helpers import (
    qpd_fund,
    qpd_source,
    raw_file_for_pdf,
    write_vanguard_lookalike,
)

TICKER = "NORTH"
PDF_PATH = "/qpd-NORTH-en.pdf"


def _vanguard_lookalike(tmp_path: Path) -> Path:
    return write_vanguard_lookalike(tmp_path / "vanguard.pdf")


def _client(handler: httpx.MockTransport) -> PoliteClient:
    return PoliteClient(
        "etfh-tests (test@example.com)",
        client=httpx.Client(transport=handler, follow_redirects=True),
    )


def test_collector_is_registered() -> None:
    assert [collector.source_id for collector in COLLECTORS] == ["vanguard_ca_qpd"]


def test_disclosure_url_is_built_from_ticker() -> None:
    collector = VanguardQpdCollector(source=qpd_source())
    assert collector.disclosure_url(" veqt ") == "https://fund-docs.vanguard.com/qpd-VEQT-en.pdf"


def test_fetch_stores_pdf_and_parses_stated_total(tmp_path: Path) -> None:
    pdf = _vanguard_lookalike(tmp_path)
    content = pdf.read_bytes()
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == PDF_PATH:
            return httpx.Response(200, content=content, headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    collector = VanguardQpdCollector(source=qpd_source())
    store = RawStore(tmp_path / "raw")
    client = _client(httpx.MockTransport(handler))
    try:
        raw_files = collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, store)
    finally:
        client.close()

    assert len(raw_files) == 1
    raw = raw_files[0]
    assert raw.source_id == "vanguard_ca_qpd"
    assert raw.fund_ticker == TICKER
    assert raw.ext == "pdf"
    assert raw.path.read_bytes() == content
    assert requests[-1] == PDF_PATH

    parsed = QpdPdfParser().parse(
        raw_file_for_pdf(raw.path, source_id=raw.source_id, fund_ticker=TICKER)
    )
    assert "stated_total_fraction=0.5" in parsed.notes
    assert round(float(parsed.frame["weight"].sum()), 4) == 0.50
    assert parsed.as_of_date.isoformat() == "2026-06-30"
    assert parsed.published_at is None
    names = parsed.frame["security_name"].to_list()
    assert "Northwind Materials Inc." in names
    assert "Financials" not in names


def test_gate_refusal_does_not_touch_the_network(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")

    collector = VanguardQpdCollector(source=qpd_source(decision=Decision.not_allowed))
    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(TermsNotAllowedError, match="skipped: terms"):
            collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()
    assert calls == []


def test_robots_403_allows_the_download(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(403, text="<Error><Code>AccessDenied</Code></Error>")
        return httpx.Response(
            200, content=b"%PDF-1.4\n", headers={"content-type": "application/pdf"}
        )

    collector = VanguardQpdCollector(source=qpd_source())
    client = _client(httpx.MockTransport(handler))
    try:
        raw_files = collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()
    assert len(raw_files) == 1
    assert raw_files[0].fund_ticker == TICKER
    assert raw_files[0].path.read_bytes() == b"%PDF-1.4\n"


def test_missing_pdf_is_a_collect_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(404)

    collector = VanguardQpdCollector(source=qpd_source())
    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(CollectError, match="no disclosure PDF"):
            collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()


def test_non_pdf_response_is_a_collect_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, content=b"<html>not a pdf</html>")

    collector = VanguardQpdCollector(source=qpd_source())
    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(CollectError, match="not a PDF"):
            collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()


def test_not_modified_returns_no_new_file(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(304)

    collector = VanguardQpdCollector(source=qpd_source())
    client = _client(httpx.MockTransport(handler))
    try:
        assert collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, RawStore(tmp_path)) == []
    finally:
        client.close()


def test_source_is_a_class_attribute_bound_by_copy(tmp_path: Path) -> None:
    assert VanguardQpdCollector.source is None
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")

    collector = copy.copy(VanguardQpdCollector())
    collector.source = qpd_source(decision=Decision.not_allowed)
    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(TermsNotAllowedError, match="skipped: terms"):
            collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()
    assert calls == []


def test_conditional_headers_come_from_the_previous_sidecar(tmp_path: Path) -> None:
    pdf = write_vanguard_lookalike(tmp_path / "vanguard.pdf").read_bytes()
    state = {"calls": 0}
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(403, text="Access Denied")
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(
                200,
                content=pdf,
                headers={"content-type": "application/pdf", "etag": '"v1"'},
            )
        seen["if-none-match"] = request.headers.get("If-None-Match")
        return httpx.Response(304)

    collector = VanguardQpdCollector(source=qpd_source())
    store = RawStore(tmp_path / "raw")
    client = _client(httpx.MockTransport(handler))
    try:
        first = collector.fetch(qpd_fund(TICKER), date(2026, 7, 1), client, store)
        second = collector.fetch(qpd_fund(TICKER), date(2026, 7, 2), client, store)
    finally:
        client.close()
    assert len(first) == 1
    assert second == []
    assert seen["if-none-match"] == '"v1"'
