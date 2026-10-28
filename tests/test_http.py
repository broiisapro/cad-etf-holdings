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


_ACCESS_DENIED_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>"
)


def test_robots_403_access_denied_allows_all(
    fake_clock: FakeClock,
    source_factory: Callable[..., Source],
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []
    source = source_factory()
    client = _client(
        _handler(
            {
                "/robots.txt": httpx.Response(403, text=_ACCESS_DENIED_XML),
                "/holdings.csv": httpx.Response(200, text="data"),
            },
            requests,
        ),
        fake_clock,
    )
    with caplog.at_level("INFO", logger="etfh.http"):
        response = client.get("https://a.example.com/holdings.csv", source=source)
    assert response.status_code == 200
    assert [request.url.path for request in requests] == ["/robots.txt", "/holdings.csv"]
    assert caplog.text.count("robots unavailable (status 403)") == 1


def test_robots_401_allows_all(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    source = source_factory()
    client = _client(
        _handler(
            {
                "/robots.txt": httpx.Response(401, text="authentication required"),
                "/holdings.csv": httpx.Response(200, text="data"),
            }
        ),
        fake_clock,
    )
    response = client.get("https://a.example.com/holdings.csv", source=source)
    assert response.status_code == 200


def test_robots_unavailable_is_logged_once_per_host(
    fake_clock: FakeClock,
    source_factory: Callable[..., Source],
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = source_factory()
    client = _client(
        _handler(
            {
                "/robots.txt": httpx.Response(403, text=_ACCESS_DENIED_XML),
                "/holdings.csv": httpx.Response(200, text="data"),
            }
        ),
        fake_clock,
    )
    with caplog.at_level("INFO", logger="etfh.http"):
        client.get("https://a.example.com/holdings.csv", source=source)
        client.get("https://a.example.com/holdings.csv", source=source)
    assert caplog.text.count("robots unavailable (status 403)") == 1


def test_robots_redirect_is_followed_and_obeyed(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    requests: list[httpx.Request] = []
    source = source_factory()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(301, headers={"Location": "/static/robots.txt"})
        if request.url.path == "/static/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /x\n")
        if request.url.path.startswith("/x/"):
            return httpx.Response(200, text="data")
        return httpx.Response(200, text="data")

    client = _client(handler, fake_clock)
    assert client.get("https://a.example.com/open.csv", source=source).status_code == 200
    with pytest.raises(RobotsDisallowedError):
        client.get("https://a.example.com/x/holdings.csv", source=source)
    assert [request.url.path for request in requests] == [
        "/robots.txt",
        "/static/robots.txt",
        "/open.csv",
    ]


def test_robots_redirect_chain_beyond_five_disallows(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    requests: list[httpx.Request] = []
    source = source_factory()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(302, headers={"Location": "/robots-1.txt"})
        return httpx.Response(302, headers={"Location": "/robots-7.txt"})

    client = _client(handler, fake_clock)
    with pytest.raises(RobotsDisallowedError):
        client.get("https://a.example.com/holdings.csv", source=source)
    assert len(requests) == 6
    assert all(request.url.path.startswith("/robots") for request in requests)


def test_robots_redirect_without_location_disallows(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    requests: list[httpx.Request] = []
    source = source_factory()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(302)
        return httpx.Response(200, text="data")

    client = _client(handler, fake_clock)
    with pytest.raises(RobotsDisallowedError):
        client.get("https://a.example.com/holdings.csv", source=source)
    assert [request.url.path for request in requests] == ["/robots.txt"]


def test_robots_html_page_parses_to_no_rules(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    source = source_factory()
    client = _client(
        _handler(
            {
                "/robots.txt": httpx.Response(
                    200, text="<!DOCTYPE html><html><body>not a robots file</body></html>"
                ),
                "/holdings.csv": httpx.Response(200, text="data"),
            }
        ),
        fake_clock,
    )
    response = client.get("https://a.example.com/holdings.csv", source=source)
    assert response.status_code == 200


def test_robots_transport_error_disallows_without_retry(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    requests: list[httpx.Request] = []
    source = source_factory()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("connection refused")

    client = _client(handler, fake_clock)
    with pytest.raises(RobotsDisallowedError):
        client.get("https://a.example.com/holdings.csv", source=source)
    assert [request.url.path for request in requests] == ["/robots.txt"]


def test_robots_allow_all_not_cached_after_5xx(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    state = {"robots_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            state["robots_calls"] += 1
            return httpx.Response(503)
        return httpx.Response(200, text="data")

    source = source_factory()
    client = _client(handler, fake_clock)
    for _ in range(2):
        with pytest.raises(RobotsDisallowedError):
            client.get("https://a.example.com/holdings.csv", source=source)
    assert state["robots_calls"] == 1


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


def test_retry_after_above_cap_aborts_without_sleeping(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    state = {"data_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        state["data_calls"] += 1
        return httpx.Response(429, headers={"Retry-After": "121"})

    source = source_factory()
    client = _client(handler, fake_clock)
    with pytest.raises(HTTPRetryError) as excinfo:
        client.get("https://a.example.com/holdings.csv", source=source)
    assert excinfo.value.status_code == 429
    assert "Retry-After" in str(excinfo.value)
    assert state["data_calls"] == 1
    assert fake_clock.sleeps == [1.0]


def test_retry_after_at_cap_is_honoured(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    state = {"data_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        state["data_calls"] += 1
        if state["data_calls"] == 1:
            return httpx.Response(503, headers={"Retry-After": "120"})
        return httpx.Response(200, text="data")

    source = source_factory()
    client = _client(handler, fake_clock)
    response = client.get("https://a.example.com/holdings.csv", source=source)
    assert response.status_code == 200
    assert state["data_calls"] == 2
    assert 120.0 in fake_clock.sleeps


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


def test_extra_request_headers_are_sent_alongside_the_user_agent(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen["authorization"] = request.headers.get("Authorization")
        seen["user-agent"] = request.headers.get("User-Agent")
        return httpx.Response(200, text="data")

    source = source_factory()
    client = _client(handler, fake_clock)
    client.get(
        "https://api.example.com/prices",
        source=source,
        headers={"Authorization": "Token sentinel"},
    )
    assert seen["authorization"] == "Token sentinel"
    assert seen["user-agent"] == USER_AGENT


def test_extra_request_headers_cannot_spoof_the_user_agent(
    fake_clock: FakeClock, source_factory: Callable[..., Source]
) -> None:
    client = _client(_handler({"/prices": httpx.Response(200, text="data")}), fake_clock)
    with pytest.raises(ValueError, match="User-Agent"):
        client.get(
            "https://a.example.com/prices",
            source=source_factory(),
            headers={"user-agent": "someone else"},
        )
