"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pytest

from etfh.registry import Source, TermsRecord
from etfh.types import Decision, FileFormat, SourceKind


class FakeClock:
    """Deterministic clock whose ``sleep`` advances time instead of blocking."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


def _make_source(
    source_id: str = "syn_csv",
    *,
    decision: Decision = Decision.allowed,
    supported: bool = True,
    min_interval_seconds: float | None = None,
) -> Source:
    terms = TermsRecord(
        url="synthetic://fixtures/test",
        reviewer="synthetic fixture",
        reviewed_on=date(2026, 10, 16),
        decision=decision,
        min_interval_seconds=min_interval_seconds,
    )
    return Source(
        id=source_id,
        issuer="synthetic",
        kind=SourceKind.synthetic,
        format=FileFormat.csv,
        url_patterns=["synthetic://fixtures/<TICKER>.csv"],
        update_frequency="daily",
        supported=supported,
        terms=terms,
    )


@pytest.fixture
def source_factory() -> Callable[..., Source]:
    return _make_source
