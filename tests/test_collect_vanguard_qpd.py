"""Collector tests for Vanguard Canada quarterly disclosures."""

from __future__ import annotations

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
    write_qpd_pdf,
)

TICKER = "NORTH"
PDF_PATH = "/qpd-NORTH-en.pdf"


def _vanguard_lookalike(tmp_path: Path) -> Path:
    return write_qpd_pdf(
        tmp_path / "vanguard.pdf",
        as_of="June 30, 2026",
        fund_name="Northline Balanced ETF",
        published=None,
        header="Security % of Net Assets",
        rows=[
            "Canadian Equity",
            ("Maplecrest Properties", "18.40"),
            ("Northgate Bank", "12.10"),
            "U.S. Equity",
            ("Uplands Chemicals Inc.", "24.50"),
            ("Pineview Software Corp.", "19.90"),
            "Fixed Income",
            ("Fairhaven Bond Fund", "20.00"),
            ("Cash and Cash Equivalents", "4.00"),
        ],
        total="Total Investments 98.90%",
    )


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
    assert "stated_total_fraction=0.989" in parsed.notes
    assert round(float(parsed.frame["weight"].sum()), 4) == 0.989
    assert parsed.as_of_date.isoformat() == "2026-06-30"
    assert parsed.published_at is None
    by_name = dict(
        zip(
            parsed.frame["security_name"].to_list(),
            parsed.frame["asset_class"].to_list(),
            strict=True,
        )
    )
    assert by_name["Cash and Cash Equivalents"] == "cash"


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
