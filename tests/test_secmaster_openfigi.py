"""Tests for the optional cached OpenFIGI lookup (issues #24, #74).

Every test is offline: an ``httpx.MockTransport`` stands in for the API and a
fake clock stands in for the rate limiter's wait.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from etfh.secmaster import FigiLookup
from tests.conftest import FakeClock

URL = "https://api.openfigi.com/v3/mapping"


class _Api:
    """A recording MockTransport around a responder."""

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)
        self._responder = responder

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)

    def bodies(self) -> list[list[dict[str, str]]]:
        return [json.loads(request.content) for request in self.requests]


def _respond_with(composite: str) -> Callable[[httpx.Request], httpx.Response]:
    def responder(request: httpx.Request) -> httpx.Response:
        jobs = json.loads(request.content)
        return httpx.Response(200, json=[{"data": [{"compositeFIGI": composite}]} for _ in jobs])

    return responder


def _lookup(
    tmp_path: Path,
    responder: Callable[[httpx.Request], httpx.Response],
    fake_clock: FakeClock,
    *,
    api_key: str | None = None,
) -> tuple[FigiLookup, _Api]:
    api = _Api(responder)
    lookup = FigiLookup(
        tmp_path,
        api_key=api_key,
        transport=api.transport,
        clock=fake_clock.time,
        sleep=fake_clock.sleep,
    )
    return lookup, api


def test_composite_figi_posts_one_job_and_normalizes_the_key(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    lookup, api = _lookup(tmp_path, _respond_with("BBG000AAA111"), fake_clock)
    assert lookup.composite_figi("ID_ISIN", "US0378331005") == "BBG000AAA111"
    assert lookup.composite_figi("id_isin", " us0378331005 ") == "BBG000AAA111"
    assert len(api.requests) == 1
    assert api.requests[0].url == httpx.URL(URL)
    assert api.bodies() == [[{"idType": "ID_ISIN", "idValue": "US0378331005"}]]
    assert "X-OPENFIGI-APIKEY" not in api.requests[0].headers
    assert lookup.requests_made == 1
    assert lookup.cache_hits == 1


def test_isin_lookups_send_no_venue_filter(tmp_path: Path, fake_clock: FakeClock) -> None:
    lookup, api = _lookup(tmp_path, _respond_with("BBG000AAA111"), fake_clock)
    lookup.composite_figi("ID_ISIN", "CA7800871021")
    assert api.bodies() == [[{"idType": "ID_ISIN", "idValue": "CA7800871021"}]]


def test_mic_is_sent_and_part_of_the_cache_key(tmp_path: Path, fake_clock: FakeClock) -> None:
    lookup, api = _lookup(tmp_path, _respond_with("BBG000AAA111"), fake_clock)
    assert lookup.composite_figi("TICKER", "RY", "XTSE") == "BBG000AAA111"
    assert lookup.composite_figi("TICKER", "RY", "xtse") == "BBG000AAA111"
    assert lookup.composite_figi("TICKER", "RY", "XNYS") == "BBG000AAA111"
    assert api.bodies() == [
        [{"idType": "TICKER", "idValue": "RY", "marketSecDes": "Equity", "micCode": "XTSE"}],
        [{"idType": "TICKER", "idValue": "RY", "marketSecDes": "Equity", "micCode": "XNYS"}],
    ]


def test_composite_exchange_code_is_sent_and_part_of_the_cache_key(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    lookup, api = _lookup(tmp_path, _respond_with("BBG000AAA111"), fake_clock)
    assert lookup.composite_figi("TICKER", "RY", exch_code="us") == "BBG000AAA111"
    assert lookup.composite_figi("TICKER", "RY", None, " US ") == "BBG000AAA111"
    assert lookup.composite_figi("TICKER", "RY", "XTSE") == "BBG000AAA111"
    assert api.bodies() == [
        [{"idType": "TICKER", "idValue": "RY", "marketSecDes": "Equity", "exchCode": "US"}],
        [{"idType": "TICKER", "idValue": "RY", "marketSecDes": "Equity", "micCode": "XTSE"}],
    ]


def test_cache_hit_makes_no_request_and_records_the_key(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    lookup, api = _lookup(tmp_path, _respond_with("BBG000AAA111"), fake_clock)
    lookup.composite_figi("TICKER", "RY", "XTSE")
    lookup.composite_figi("TICKER", "RY", "XTSE")
    assert len(api.requests) == 1
    assert lookup.cache_hits == 1
    files = sorted(tmp_path.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload == {
        "id_type": "TICKER",
        "value": "RY",
        "mic": "XTSE",
        "exch_code": None,
        "composite_figi": "BBG000AAA111",
        "ambiguous": False,
    }


def test_api_key_header_present_and_absent(tmp_path: Path, fake_clock: FakeClock) -> None:
    lookup, api = _lookup(tmp_path, _respond_with("BBG000AAA111"), fake_clock, api_key="secret")
    lookup.composite_figi("TICKER", "RY", "XTSE")
    assert api.requests[0].headers["X-OPENFIGI-APIKEY"] == "secret"

    plain, plain_api = _lookup(tmp_path / "plain", _respond_with("BBG000AAA111"), fake_clock)
    plain.composite_figi("TICKER", "RY", "XTSE")
    assert "X-OPENFIGI-APIKEY" not in plain_api.requests[0].headers


def test_user_agent_from_env_is_sent(
    tmp_path: Path, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETFH_USER_AGENT", "cad-etf-holdings (mailto:test@example.com)")
    lookup, api = _lookup(tmp_path, _respond_with("BBG000AAA111"), fake_clock)
    lookup.composite_figi("TICKER", "RY", "XTSE")
    assert api.requests[0].headers["User-Agent"] == "cad-etf-holdings (mailto:test@example.com)"


def test_batching_splits_at_ten_jobs_without_a_key(tmp_path: Path, fake_clock: FakeClock) -> None:
    lookup, api = _lookup(tmp_path, _respond_with("BBG000AAA111"), fake_clock)
    keys = [("TICKER", f"T{index:02d}", "XTSE", None) for index in range(25)]
    results = lookup.lookup_many(keys)
    assert len(results) == 25
    assert [len(body) for body in api.bodies()] == [10, 10, 5]
    assert all(value == "BBG000AAA111" for value in results.values())


def test_batching_splits_at_hundred_jobs_with_a_key(tmp_path: Path, fake_clock: FakeClock) -> None:
    lookup, api = _lookup(tmp_path, _respond_with("BBG000AAA111"), fake_clock, api_key="secret")
    keys = [("TICKER", f"T{index:03d}", "XTSE", None) for index in range(120)]
    lookup.lookup_many(keys)
    assert [len(body) for body in api.bodies()] == [100, 20]


def test_lookup_many_deduplicates_keys(tmp_path: Path, fake_clock: FakeClock) -> None:
    lookup, api = _lookup(tmp_path, _respond_with("BBG000AAA111"), fake_clock)
    key = ("TICKER", "RY", "XTSE", None)
    results = lookup.lookup_many([key, key])
    assert len(results) == 1
    assert len(api.requests) == 1
    assert api.bodies() == [
        [{"idType": "TICKER", "idValue": "RY", "marketSecDes": "Equity", "micCode": "XTSE"}]
    ]


def test_rate_limit_waits_after_twenty_five_requests(tmp_path: Path, fake_clock: FakeClock) -> None:
    lookup, api = _lookup(tmp_path, _respond_with("BBG000AAA111"), fake_clock)
    for index in range(25):
        lookup.composite_figi("TICKER", f"T{index}", "XTSE")
    assert fake_clock.sleeps == []
    lookup.composite_figi("TICKER", "T25", "XTSE")
    assert fake_clock.sleeps == [60.0]
    assert len(api.requests) == 26


def test_429_retries_after_retry_after(tmp_path: Path, fake_clock: FakeClock) -> None:
    responses = [
        httpx.Response(429, headers={"retry-after": "1"}),
        httpx.Response(200, json=[{"data": [{"compositeFIGI": "BBG000AAA111"}]}]),
    ]

    def responder(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    lookup, api = _lookup(tmp_path, responder, fake_clock)
    assert lookup.composite_figi("TICKER", "RY", "XTSE") == "BBG000AAA111"
    assert fake_clock.sleeps == [1.0]
    assert len(api.requests) == 2
    assert lookup.requests_made == 2


def test_429_uses_ratelimit_reset_without_retry_after(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    responses = [
        httpx.Response(429, headers={"ratelimit-reset": "2"}),
        httpx.Response(200, json=[{"data": [{"compositeFIGI": "BBG000AAA111"}]}]),
    ]

    def responder(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    lookup, _ = _lookup(tmp_path, responder, fake_clock)
    assert lookup.composite_figi("TICKER", "RY", "XTSE") == "BBG000AAA111"
    assert fake_clock.sleeps == [2.0]


def test_429_gives_up_after_max_attempts_without_caching(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "1"})

    lookup, api = _lookup(tmp_path, responder, fake_clock)
    assert lookup.composite_figi("TICKER", "RY", "XTSE") is None
    assert fake_clock.sleeps == [1.0, 1.0]
    assert len(api.requests) == 3
    assert lookup.composite_figi("TICKER", "RY", "XTSE") is None
    assert len(api.requests) == 6


def test_not_found_is_cached(tmp_path: Path, fake_clock: FakeClock) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"warning": "No identifier found."}])

    lookup, api = _lookup(tmp_path, responder, fake_clock)
    assert lookup.composite_figi("TICKER", "RY", "XTSE") is None
    assert lookup.ambiguous_count == 0
    assert lookup.composite_figi("TICKER", "RY", "XTSE") is None
    assert len(api.requests) == 1
    payload = json.loads(next(iter(tmp_path.glob("*.json"))).read_text(encoding="utf-8"))
    assert payload["composite_figi"] is None
    assert payload["ambiguous"] is False


def test_malformed_response_is_not_cached(tmp_path: Path, fake_clock: FakeClock) -> None:
    def short_responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    lookup, api = _lookup(tmp_path, short_responder, fake_clock)
    assert lookup.composite_figi("TICKER", "RY", "XTSE") is None
    assert lookup.composite_figi("TICKER", "RY", "XTSE") is None
    assert len(api.requests) == 2

    def broken_responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    broken, broken_api = _lookup(tmp_path / "broken", broken_responder, fake_clock)
    assert broken.composite_figi("TICKER", "RY", "XTSE") is None
    assert len(broken_api.requests) == 1


def test_transport_error_retries_then_returns_none(tmp_path: Path, fake_clock: FakeClock) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    lookup, api = _lookup(tmp_path, responder, fake_clock)
    assert lookup.composite_figi("TICKER", "RY", "XTSE") is None
    assert fake_clock.sleeps == [2.0, 4.0]
    assert len(api.requests) == 3
    assert lookup.composite_figi("TICKER", "RY", "XTSE") is None
    assert len(api.requests) == 6


def test_cache_dir_is_created(tmp_path: Path, fake_clock: FakeClock) -> None:
    cache_dir = tmp_path / "nested" / "figi-cache"
    FigiLookup(cache_dir, transport=_Api(_respond_with("BBG000AAA111")).transport)
    assert cache_dir.is_dir()


def _respond_ambiguous(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json=[{"data": [{"compositeFIGI": "BBG000AAA111"}, {"compositeFIGI": "BBG000ZZZ999"}]}],
    )


def test_ambiguous_answer_is_none_counted_and_cached(tmp_path: Path, fake_clock: FakeClock) -> None:
    lookup, api = _lookup(tmp_path, _respond_ambiguous, fake_clock)
    key = ("TICKER", "RY", "XTSE", None)
    assert lookup.composite_figi("TICKER", "RY", "XTSE") is None
    assert lookup.ambiguous_count == 1
    assert lookup.ambiguous_keys == {key}
    assert lookup.composite_figi("TICKER", "RY", "XTSE") is None
    assert len(api.requests) == 1
    assert lookup.ambiguous_count == 2
    payload = json.loads(next(iter(tmp_path.glob("*.json"))).read_text(encoding="utf-8"))
    assert payload["composite_figi"] is None
    assert payload["ambiguous"] is True


def test_repeated_composite_in_one_entry_is_not_ambiguous(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "data": [
                        {"compositeFIGI": "BBG000AAA111", "ticker": "RY", "exchCode": "CT"},
                        {"compositeFIGI": "BBG000AAA111", "ticker": "RY", "exchCode": "CN"},
                    ]
                }
            ],
        )

    lookup, _ = _lookup(tmp_path, responder, fake_clock)
    assert lookup.composite_figi("ID_ISIN", "CA7800871021") == "BBG000AAA111"
    assert lookup.ambiguous_count == 0
    assert lookup.ambiguous_keys == set()


def test_cached_ambiguity_is_reloaded_without_a_request(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    first, api = _lookup(tmp_path, _respond_ambiguous, fake_clock)
    first.composite_figi("TICKER", "RY", "XTSE")
    assert len(api.requests) == 1
    second, second_api = _lookup(tmp_path, _respond_ambiguous, fake_clock)
    assert second.composite_figi("TICKER", "RY", "XTSE") is None
    assert second_api.requests == []
    assert second.ambiguous_keys == {("TICKER", "RY", "XTSE", None)}
