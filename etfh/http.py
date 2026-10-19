"""Polite HTTP client: terms gate, per-host limiter, robots.txt, retries."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from etfh.registry import Source, require_collectable

__all__ = ["HTTPRetryError", "PoliteClient", "RobotsDisallowedError"]

_LOGGER = logging.getLogger(__name__)

_MAX_ATTEMPTS = 4
_BASE_INTERVAL_SECONDS = 1.0
_MAX_RETRY_AFTER_SECONDS = 120.0
_MAX_ROBOTS_REDIRECTS = 5


class RobotsDisallowedError(PermissionError):
    """Raised when robots.txt disallows the requested URL."""


class HTTPRetryError(Exception):
    """Raised after retryable responses (429/5xx) exhaust the attempts."""

    def __init__(self, url: str, status_code: int, reason: str | None = None) -> None:
        self.url = url
        self.status_code = status_code
        self.reason = reason
        detail = reason or f"last status {status_code} after {_MAX_ATTEMPTS} attempts"
        super().__init__(f"giving up on {url}: {detail}")


class _RetryableStatus(Exception):
    """Internal marker for a response that should be retried."""

    def __init__(self, status_code: int, retry_after: float | None) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"retryable status {status_code}")


class _RobotsPolicy:
    """Result of loading (or failing to load) a host's robots.txt."""

    def __init__(
        self,
        parser: RobotFileParser | None,
        *,
        allow_all: bool = False,
        disallow_all: bool = False,
    ) -> None:
        self._parser = parser
        self._allow_all = allow_all
        self._disallow_all = disallow_all

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self._allow_all:
            return True
        if self._disallow_all or self._parser is None:
            return False
        return self._parser.can_fetch(user_agent, url)

    def crawl_delay(self, user_agent: str) -> float | None:
        if self._parser is None:
            return None
        delay = self._parser.crawl_delay(user_agent)
        return float(delay) if delay is not None else None


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class PoliteClient:
    """An ``httpx`` wrapper that enforces the terms gate and politeness rules.

    Every request re-checks the source's terms decision, respects a per-host
    minimum interval of ``max(1s, robots Crawl-delay, source conditions)``,
    fetches and obeys robots.txt once per host, and retries 429/5xx/timeouts
    with exponential backoff and jitter (4 attempts maximum).
    """

    def __init__(
        self,
        user_agent: str | None = None,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        resolved = user_agent if user_agent is not None else os.environ.get("ETFH_USER_AGENT")
        if not resolved or ("@" not in resolved and "http" not in resolved):
            raise ValueError(
                "ETFH_USER_AGENT must identify the project and include a contact e-mail or URL"
            )
        self._user_agent = resolved
        self._client = (
            client if client is not None else httpx.Client(follow_redirects=True, timeout=30.0)
        )
        self._owns_client = client is None
        self._clock = clock if clock is not None else time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        self._next_request_at: dict[str, float] = {}
        self._robots: dict[str, _RobotsPolicy] = {}
        self._backoff = wait_exponential_jitter(initial=1.0, max=30.0)

    def close(self) -> None:
        """Close the underlying client if this instance created it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(
        self,
        url: str,
        *,
        source: Source,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> httpx.Response:
        """GET ``url`` for ``source``, applying the gate and politeness rules."""
        require_collectable(source)
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            raise ValueError(f"url must be absolute: {url!r}")
        host = parts.netloc
        policy = self._robots_for(parts.scheme, host)
        if not policy.can_fetch(self._user_agent, url):
            raise RobotsDisallowedError(f"robots.txt disallows {url}")

        interval = max(
            _BASE_INTERVAL_SECONDS,
            policy.crawl_delay(self._user_agent) or 0.0,
            source.terms.min_interval_seconds or 0.0,
        )
        headers = {"User-Agent": self._user_agent}
        if etag is not None:
            headers["If-None-Match"] = etag
        if last_modified is not None:
            headers["If-Modified-Since"] = last_modified

        def attempt() -> httpx.Response:
            self._throttle(host, interval)
            response = self._client.get(url, headers=headers)
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                if retry_after is not None and retry_after > _MAX_RETRY_AFTER_SECONDS:
                    raise HTTPRetryError(
                        url,
                        response.status_code,
                        reason=(
                            f"Retry-After {retry_after:.0f}s exceeds the "
                            f"{_MAX_RETRY_AFTER_SECONDS:.0f}s cap"
                        ),
                    )
                raise _RetryableStatus(response.status_code, retry_after)
            return response

        retryer: Retrying = Retrying(
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=self._wait,
            retry=retry_if_exception_type((_RetryableStatus, httpx.TransportError)),
            reraise=True,
            sleep=self._sleep,
        )
        try:
            return retryer(attempt)
        except _RetryableStatus as exc:
            raise HTTPRetryError(url, exc.status_code) from None

    def _wait(self, retry_state: RetryCallState) -> float:
        if retry_state.outcome is not None:
            exception = retry_state.outcome.exception()
            if isinstance(exception, _RetryableStatus) and exception.retry_after is not None:
                return exception.retry_after
        return float(self._backoff(retry_state))

    def _throttle(self, host: str, min_interval: float) -> None:
        now = self._clock()
        next_at = self._next_request_at.get(host, 0.0)
        if next_at > now:
            self._sleep(next_at - now)
            now = next_at
        self._next_request_at[host] = now + min_interval

    def _robots_for(self, scheme: str, host: str) -> _RobotsPolicy:
        cached = self._robots.get(host)
        if cached is not None:
            return cached
        policy = self._load_robots(scheme, host)
        self._robots[host] = policy
        return policy

    def _load_robots(self, scheme: str, host: str) -> _RobotsPolicy:
        """Load and interpret a host's robots.txt following RFC 9309 (D46).

        ``2xx`` responses are parsed and obeyed; ``3xx`` responses are followed
        for at most five hops and the final response is interpreted with the
        same rules; ``4xx`` responses (including 401 and 403) mean the host has
        no robots.txt, so everything is allowed and one line is logged per host
        as ``robots unavailable (status N)``. ``5xx`` responses and transport
        errors mean robots.txt is unreachable, so the host is disallowed for
        this client's lifetime. Redirects that run past the hop limit are
        treated as unreachable as well (fail closed).
        """
        url = f"{scheme}://{host}/robots.txt"
        for _ in range(_MAX_ROBOTS_REDIRECTS + 1):
            request_host = urlsplit(url).netloc or host
            self._throttle(request_host, _BASE_INTERVAL_SECONDS)
            try:
                response = self._client.get(
                    url,
                    headers={"User-Agent": self._user_agent},
                    follow_redirects=False,
                )
            except httpx.TransportError:
                _LOGGER.warning("robots.txt unreachable for %s; disallowing all requests", host)
                return _RobotsPolicy(None, disallow_all=True)
            status = response.status_code
            if 300 <= status < 400:
                location = response.headers.get("location")
                if not location:
                    _LOGGER.warning(
                        "robots.txt redirect without a Location header for %s; "
                        "disallowing all requests",
                        host,
                    )
                    return _RobotsPolicy(None, disallow_all=True)
                url = urljoin(url, location)
                continue
            if status >= 500:
                _LOGGER.warning(
                    "robots.txt unavailable (status %d) for %s; disallowing all requests",
                    status,
                    host,
                )
                return _RobotsPolicy(None, disallow_all=True)
            if 400 <= status < 500:
                _LOGGER.info("robots unavailable (status %d) for %s", status, host)
                return _RobotsPolicy(None, allow_all=True)
            if status < 200 or status >= 300:
                _LOGGER.warning(
                    "robots.txt returned unexpected status %d for %s; disallowing all requests",
                    status,
                    host,
                )
                return _RobotsPolicy(None, disallow_all=True)
            parser = RobotFileParser()
            parser.set_url(url)
            text: Any = response.text
            parser.parse(str(text).splitlines())
            return _RobotsPolicy(parser)
        _LOGGER.warning(
            "robots.txt exceeded %d redirects for %s; disallowing all requests",
            _MAX_ROBOTS_REDIRECTS,
            host,
        )
        return _RobotsPolicy(None, disallow_all=True)
