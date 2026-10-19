"""Collector tests for Purpose and Evolve quarterly disclosures."""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path

import httpx
import pytest

from etfh.collect.purpose_evolve_qpd import (
    COLLECTORS,
    EvolveQpdCollector,
    PurposeQpdCollector,
    evolve_disclosure_url,
    purpose_disclosure_url,
)
from etfh.collect.registry import CollectError
from etfh.http import PoliteClient
from etfh.registry import TermsNotAllowedError
from etfh.storage import RawStore
from etfh.types import Decision
from tests._qpd_helpers import (
    qpd_fund,
    qpd_source,
    write_evolve_lookalike,
    write_purpose_lookalike,
)
from tests.conftest import FakeClock

PURPOSE_SLUG = "purpose-fictional-dividend-fund"
PURPOSE_PDF = (
    "https://documents.purposeinvest.com/Docs/PDF/quarterly_portfolio_disclosure/en/"
    "Purpose%20Fictional%20Dividend%20Fund%20Quarterly%20Portfolio%20Disclosure%202026-06-30.pdf"
)
EVOLVE_PRODUCT_PATH = "/product/CYBX/"
EVOLVE_OLD = (
    "https://evolve-cdn.tor1.cdn.digitaloceanspaces.com/wp-content/uploads/2025/10/"
    "CYBX-EN-QPDSeptember2025.pdf"
)
EVOLVE_FRENCH = (
    "https://evolve-cdn.tor1.cdn.digitaloceanspaces.com/wp-content/uploads/2026/05/"
    "CYBX-FR-QPDMarch2026.pdf"
)
EVOLVE_NEW = (
    "https://evolve-cdn.tor1.cdn.digitaloceanspaces.com/wp-content/uploads/2026/05/"
    "CYBX-EN-QPDMarch2026.pdf"
)

EVOLVE_HTML = f"""
<html><body>
<a href="{EVOLVE_OLD}">Quarterly Portfolio Disclosure</a>
<a href="{EVOLVE_FRENCH}">Divulgation trimestrielle du portefeuille</a>
<a href="{EVOLVE_NEW}">Quarterly Portfolio Disclosure</a>
<a href="https://evolve-cdn.tor1.cdn.digitaloceanspaces.com/wp-content/uploads/x/
CYBX_FS.pdf">Financial Statements</a>
</body></html>
"""


def _purpose_payload() -> dict[str, object]:
    return {
        "documents": {
            "fund": {
                "quarterly_portfolio_disclosure": {
                    "id": 1,
                    "name": "Quarterly Portfolio Disclosure",
                    "url": PURPOSE_PDF,
                    "dt": "2026-06-30",
                }
            },
            "series": [],
        }
    }


def _purpose_source() -> object:
    return qpd_source("purpose_qpd", issuer="purpose")


def _evolve_source() -> object:
    return qpd_source("evolve_qpd", issuer="evolve", min_interval_seconds=10)


def _client(handler: httpx.MockTransport, clock: FakeClock | None = None) -> PoliteClient:
    return PoliteClient(
        "etfh-tests (test@example.com)",
        client=httpx.Client(transport=handler, follow_redirects=True),
        clock=clock.time if clock is not None else None,
        sleep=clock.sleep if clock is not None else None,
    )


def _purpose_fund() -> object:
    return qpd_fund("PDIV", issuer="purpose", source_id="purpose_qpd").model_copy(
        update={"params": {"purpose_slug": PURPOSE_SLUG}}
    )


def test_collectors_are_registered() -> None:
    assert [collector.source_id for collector in COLLECTORS] == ["purpose_qpd", "evolve_qpd"]


def test_purpose_disclosure_url_reads_nested_documents() -> None:
    assert purpose_disclosure_url(_purpose_payload()) == PURPOSE_PDF
    assert purpose_disclosure_url({"documents": {"fund": {}}}) is None
    assert purpose_disclosure_url([]) is None


def test_evolve_disclosure_url_picks_newest_english() -> None:
    assert evolve_disclosure_url(EVOLVE_HTML) == EVOLVE_NEW
    assert evolve_disclosure_url("<html></html>") is None


def test_purpose_fetch_stores_pdf(tmp_path: Path, fake_clock: FakeClock) -> None:
    pdf = write_purpose_lookalike(tmp_path / "purpose.pdf").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.host == "www.purposeinvest.com":
            return httpx.Response(200, json=_purpose_payload())
        if request.url.host == "documents.purposeinvest.com":
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    collector = PurposeQpdCollector(source=_purpose_source())
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        raw_files = collector.fetch(
            _purpose_fund(), date(2026, 7, 1), client, RawStore(tmp_path / "raw")
        )
    finally:
        client.close()
    assert len(raw_files) == 1
    assert raw_files[0].url == PURPOSE_PDF
    assert raw_files[0].path.read_bytes() == pdf
    assert raw_files[0].source_id == "purpose_qpd"


def test_purpose_missing_slug_is_a_collect_error(tmp_path: Path) -> None:
    collector = PurposeQpdCollector(source=_purpose_source())
    client = _client(httpx.MockTransport(lambda request: httpx.Response(500)))
    try:
        with pytest.raises(CollectError, match="purpose_slug"):
            collector.fetch(
                qpd_fund("PDIV", issuer="purpose", source_id="purpose_qpd"),
                date(2026, 7, 1),
                client,
                RawStore(tmp_path),
            )
    finally:
        client.close()


def test_purpose_pdf_request_is_conditional(tmp_path: Path, fake_clock: FakeClock) -> None:
    pdf = write_purpose_lookalike(tmp_path / "purpose.pdf").read_bytes()
    state = {"pdf_calls": 0}
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "www.purposeinvest.com":
            return httpx.Response(200, json=_purpose_payload())
        state["pdf_calls"] += 1
        if state["pdf_calls"] == 1:
            return httpx.Response(
                200,
                content=pdf,
                headers={
                    "content-type": "application/pdf",
                    "last-modified": "Wed, 01 Jul 2026 00:00:00 GMT",
                },
            )
        seen["if-modified-since"] = request.headers.get("If-Modified-Since")
        return httpx.Response(304)

    collector = PurposeQpdCollector(source=_purpose_source())
    store = RawStore(tmp_path / "raw")
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        first = collector.fetch(_purpose_fund(), date(2026, 7, 1), client, store)
        second = collector.fetch(_purpose_fund(), date(2026, 7, 2), client, store)
    finally:
        client.close()
    assert len(first) == 1
    assert second == []
    assert seen["if-modified-since"] == "Wed, 01 Jul 2026 00:00:00 GMT"


def test_evolve_source_is_a_class_attribute_bound_by_copy(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    assert EvolveQpdCollector.source is None
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nAllow: /")

    collector = copy.copy(EvolveQpdCollector())
    collector.source = qpd_source("evolve_qpd", issuer="evolve", decision=Decision.not_allowed)
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        with pytest.raises(TermsNotAllowedError, match="skipped: terms"):
            collector.fetch(
                qpd_fund("CYBX", issuer="evolve", source_id="evolve_qpd"),
                date(2026, 7, 1),
                client,
                RawStore(tmp_path),
            )
    finally:
        client.close()
    assert calls == []


def test_purpose_json_without_disclosure_is_a_collect_error(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, json={"documents": {"fund": {}}})

    collector = PurposeQpdCollector(source=_purpose_source())
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        with pytest.raises(CollectError, match="no quarterly portfolio disclosure"):
            collector.fetch(_purpose_fund(), date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()


def test_purpose_invalid_json_is_a_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text="<html>not json</html>")

    collector = PurposeQpdCollector(source=_purpose_source())
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        with pytest.raises(CollectError, match="invalid JSON"):
            collector.fetch(_purpose_fund(), date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()


def test_purpose_robots_403_allows_pdf_download(tmp_path: Path, fake_clock: FakeClock) -> None:
    pdf = write_purpose_lookalike(tmp_path / "purpose.pdf").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            if request.url.host == "documents.purposeinvest.com":
                return httpx.Response(403, text="Access Denied")
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.host == "www.purposeinvest.com":
            return httpx.Response(200, json=_purpose_payload())
        return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})

    collector = PurposeQpdCollector(source=_purpose_source())
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        raw_files = collector.fetch(
            _purpose_fund(), date(2026, 7, 1), client, RawStore(tmp_path / "raw")
        )
    finally:
        client.close()
    assert len(raw_files) == 1
    assert raw_files[0].url == PURPOSE_PDF


def test_evolve_fetch_stores_newest_pdf(tmp_path: Path, fake_clock: FakeClock) -> None:
    pdf = write_evolve_lookalike(tmp_path / "evolve.pdf").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.host == "evolveetfs.com":
            return httpx.Response(200, text=EVOLVE_HTML)
        if request.url.host == "evolve-cdn.tor1.cdn.digitaloceanspaces.com":
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    collector = EvolveQpdCollector(source=_evolve_source())
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        raw_files = collector.fetch(
            qpd_fund("CYBX", issuer="evolve", source_id="evolve_qpd"),
            date(2026, 7, 1),
            client,
            RawStore(tmp_path / "raw"),
        )
    finally:
        client.close()
    assert len(raw_files) == 1
    assert raw_files[0].url == EVOLVE_NEW
    assert raw_files[0].path.read_bytes() == pdf
    assert fake_clock.sleeps == [1.0, 1.0]


def test_evolve_missing_link_is_a_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text="<html><body>no documents</body></html>")

    collector = EvolveQpdCollector(source=_evolve_source())
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        with pytest.raises(CollectError, match="no quarterly portfolio disclosure"):
            collector.fetch(
                qpd_fund("CYBX", issuer="evolve", source_id="evolve_qpd"),
                date(2026, 7, 1),
                client,
                RawStore(tmp_path),
            )
    finally:
        client.close()


def test_evolve_robots_403_allows_pdf_download(tmp_path: Path, fake_clock: FakeClock) -> None:
    pdf = write_evolve_lookalike(tmp_path / "evolve.pdf").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            if request.url.host == "evolve-cdn.tor1.cdn.digitaloceanspaces.com":
                return httpx.Response(403, text="Access Denied")
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.host == "evolveetfs.com":
            return httpx.Response(200, text=EVOLVE_HTML)
        return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})

    collector = EvolveQpdCollector(source=_evolve_source())
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        raw_files = collector.fetch(
            qpd_fund("CYBX", issuer="evolve", source_id="evolve_qpd"),
            date(2026, 7, 1),
            client,
            RawStore(tmp_path / "raw"),
        )
    finally:
        client.close()
    assert len(raw_files) == 1
    assert raw_files[0].url == EVOLVE_NEW


def test_evolve_non_pdf_is_a_collect_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.host == "evolveetfs.com":
            return httpx.Response(200, text=EVOLVE_HTML)
        return httpx.Response(200, content=b"<html>challenge</html>")

    collector = EvolveQpdCollector(source=_evolve_source())
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        with pytest.raises(CollectError, match="not a PDF"):
            collector.fetch(
                qpd_fund("CYBX", issuer="evolve", source_id="evolve_qpd"),
                date(2026, 7, 1),
                client,
                RawStore(tmp_path),
            )
    finally:
        client.close()


def test_gate_refusal_does_not_touch_the_network(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nAllow: /")

    client = _client(httpx.MockTransport(handler))
    try:
        for collector, fund in (
            (
                PurposeQpdCollector(
                    source=qpd_source("purpose_qpd", decision=Decision.not_allowed)
                ),
                qpd_fund("PDIV", issuer="purpose", source_id="purpose_qpd"),
            ),
            (
                EvolveQpdCollector(source=qpd_source("evolve_qpd", decision=Decision.not_allowed)),
                qpd_fund("CYBX", issuer="evolve", source_id="evolve_qpd"),
            ),
        ):
            with pytest.raises(TermsNotAllowedError, match="skipped: terms"):
                collector.fetch(fund, date(2026, 7, 1), client, RawStore(tmp_path))
    finally:
        client.close()
    assert calls == []
