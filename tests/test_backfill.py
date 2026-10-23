"""Backfill tests for quarterly disclosure history (FR-2.2)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from etfh.backfill import BackfillError, BackfillResult, backfill_qpd, quarter_ends
from etfh.http import PoliteClient
from etfh.storage import RawStore
from etfh.types import Decision, FileFormat, SourceKind
from tests._qpd_helpers import (
    seed_qpd_raw,
    write_backfill_config,
    write_evolve_lookalike,
    write_globalx_lookalike,
    write_purpose_lookalike,
    write_vanguard_lookalike,
)
from tests.conftest import FakeClock

GLOBALX_PAGE_PATH = "/product/GXF"
GLOBALX_2025_Q1 = "/wp-content/uploads/2025/05/GXF-SOI-Summary-2025_Q1.pdf"
GLOBALX_2025_Q3 = "/wp-content/uploads/2025/12/GXF-SOI-Summary-2025_Q3.pdf"
GLOBALX_2026_Q1 = "/wp-content/uploads/2026/05/GXF-SOI-Summary-2026_Q1.pdf"
GLOBALX_HTML = f"""
<html><body>
<a href="{GLOBALX_2025_Q1}">2025 Q1 Summary</a>
<a href="{GLOBALX_2025_Q3}">2025 Q3 Summary</a>
<a href="{GLOBALX_2026_Q1}">2026 Q1 Summary</a>
<a href="/wp-content/uploads/2026/08/GXF-Proxy-2026.pdf">2026 Proxy Vote Summary</a>
</body></html>
"""


def _client(handler: httpx.MockTransport, clock: FakeClock | None = None) -> PoliteClient:
    return PoliteClient(
        "etfh-tests (test@example.com)",
        client=httpx.Client(transport=handler, follow_redirects=True),
        clock=clock.time if clock is not None else None,
        sleep=clock.sleep if clock is not None else None,
    )


def _globalx_config(tmp_path: Path, tickers: list[str] | None = None) -> Path:
    return write_backfill_config(
        tmp_path / "config",
        source_id="globalx_ca_qpd",
        issuer="globalx_ca",
        tickers=tickers or ["GXF"],
        min_interval_seconds=30,
    )


def _globalx_transport(
    tmp_path: Path,
    *,
    page: str = GLOBALX_HTML,
    calls: list[str] | None = None,
) -> httpx.MockTransport:
    pdfs = {
        GLOBALX_2025_Q1: write_globalx_lookalike(
            tmp_path / "gx-2025-q1.pdf", as_of="March 31, 2025"
        ).read_bytes(),
        GLOBALX_2025_Q3: write_globalx_lookalike(
            tmp_path / "gx-2025-q3.pdf", as_of="September 30, 2025"
        ).read_bytes(),
        GLOBALX_2026_Q1: write_globalx_lookalike(
            tmp_path / "gx-2026-q1.pdf", as_of="March 31, 2026"
        ).read_bytes(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.path == GLOBALX_PAGE_PATH:
            return httpx.Response(200, text=page)
        if request.url.path in pdfs:
            return httpx.Response(
                200, content=pdfs[request.url.path], headers={"content-type": "application/pdf"}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _by_quarter(results: list[BackfillResult]) -> dict[date, BackfillResult]:
    return {result.quarter_end: result for result in results}


def test_quarter_ends_are_the_last_quarters_before_today() -> None:
    assert quarter_ends(8, date(2026, 10, 23)) == [
        date(2024, 12, 31),
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
        date(2025, 12, 31),
        date(2026, 3, 31),
        date(2026, 6, 30),
        date(2026, 9, 30),
    ]


def test_quarter_ends_are_strictly_before_today() -> None:
    assert quarter_ends(1, date(2026, 9, 30)) == [date(2026, 6, 30)]
    assert quarter_ends(1, date(2026, 10, 1)) == [date(2026, 9, 30)]


def test_quarter_ends_cross_year_boundaries_and_leap_days() -> None:
    assert quarter_ends(5, date(2026, 2, 15)) == [
        date(2024, 12, 31),
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
        date(2025, 12, 31),
    ]
    assert quarter_ends(1, date(2024, 2, 29)) == [date(2023, 12, 31)]


def test_quarter_count_must_be_positive() -> None:
    with pytest.raises(BackfillError, match="at least 1"):
        quarter_ends(0, date(2026, 10, 23))


def test_backfill_fetches_only_the_quarters_the_page_lists(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    calls: list[str] = []
    store = RawStore(tmp_path / "raw")
    config = _globalx_config(tmp_path)
    client = _client(_globalx_transport(tmp_path, calls=calls), fake_clock)
    try:
        results = backfill_qpd(
            "globalx_ca",
            8,
            store.root,
            config_dir=config,
            client=client,
            today=date(2026, 10, 23),
        )
    finally:
        client.close()

    assert len(results) == 8
    by_quarter = _by_quarter(results)
    fetched = {end for end, result in by_quarter.items() if result.status == "fetched"}
    missing = {end for end, result in by_quarter.items() if result.status == "missing"}
    assert fetched == {date(2025, 3, 31), date(2025, 9, 30), date(2026, 3, 31)}
    assert missing == {
        date(2024, 12, 31),
        date(2025, 6, 30),
        date(2025, 12, 31),
        date(2026, 6, 30),
        date(2026, 9, 30),
    }
    # One product page per fund, three PDFs, and no guessed URLs.
    assert calls.count(GLOBALX_PAGE_PATH) == 1
    assert len([path for path in calls if path.endswith(".pdf")]) == 3
    for end in fetched:
        raw = by_quarter[end].raw
        assert raw is not None
        assert raw.as_of_hint == end
        assert raw.path.parent.name == end.isoformat()
        assert raw.source_id == "globalx_ca_qpd"
    assert all("not published" in by_quarter[end].detail for end in missing)


def test_rerun_reports_present_without_any_request(tmp_path: Path, fake_clock: FakeClock) -> None:
    store = RawStore(tmp_path / "raw")
    config = _globalx_config(tmp_path)
    client = _client(_globalx_transport(tmp_path), fake_clock)
    try:
        backfill_qpd(
            "globalx_ca",
            8,
            store.root,
            config_dir=config,
            client=client,
            today=date(2026, 10, 23),
        )
    finally:
        client.close()

    calls: list[str] = []
    client = _client(_globalx_transport(tmp_path, calls=calls), fake_clock)
    try:
        results = backfill_qpd(
            "globalx_ca",
            8,
            store.root,
            config_dir=config,
            client=client,
            today=date(2026, 10, 23),
        )
    finally:
        client.close()
    by_quarter = _by_quarter(results)
    assert [
        by_quarter[end].status for end in (date(2025, 3, 31), date(2025, 9, 30), date(2026, 3, 31))
    ] == [
        "present",
        "present",
        "present",
    ]
    assert calls.count(GLOBALX_PAGE_PATH) == 1
    assert not [path for path in calls if path.endswith(".pdf")]


def test_a_stored_pdf_is_recognised_by_its_stated_date(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """Daily collectors store a disclosure without an as-of hint.

    The backfill must read the portfolio date out of the stored PDF rather
    than download the same quarter again.
    """
    store = RawStore(tmp_path / "raw")
    pdf = write_globalx_lookalike(tmp_path / "gx-2026-q1.pdf", as_of="March 31, 2026")
    seed_qpd_raw(
        store,
        pdf,
        source_id="globalx_ca_qpd",
        issuer="globalx_ca",
        ticker="GXF",
        as_of=None,
    )
    config = _globalx_config(tmp_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500)

    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        results = backfill_qpd(
            "globalx_ca",
            1,
            store.root,
            config_dir=config,
            client=client,
            today=date(2026, 4, 15),
        )
    finally:
        client.close()
    assert len(results) == 1
    assert results[0].status == "present"
    assert results[0].raw is not None
    assert calls == []


def test_a_failing_product_page_costs_one_request_per_fund(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(404)

    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        results = backfill_qpd(
            "globalx_ca",
            8,
            tmp_path / "raw",
            config_dir=_globalx_config(tmp_path),
            client=client,
            today=date(2026, 10, 23),
        )
    finally:
        client.close()
    assert [result.status for result in results] == ["missing"] * 8
    assert all("HTTP 404" in result.detail for result in results)
    assert calls.count(GLOBALX_PAGE_PATH) == 1


def test_backfill_evolve_history_from_the_product_page(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    old = (
        "https://evolve-cdn.tor1.cdn.digitaloceanspaces.com/wp-content/uploads/2025/10/"
        "CYBX-EN-QPDSeptember2025.pdf"
    )
    new = (
        "https://evolve-cdn.tor1.cdn.digitaloceanspaces.com/wp-content/uploads/2026/05/"
        "CYBX-EN-QPDMarch2026.pdf"
    )
    french = (
        "https://evolve-cdn.tor1.cdn.digitaloceanspaces.com/wp-content/uploads/2026/05/"
        "CYBX-FR-QPDMarch2026.pdf"
    )
    page = f"""
    <html><body>
    <a href="{old}">Quarterly Portfolio Disclosure</a>
    <a href="{french}">Divulgation trimestrielle du portefeuille</a>
    <a href="{new}">Quarterly Portfolio Disclosure</a>
    </body></html>
    """
    pdfs = {
        old: write_evolve_lookalike(tmp_path / "evolve-old.pdf", as_of="September 30, 2025"),
        new: write_evolve_lookalike(tmp_path / "evolve-new.pdf", as_of="March 31, 2026"),
    }
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.path == "/product/CYBX/":
            return httpx.Response(200, text=page)
        if str(request.url) in pdfs:
            return httpx.Response(
                200,
                content=pdfs[str(request.url)].read_bytes(),
                headers={"content-type": "application/pdf"},
            )
        return httpx.Response(404)

    config = write_backfill_config(
        tmp_path / "config",
        source_id="evolve_qpd",
        issuer="evolve",
        tickers=["CYBX"],
        min_interval_seconds=10,
    )
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        results = backfill_qpd(
            "evolve",
            3,
            tmp_path / "raw",
            config_dir=config,
            client=client,
            today=date(2026, 4, 15),
        )
    finally:
        client.close()

    by_quarter = _by_quarter(results)
    assert by_quarter[date(2025, 9, 30)].status == "fetched"
    assert by_quarter[date(2026, 3, 31)].status == "fetched"
    assert by_quarter[date(2025, 12, 31)].status == "missing"
    assert "not published" in by_quarter[date(2025, 12, 31)].detail
    assert calls.count("/product/CYBX/") == 1
    assert [path for path in calls if path.endswith(".pdf")] == [
        "/wp-content/uploads/2025/10/CYBX-EN-QPDSeptember2025.pdf",
        "/wp-content/uploads/2026/05/CYBX-EN-QPDMarch2026.pdf",
    ]
    assert 10.0 in fake_clock.sleeps


def test_unknown_issuer_and_issuer_without_a_disclosure_source(tmp_path: Path) -> None:
    config = _globalx_config(tmp_path)
    with pytest.raises(BackfillError, match="unknown issuer"):
        backfill_qpd("nowhere", 1, tmp_path / "raw", config_dir=config)
    daily = write_backfill_config(
        tmp_path / "daily-config",
        source_id="dailyco_daily",
        issuer="dailyco",
        tickers=["DAY"],
        kind=SourceKind.daily_holdings,
        file_format=FileFormat.csv,
    )
    with pytest.raises(BackfillError, match="no quarterly disclosure source"):
        backfill_qpd("dailyco", 1, tmp_path / "raw", config_dir=daily)


def test_issuer_without_an_approved_disclosure_source(tmp_path: Path) -> None:
    config = write_backfill_config(
        tmp_path / "config",
        source_id="globalx_ca_qpd",
        issuer="globalx_ca",
        tickers=["GXF"],
        decision=Decision.not_allowed,
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500)

    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(BackfillError, match="no approved quarterly disclosure source"):
            backfill_qpd("globalx_ca", 1, tmp_path / "raw", config_dir=config, client=client)
    finally:
        client.close()
    assert calls == []


def test_fund_filter_and_unknown_ticker(tmp_path: Path, fake_clock: FakeClock) -> None:
    config = _globalx_config(tmp_path, ["GXF", "GXH"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.path.endswith(".pdf"):
            return httpx.Response(404)
        return httpx.Response(200, text="<html><body>nothing</body></html>")

    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        results = backfill_qpd(
            "globalx_ca",
            1,
            tmp_path / "raw",
            config_dir=config,
            client=client,
            today=date(2026, 4, 15),
            funds=["GXF"],
        )
        assert [result.fund_ticker for result in results] == ["GXF"]
        with pytest.raises(BackfillError, match="not configured"):
            backfill_qpd(
                "globalx_ca",
                1,
                tmp_path / "raw",
                config_dir=config,
                client=client,
                today=date(2026, 4, 15),
                funds=["NOPE"],
            )
    finally:
        client.close()


def test_backfill_purpose_reads_the_fund_document_index(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    old = (
        "https://documents.purposeinvest.com/Docs/PDF/quarterly_portfolio_disclosure/en/"
        "Purpose%20Fictional%20PDF%20Fund%20Quarterly%20Portfolio%20Disclosure%202025-09-30.pdf"
    )
    new = (
        "https://documents.purposeinvest.com/Docs/PDF/quarterly_portfolio_disclosure/en/"
        "Purpose%20Fictional%20PDF%20Fund%20Quarterly%20Portfolio%20Disclosure%202026-03-31.pdf"
    )
    payload = {
        "documents": {
            "fund": [
                {"quarterly_portfolio_disclosure": {"dt": "2025-09-30", "url": old}},
                {"quarterly_portfolio_disclosure": {"dt": "2026-03-31", "url": new}},
            ],
            "series": [],
        }
    }
    pdfs = {
        old: write_purpose_lookalike(tmp_path / "purpose-old.pdf", as_of="September 30, 2025"),
        new: write_purpose_lookalike(tmp_path / "purpose-new.pdf", as_of="March 31, 2026"),
    }
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.host == "www.purposeinvest.com":
            return httpx.Response(200, json=payload)
        if str(request.url) in pdfs:
            return httpx.Response(
                200,
                content=pdfs[str(request.url)].read_bytes(),
                headers={"content-type": "application/pdf"},
            )
        return httpx.Response(404)

    config = write_backfill_config(
        tmp_path / "config",
        source_id="purpose_qpd",
        issuer="purpose",
        tickers=["PDF"],
        params="purpose_slug=purpose-fictional-pdf-fund",
    )
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        results = backfill_qpd(
            "purpose",
            3,
            tmp_path / "raw",
            config_dir=config,
            client=client,
            today=date(2026, 4, 15),
        )
    finally:
        client.close()

    by_quarter = _by_quarter(results)
    assert by_quarter[date(2025, 9, 30)].status == "fetched"
    assert by_quarter[date(2026, 3, 31)].status == "fetched"
    assert by_quarter[date(2025, 12, 31)].status == "missing"
    assert "not published" in by_quarter[date(2025, 12, 31)].detail
    assert calls.count("/api/funds/purpose-fictional-pdf-fund") == 1
    assert len([path for path in calls if path.endswith(".pdf")]) == 2


def test_backfill_vanguard_reports_present_and_missing(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    store = RawStore(tmp_path / "raw")
    pdf = write_vanguard_lookalike(tmp_path / "vanguard.pdf", as_of="June 30, 2026")
    seed_qpd_raw(
        store,
        pdf,
        source_id="vanguard_ca_qpd",
        issuer="vanguard_ca",
        ticker="VFV",
        as_of=None,
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500)

    config = write_backfill_config(
        tmp_path / "config",
        source_id="vanguard_ca_qpd",
        issuer="vanguard_ca",
        tickers=["VFV"],
    )
    client = _client(httpx.MockTransport(handler), fake_clock)
    try:
        results = backfill_qpd(
            "vanguard_ca",
            3,
            store.root,
            config_dir=config,
            client=client,
            today=date(2026, 10, 23),
        )
    finally:
        client.close()
    by_quarter = _by_quarter(results)
    assert by_quarter[date(2026, 6, 30)].status == "present"
    for end in (date(2026, 3, 31), date(2026, 9, 30)):
        assert by_quarter[end].status == "missing"
        assert "not published: Vanguard" in by_quarter[end].detail
    assert calls == []
