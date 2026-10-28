"""Comparison rules of the weekly digest document (interfaces-08 §3)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from etfh.digest import build_digest, validate_digest
from etfh.digest.compare import WEIGHT_CHANGE_THRESHOLD
from tests._digest_helpers import (
    DAY,
    QUARTERLY,
    WEEK_START,
    cash,
    fund,
    holding,
    make_con,
    published,
    sources_registry,
    two_snapshot_store,
    version_sha,
)

REGISTRY = sources_registry()
TESTA = [fund("TESTA")]


def _entry(doc: dict[str, object], ticker: str = "TESTA") -> dict[str, object]:
    funds = doc["funds"]
    assert isinstance(funds, list)
    matches = [entry for entry in funds if entry["ticker"] == ticker]
    assert len(matches) == 1
    return matches[0]


def test_counts_turnover_and_change_lines() -> None:
    con = make_con(two_snapshot_store())
    doc = build_digest(con, DAY, public=False, sources=REGISTRY, funds=TESTA)
    validate_digest(doc)
    entry = _entry(doc)

    assert entry["status"] == "ok"
    assert entry["portfolio_date_start"] == "2026-10-23"
    assert entry["portfolio_date_end"] == "2026-10-30"
    assert entry["counts"] == {"adds": 1, "drops": 1, "weight_changes": 2}
    assert entry["turnover"] == pytest.approx(0.575, abs=1e-9)
    assert entry["top10_weight_start"] == pytest.approx(0.95, abs=1e-9)
    assert entry["top10_weight_end"] == pytest.approx(1.0, abs=1e-9)

    changes = entry["changes"]
    assert isinstance(changes, list)
    assert [
        (change["change_type"], change["security_id"], change["delta"]) for change in changes
    ] == [
        ("drop", "ISIN:AA0000000001", -0.5),
        ("weight_change", "ISIN:AA0000000003", 0.5),
        ("add", "ISIN:AA0000000004", 0.1),
        ("weight_change", "ISIN:AA0000000002", -0.05),
    ]
    assert changes[0]["weight_before"] == pytest.approx(0.5)
    assert changes[0]["weight_after"] == 0.0
    assert changes[2]["weight_before"] == 0.0
    assert changes[2]["weight_after"] == pytest.approx(0.1)


def test_cash_and_derivative_lines_never_count() -> None:
    rows = [
        holding("TESTA", "ISIN:AA0000000001", 0.90, as_of=WEEK_START),
        cash("TESTA", 0.05, as_of=WEEK_START),
        holding(
            "TESTA",
            "DERIV:FUTURES",
            0.05,
            as_of=WEEK_START,
            name="Futures",
            asset_class="derivative",
        ),
        holding("TESTA", "ISIN:AA0000000001", 0.90, as_of=DAY),
        cash("TESTA", 0.10, as_of=DAY),
    ]
    doc = build_digest(make_con(rows), DAY, public=False, sources=REGISTRY, funds=TESTA)
    validate_digest(doc)
    entry = _entry(doc)

    assert entry["counts"] == {"adds": 0, "drops": 0, "weight_changes": 0}
    assert entry["turnover"] == 0.0
    assert entry["changes"] == []
    assert entry["top10_weight_end"] == pytest.approx(0.90, abs=1e-9)


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (0.1000, 0.1004, 0),
        (0.1000, 0.0996, 0),
        (0.1000, 0.1005, 1),
        (0.1000, 0.0995, 1),
    ],
)
def test_weight_change_threshold(before: float, after: float, expected: int) -> None:
    rows = [
        holding("TESTA", "ISIN:AA0000000001", before, as_of=WEEK_START),
        holding("TESTA", "ISIN:AA0000000001", after, as_of=DAY),
    ]
    entry = _entry(build_digest(make_con(rows), DAY, public=False, sources=REGISTRY, funds=TESTA))
    assert WEIGHT_CHANGE_THRESHOLD == 0.0005
    assert entry["counts"]["weight_changes"] == expected
    assert len(entry["changes"]) == expected


def test_quarterly_only_fund_has_no_comparable_snapshot() -> None:
    rows = [
        holding(
            "TESTQ",
            "ISIN:AA0000000001",
            0.60,
            as_of=date(2026, 9, 30),
            source_id=QUARTERLY,
            name="Quarter Corp",
        ),
        holding(
            "TESTQ",
            "ISIN:AA0000000002",
            0.20,
            as_of=date(2026, 9, 30),
            source_id=QUARTERLY,
            name="Top Holdings Corp",
        ),
    ]
    funds = [fund("TESTA"), fund("TESTQ", source_ids=(QUARTERLY,))]
    doc = build_digest(make_con(rows), DAY, public=False, sources=REGISTRY, funds=funds)
    validate_digest(doc)

    assert [entry["ticker"] for entry in doc["funds"]] == []
    assert doc["quality"]["funds_without_data"] == ["TESTQ"]


def test_daily_versions_win_over_quarterly_files() -> None:
    rows = [
        holding("TESTM", "ISIN:AA0000000001", 0.10, source_id=QUARTERLY, name="Old Corp"),
        holding("TESTM", "ISIN:AA0000000001", 0.40, as_of=WEEK_START),
        holding("TESTM", "ISIN:AA0000000002", 0.40, as_of=DAY),
        holding("TESTM", "ISIN:AA0000000001", 0.60, as_of=DAY),
    ]
    rows[0]["as_of_date"] = date(2026, 9, 30)
    rows[0]["known_at"] = published(date(2026, 9, 30))
    rows[0]["raw_file_sha256"] = version_sha(QUARTERLY, "TESTM", date(2026, 9, 30))
    entry = _entry(
        build_digest(make_con(rows), DAY, public=False, sources=REGISTRY, funds=[fund("TESTM")]),
        ticker="TESTM",
    )
    assert entry["portfolio_date_start"] == "2026-10-23"
    assert entry["portfolio_date_end"] == "2026-10-30"
    assert entry["counts"] == {"adds": 1, "drops": 0, "weight_changes": 1}


def test_new_in_window_compares_from_the_first_snapshot_in_the_week() -> None:
    rows = [
        holding("TESTA", "ISIN:AA0000000001", 0.50, as_of=date(2026, 10, 27)),
        holding("TESTA", "ISIN:AA0000000002", 0.50, as_of=date(2026, 10, 27)),
        holding("TESTA", "ISIN:AA0000000001", 0.70, as_of=DAY),
        holding("TESTA", "ISIN:AA0000000002", 0.30, as_of=DAY),
    ]
    entry = _entry(build_digest(make_con(rows), DAY, public=False, sources=REGISTRY, funds=TESTA))
    assert entry["status"] == "new_in_window"
    assert entry["portfolio_date_start"] == "2026-10-27"
    assert entry["portfolio_date_end"] == "2026-10-30"
    assert entry["counts"]["weight_changes"] == 2


def test_new_fund_with_a_single_snapshot_has_no_comparison() -> None:
    rows = [holding("TESTA", "ISIN:AA0000000001", 1.0, as_of=date(2026, 10, 28))]
    doc = build_digest(make_con(rows), DAY, public=False, sources=REGISTRY, funds=TESTA)
    validate_digest(doc)
    entry = _entry(doc)

    assert entry["status"] == "new_in_window"
    assert entry["portfolio_date_start"] == entry["portfolio_date_end"] == "2026-10-28"
    assert entry["counts"] == {"adds": 0, "drops": 0, "weight_changes": 0}
    assert entry["turnover"] == 0.0
    assert entry["changes"] == []


def test_stale_fund_reports_no_change_data() -> None:
    rows = [
        holding("TESTA", "ISIN:AA0000000001", 0.60, as_of=date(2026, 10, 20)),
        holding("TESTA", "ISIN:AA0000000002", 0.40, as_of=date(2026, 10, 20)),
    ]
    doc = build_digest(make_con(rows), DAY, public=False, sources=REGISTRY, funds=TESTA)
    validate_digest(doc)
    entry = _entry(doc)

    assert entry["status"] == "no_change_data"
    assert entry["portfolio_date_start"] == entry["portfolio_date_end"] == "2026-10-20"
    assert entry["turnover"] == 0.0
    assert entry["top10_weight_start"] == entry["top10_weight_end"]


def test_versions_published_after_a_cutoff_never_affect_the_week() -> None:
    rows = [
        holding("TESTA", "ISIN:AA0000000001", 0.50, as_of=WEEK_START),
        holding("TESTA", "ISIN:AA0000000002", 0.50, as_of=WEEK_START),
        holding("TESTA", "ISIN:AA0000000001", 0.55, as_of=DAY),
        holding("TESTA", "ISIN:AA0000000002", 0.45, as_of=DAY),
        # Republished on 2026-10-31 after the week-end cutoff: must not be seen.
        holding(
            "TESTA",
            "ISIN:AA0000000001",
            0.10,
            as_of=DAY,
            file_version=2,
            known_at=datetime(2026, 10, 31, 14, tzinfo=UTC),
        ),
        holding(
            "TESTA",
            "ISIN:AA0000000002",
            0.90,
            as_of=DAY,
            file_version=2,
            known_at=datetime(2026, 10, 31, 14, tzinfo=UTC),
        ),
    ]
    entry = _entry(build_digest(make_con(rows), DAY, public=False, sources=REGISTRY, funds=TESTA))
    assert entry["portfolio_date_end"] == "2026-10-30"
    assert entry["counts"] == {"adds": 0, "drops": 0, "weight_changes": 2}
    assert entry["turnover"] == pytest.approx(0.05, abs=1e-9)


def test_unmatched_rows_count_but_never_appear_in_changes() -> None:
    rows = [
        holding("TESTA", "ISIN:AA0000000001", 0.90, as_of=WEEK_START),
        holding("TESTA", None, 0.10, as_of=WEEK_START, name="Ghost Holdings"),
        holding("TESTA", "ISIN:AA0000000001", 1.0, as_of=DAY),
    ]
    doc = build_digest(make_con(rows), DAY, public=False, sources=REGISTRY, funds=TESTA)
    validate_digest(doc)
    entry = _entry(doc)

    assert entry["counts"] == {"adds": 0, "drops": 1, "weight_changes": 1}
    assert entry["turnover"] == pytest.approx((0.10 + 0.10) / 2, abs=1e-9)
    assert [change["security_id"] for change in entry["changes"]] == ["ISIN:AA0000000001"]


def test_unmatched_rows_match_across_dates_by_name() -> None:
    rows = [
        holding("TESTA", None, 0.10, as_of=WEEK_START, name="Ghost Holdings", ticker="GHST"),
        holding("TESTA", "ISIN:AA0000000001", 0.90, as_of=WEEK_START),
        holding("TESTA", None, 0.10, as_of=DAY, name="Ghost Holdings", ticker="GHST"),
        holding("TESTA", "ISIN:AA0000000001", 0.90, as_of=DAY),
    ]
    entry = _entry(build_digest(make_con(rows), DAY, public=False, sources=REGISTRY, funds=TESTA))
    assert entry["counts"] == {"adds": 0, "drops": 0, "weight_changes": 0}
    assert entry["turnover"] == 0.0


def test_top_n_caps_changes_and_concentration() -> None:
    start = [holding("TESTA", f"ISIN:AA000000{i:04d}", 0.10, as_of=WEEK_START) for i in range(12)]
    end = [holding("TESTA", f"ISIN:AA000000{i:04d}", 0.05 + i / 1000, as_of=DAY) for i in range(12)]
    entry = _entry(
        build_digest(
            make_con(start + end), DAY, public=False, sources=REGISTRY, funds=TESTA, top_n=3
        )
    )
    assert len(entry["changes"]) == 3
    assert entry["counts"]["weight_changes"] == 12
    top_three = sum(0.05 + i / 1000 for i in (11, 10, 9))
    assert entry["top10_weight_end"] == pytest.approx(top_three, abs=1e-9)


def test_overlap_shifts_use_both_cutoffs_and_the_configured_threshold() -> None:
    from tests._digest_helpers import overlapping_store

    funds = [fund("TESTA"), fund("TESTB"), fund("TESTC")]
    doc = build_digest(
        make_con(overlapping_store()),
        DAY,
        public=False,
        sources=REGISTRY,
        funds=funds,
        overlap_threshold=0.01,
    )
    validate_digest(doc)
    assert doc["overlap_shifts"] == [
        {
            "fund_a": "TESTA",
            "fund_b": "TESTB",
            "overlap_start": 0.65,
            "overlap_end": 0.75,
            "delta": 0.1,
        },
        {
            "fund_a": "TESTB",
            "fund_b": "TESTC",
            "overlap_start": 0.5,
            "overlap_end": 0.6,
            "delta": 0.1,
        },
        {
            "fund_a": "TESTA",
            "fund_b": "TESTC",
            "overlap_start": 0.55,
            "overlap_end": 0.5,
            "delta": -0.05,
        },
    ]

    quiet = build_digest(
        make_con(overlapping_store()),
        DAY,
        public=False,
        sources=REGISTRY,
        funds=funds,
        overlap_threshold=0.2,
    )
    assert quiet["overlap_shifts"] == []


def test_funds_are_sorted_by_ticker_and_quality_counts_files() -> None:
    rows = two_snapshot_store() + [
        holding("TESTB", "ISIN:AA0000000001", 0.90, as_of=WEEK_START),
        holding("TESTB", "ISIN:AA0000000001", 0.80, as_of=DAY),
    ]
    funds = [fund("TESTB"), fund("TESTA")]
    doc = build_digest(
        make_con(rows, excluded=[("TESTZ", DAY)]),
        DAY,
        public=False,
        sources=REGISTRY,
        funds=funds + [fund("TESTZ")],
    )
    validate_digest(doc)
    assert [entry["ticker"] for entry in doc["funds"]] == ["TESTA", "TESTB"]
    assert doc["quality"] == {
        "files_included": 4,
        "files_excluded": 1,
        "funds_without_data": ["TESTZ"],
    }


def test_build_is_byte_stable_and_rejects_bad_arguments() -> None:
    con = make_con(two_snapshot_store())
    first = build_digest(con, DAY, public=True, sources=REGISTRY, funds=TESTA)
    second = build_digest(con, DAY, public=True, sources=REGISTRY, funds=TESTA)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert json.loads(json.dumps(first)) == first
    validate_digest(json.loads(json.dumps(first)))

    with pytest.raises(ValueError, match="top_n"):
        build_digest(con, DAY, public=True, sources=REGISTRY, funds=TESTA, top_n=0)
    with pytest.raises(ValueError, match="overlap_threshold"):
        build_digest(con, DAY, public=True, sources=REGISTRY, funds=TESTA, overlap_threshold=-1)
    with pytest.raises(ValueError, match="duplicate fund ticker"):
        build_digest(con, DAY, public=True, sources=REGISTRY, funds=[fund("TESTA"), fund("TESTA")])
