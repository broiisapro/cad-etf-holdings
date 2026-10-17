"""Tests for the polite HTTP client. All offline, with a fake clock and transport."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from etfh.http import HTTPRetryError, PoliteClient, RobotsDisallowedError
from etfh.registry import Source, TermsNotAllowedError
from etfh.types import Decision
from tests.conftest import FakeClock

USER_AGENT = "cad-etf-holdings-tests (https://example.com)"


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    fake_clock: FakeClock,
    **kwargs: object,
) -> PoliteClient:
    return PoliteClient(
        user_agent=str(kwargs.pop("user_agent", USER_AGENT)),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=fake_clock.time,
        sleep=fake_clock.sleep,
        **kwargs,
    )


def _handler(
    body_by_path: dict[str, httpx.Response],
    requests: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        response = body_by_path.get(request.url.path)
        if response is None:
            return httpx.Response(404, text="missing")
        return response

    return handler


def test_limiter_spacing_per_host(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    source = source_factory()
    client = _client(_handler({"/holdings.csv": httpx.Response(200, text="data")}), fake_clock)
    client.get("https://a.example.com/holdings.csv", source=source)
    client.get("https://b.example.com/holdings.csv", source=source)
    assert fake_clock.sleeps == [1.0, 1.0]
    assert fake_clock.now == 2.0


def test_same_host_waits_and_fetches_robots_once(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    requests: list[httpx.Request] = []
    source = source_factory()
    client = _client(
        _handler(
            {"/robots.txt": httpx.Response(404), "/holdings.csv": httpx.Response(200, text="data")},
            requests,
        ),
        fake_clock,
    )
    client.get("https://a.example.com/holdings.csv", source=source)
    client.get("https://a.example.com/holdings.csv", source=source)
    assert fake_clock.sleeps == [1.0, 1.0]
    assert [request.url.path for request in requests].count("/robots.txt") == 1


def test_crawl_delay_raises_interval(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    robots = httpx.Response(200, text="User-agent: *\nCrawl-delay: 5\nDisallow: /nothing\n")
    source = source_factory()
    client = _client(
        _handler({"/robots.txt": robots, "/holdings.csv": httpx.Response(200, text="data")}),
        fake_clock,
    )
    client.get("https://a.example.com/holdings.csv", source=source)
    client.get("https://a.example.com/holdings.csv", source=source)
    assert fake_clock.sleeps == [1.0, 5.0]


def test_source_min_interval_raises_interval(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    source = source_factory(min_interval_seconds=10)
    client = _client(_handler({"/holdings.csv": httpx.Response(200, text="data")}), fake_clock)
    client.get("https://a.example.com/holdings.csv", source=source)
    client.get("https://a.example.com/holdings.csv", source=source)
    assert fake_clock.sleeps == [1.0, 10.0]


def test_robots_disallow_refused_without_fetching_url(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    requests: list[httpx.Request] = []
    source = source_factory()
    client = _client(
        _handler(
            {
                "/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /private\n"),
                "/private/holdings.csv": httpx.Response(200, text="data"),
            },
            requests,
        ),
        fake_clock,
    )
    with pytest.raises(RobotsDisallowedError):
        client.get("https://a.example.com/private/holdings.csv", source=source)
    assert [request.url.path for request in requests] == ["/robots.txt"]


def test_robots_5xx_disallows_all(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    requests: list[httpx.Request] = []
    source = source_factory()
    client = _client(
        _handler(
            {
                "/robots.txt": httpx.Response(503),
                "/holdings.csv": httpx.Response(200, text="data"),
            },
            requests,
        ),
        fake_clock,
    )
    with pytest.raises(RobotsDisallowedError):
        client.get("https://a.example.com/holdings.csv", source=source)
    assert [request.url.path for request in requests] == ["/robots.txt"]


def test_robots_404_allows_all(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    source = source_factory()
    client = _client(_handler({"/holdings.csv": httpx.Response(200, text="data")}), fake_clock)
    response = client.get("https://a.example.com/holdings.csv", source=source)
    assert response.status_code == 200


def test_retry_on_429_then_success(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    state = {"data_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        state["data_calls"] += 1
        if state["data_calls"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, text="data")

    source = source_factory()
    client = _client(handler, fake_clock)
    response = client.get("https://a.example.com/holdings.csv", source=source)
    assert response.status_code == 200
    assert state["data_calls"] == 2
    assert 2.0 in fake_clock.sleeps


def test_retry_on_timeout_then_success(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    state = {"data_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        state["data_calls"] += 1
        if state["data_calls"] == 1:
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(200, text="data")

    source = source_factory()
    client = _client(handler, fake_clock)
    response = client.get("https://a.example.com/holdings.csv", source=source)
    assert response.status_code == 200
    assert state["data_calls"] == 2


def test_gives_up_after_four_attempts(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    state = {"data_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        state["data_calls"] += 1
        return httpx.Response(500)

    source = source_factory()
    client = _client(handler, fake_clock)
    with pytest.raises(HTTPRetryError) as excinfo:
        client.get("https://a.example.com/holdings.csv", source=source)
    assert excinfo.value.status_code == 500
    assert state["data_calls"] == 4


def test_missing_user_agent_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ETFH_USER_AGENT", raising=False)
    with pytest.raises(ValueError, match="ETFH_USER_AGENT"):
        PoliteClient()
    with pytest.raises(ValueError, match="contact e-mail or URL"):
        PoliteClient(user_agent="etfh/0.1")


def test_user_agent_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETFH_USER_AGENT", "cad-etf-holdings (mailto:dev@example.com)")
    client = PoliteClient()
    client.close()


def test_conditional_headers_sent(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen["if-none-match"] = request.headers.get("If-None-Match")
        seen["if-modified-since"] = request.headers.get("If-Modified-Since")
        return httpx.Response(200, text="data")

    source = source_factory()
    client = _client(handler, fake_clock)
    client.get(
        "https://a.example.com/holdings.csv",
        source=source,
        etag='"abc"',
        last_modified="Mon, 01 Jan 2026 00:00:00 GMT",
    )
    assert seen["if-none-match"] == '"abc"'
    assert seen["if-modified-since"] == "Mon, 01 Jan 2026 00:00:00 GMT"


def test_gate_rechecked_inside_get(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    requests: list[httpx.Request] = []
    source = source_factory(decision=Decision.pending_review)
    client = _client(
        _handler({"/holdings.csv": httpx.Response(200, text="data")}, requests), fake_clock
    )
    with pytest.raises(TermsNotAllowedError):
        client.get("https://a.example.com/holdings.csv", source=source)
    assert requests == []
