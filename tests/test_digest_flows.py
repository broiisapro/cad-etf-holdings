"""Flow estimates inside the digest (D67, interfaces-08 §3)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from etfh.analytics import FLOW_METHOD
from etfh.digest import build_digest, validate_digest
from tests._digest_helpers import (
    DAY,
    WEEK_START,
    fund,
    make_con,
    metric,
    sources_registry,
    two_snapshot_store,
)

REGISTRY = sources_registry()
TESTA = [fund("TESTA")]


def _entry(doc: dict) -> dict:
    return doc["funds"][0]


def test_flow_sum_covers_the_week_and_counts_the_days_it_has() -> None:
    metrics = [
        metric("TESTA", date(2026, 10, 26), nav=10.0, shares=1000.0),
        metric("TESTA", date(2026, 10, 27), nav=10.0, shares=1100.0),
        metric("TESTA", date(2026, 10, 28), nav=10.0, shares=1100.0),
        metric("TESTA", date(2026, 10, 30), nav=12.0, shares=1200.0),
    ]
    doc = build_digest(
        make_con(two_snapshot_store(), metrics=metrics),
        DAY,
        public=False,
        sources=REGISTRY,
        funds=TESTA,
    )
    validate_digest(doc)
    entry = _entry(doc)
    assert entry["flow_estimate_sum"] == pytest.approx(2200.0)
    assert entry["flow_days"] == 3
    assert entry["flow_method"] == FLOW_METHOD


def test_previous_share_count_may_sit_before_the_week() -> None:
    metrics = [
        metric("TESTA", date(2026, 10, 20), nav=9.0, shares=900.0),
        metric("TESTA", date(2026, 10, 26), nav=10.0, shares=1000.0),
    ]
    entry = _entry(
        build_digest(
            make_con(two_snapshot_store(), metrics=metrics),
            DAY,
            public=False,
            sources=REGISTRY,
            funds=TESTA,
        )
    )
    assert entry["flow_estimate_sum"] == pytest.approx(1000.0)
    assert entry["flow_days"] == 1


def test_metrics_published_after_the_week_end_are_not_used() -> None:
    metrics = [
        metric(
            "TESTA",
            DAY,
            nav=12.0,
            shares=1200.0,
            known_at=datetime(2026, 10, 31, 14, tzinfo=UTC),
        )
    ]
    doc = build_digest(
        make_con(two_snapshot_store(), metrics=metrics),
        DAY,
        public=False,
        sources=REGISTRY,
        funds=TESTA,
    )
    validate_digest(doc)
    assert _entry(doc)["flow_estimate_sum"] is None
    assert _entry(doc)["flow_days"] == 0


def test_a_fund_without_metrics_reports_null_and_never_raises() -> None:
    doc = build_digest(
        make_con(two_snapshot_store()), DAY, public=False, sources=REGISTRY, funds=TESTA
    )
    validate_digest(doc)
    entry = _entry(doc)
    assert entry["flow_estimate_sum"] is None
    assert entry["flow_days"] == 0
    assert entry["flow_method"] == FLOW_METHOD


def test_a_single_metrics_date_has_no_flow_estimate() -> None:
    metrics = [metric("TESTA", date(2026, 10, 27), nav=10.0, shares=1000.0)]
    entry = _entry(
        build_digest(
            make_con(two_snapshot_store(), metrics=metrics),
            DAY,
            public=False,
            sources=REGISTRY,
            funds=TESTA,
        )
    )
    assert entry["flow_estimate_sum"] is None
    assert entry["flow_days"] == 0


def test_missing_shares_or_nav_never_becomes_a_flow() -> None:
    metrics = [
        metric("TESTA", date(2026, 10, 26), nav=10.0, shares=None),
        metric("TESTA", date(2026, 10, 27), nav=None, shares=1100.0),
        metric("TESTA", date(2026, 10, 28), nav=10.0, shares=None),
    ]
    entry = _entry(
        build_digest(
            make_con(two_snapshot_store(), metrics=metrics),
            DAY,
            public=False,
            sources=REGISTRY,
            funds=TESTA,
        )
    )
    assert entry["flow_estimate_sum"] is None
    assert entry["flow_days"] == 0


def test_flows_ignore_week_start_itself() -> None:
    metrics = [
        metric("TESTA", WEEK_START, nav=10.0, shares=1000.0),
        metric("TESTA", date(2026, 10, 24), nav=10.0, shares=1050.0),
    ]
    entry = _entry(
        build_digest(
            make_con(two_snapshot_store(), metrics=metrics),
            DAY,
            public=False,
            sources=REGISTRY,
            funds=TESTA,
        )
    )
    assert entry["flow_estimate_sum"] == pytest.approx(500.0)
    assert entry["flow_days"] == 1
