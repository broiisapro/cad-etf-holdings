"""Tests for as-of fund metrics and flow estimates (issue #151)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from etfh import analytics
from etfh.analytics import FLOW_METHOD, FundNotFoundError
from etfh.asof import cutoff_utc
from etfh.schema import FundMetrics
from tests._analytics_helpers import make_con, metric

AS_OF = date(2026, 11, 2)

_WED = date(2026, 10, 28)
_THU = date(2026, 10, 29)
_FRI = date(2026, 10, 30)
_MON = date(2026, 11, 2)

_THU_KNOWN = datetime(2026, 10, 29, 22, tzinfo=UTC)
_FRI_KNOWN = datetime(2026, 10, 30, 22, tzinfo=UTC)


def _columns() -> list[str]:
    return [
        "as_of_date",
        "shares_outstanding",
        "nav_per_unit",
        "prev_as_of_date",
        "prev_shares",
        "gap_days",
        "flow_estimate",
        "method",
    ]


def test_metrics_asof_returns_the_latest_version_per_date() -> None:
    con = make_con(
        metrics=[
            metric("MF", _THU, nav=10.0, shares=100.0, known_at=_THU_KNOWN, file_version=1),
            metric(
                "MF",
                _THU,
                nav=11.0,
                shares=110.0,
                known_at=datetime(2026, 10, 31, 22, tzinfo=UTC),
                file_version=2,
            ),
            metric("MF", _FRI, nav=12.0, shares=120.0, known_at=_FRI_KNOWN),
        ]
    )
    before = analytics.metrics_asof(con, "MF", _FRI)
    assert before["as_of_date"].to_list() == [_THU, _FRI]
    assert before["nav_per_unit"].to_list() == [10.0, 12.0]
    assert before["file_version"].to_list() == [1, 1]
    after = analytics.metrics_asof(con, "MF", date(2026, 11, 1))
    assert after["as_of_date"].to_list() == [_THU, _FRI]
    assert after["nav_per_unit"].to_list() == [11.0, 12.0]
    assert after["file_version"].to_list() == [2, 1]
    assert after.columns == list(FundMetrics.to_schema().columns)


def test_metrics_asof_excludes_a_version_known_exactly_at_the_cutoff() -> None:
    cutoff = cutoff_utc(_FRI)
    con = make_con(
        metrics=[
            metric(
                "CF",
                _THU,
                nav=10.0,
                shares=100.0,
                known_at=cutoff - timedelta(seconds=1),
            ),
            metric("CF", _FRI, nav=11.0, shares=110.0, known_at=cutoff),
        ]
    )
    frame = analytics.metrics_asof(con, "CF", _FRI)
    assert frame["as_of_date"].to_list() == [_THU]


def test_metrics_asof_returns_an_empty_frame_when_nothing_is_known() -> None:
    con = make_con(metrics=[metric("MF", _FRI, nav=10.0, shares=100.0)])
    frame = analytics.metrics_asof(con, "MF", date(2026, 10, 1))
    assert frame.is_empty()
    assert frame.columns == list(FundMetrics.to_schema().columns)


def test_metrics_asof_raises_for_a_fund_without_metrics() -> None:
    con = make_con()
    with pytest.raises(FundNotFoundError):
        analytics.metrics_asof(con, "NOPE", AS_OF)


def test_newest_known_source_wins_for_the_same_date() -> None:
    con = make_con(
        metrics=[
            metric(
                "MF",
                _THU,
                nav=10.0,
                shares=100.0,
                known_at=_THU_KNOWN,
                source_id="aaa_metrics",
            ),
            metric(
                "MF",
                _THU,
                nav=10.0,
                shares=120.0,
                known_at=_FRI_KNOWN,
                source_id="bbb_metrics",
            ),
        ]
    )
    frame = analytics.metrics_asof(con, "MF", AS_OF)
    assert frame["shares_outstanding"].to_list() == [120.0]
    assert frame["source_id"].to_list() == ["bbb_metrics"]


def test_two_day_flow_uses_shares_and_nav_from_the_same_date() -> None:
    con = make_con(
        metrics=[
            metric("FF", _THU, nav=25.0, shares=1_000_000.0, known_at=_THU_KNOWN),
            metric("FF", _FRI, nav=25.5, shares=1_050_000.0, known_at=_FRI_KNOWN),
        ]
    )
    frame = analytics.flows(con, "FF", _THU, _FRI, asof=_FRI)
    assert frame.columns == _columns()
    first, second = frame.iter_rows(named=True)
    assert first["as_of_date"] == _THU
    assert first["prev_as_of_date"] is None
    assert first["prev_shares"] is None
    assert first["gap_days"] is None
    assert first["flow_estimate"] is None
    assert second["prev_as_of_date"] == _THU
    assert second["prev_shares"] == 1_000_000.0
    assert second["gap_days"] == 1
    assert second["flow_estimate"] == pytest.approx(50_000.0 * 25.5)
    assert frame["method"].to_list() == [FLOW_METHOD, FLOW_METHOD]


def test_weekend_gap_is_one_business_day_and_a_missing_day_is_two() -> None:
    con = make_con(
        metrics=[
            metric(
                "GF", _WED, nav=10.0, shares=100.0, known_at=datetime(2026, 10, 28, 22, tzinfo=UTC)
            ),
            metric("GF", _FRI, nav=10.0, shares=110.0, known_at=_FRI_KNOWN),
            metric(
                "GF", _MON, nav=10.0, shares=120.0, known_at=datetime(2026, 11, 2, 22, tzinfo=UTC)
            ),
        ]
    )
    frame = analytics.flows(con, "GF", _WED, _MON, asof=date(2026, 11, 3))
    gaps = dict(zip(frame["as_of_date"], frame["gap_days"], strict=True))
    assert gaps[_WED] is None
    assert gaps[_FRI] == 2  # Thursday is missing
    assert gaps[_MON] == 1  # Fri -> Mon, the weekend is not a gap
    assert frame.filter(frame["gap_days"] > 1).height == 1


def test_a_date_with_nav_but_no_shares_is_not_forward_filled() -> None:
    con = make_con(
        metrics=[
            metric(
                "NF", _WED, nav=10.0, shares=100.0, known_at=datetime(2026, 10, 28, 22, tzinfo=UTC)
            ),
            metric("NF", _THU, nav=11.0, shares=None, known_at=_THU_KNOWN),
            metric("NF", _FRI, nav=12.0, shares=120.0, known_at=_FRI_KNOWN),
        ]
    )
    frame = analytics.flows(con, "NF", _WED, _FRI, asof=AS_OF)
    rows = {row["as_of_date"]: row for row in frame.iter_rows(named=True)}
    assert rows[_THU]["shares_outstanding"] is None
    assert rows[_THU]["flow_estimate"] is None
    assert rows[_THU]["prev_as_of_date"] == _WED
    # the next date looks back past the NAV-only date, to the last shares figure
    assert rows[_FRI]["prev_as_of_date"] == _WED
    assert rows[_FRI]["prev_shares"] == 100.0
    assert rows[_FRI]["gap_days"] == 2
    assert rows[_FRI]["flow_estimate"] == pytest.approx((120.0 - 100.0) * 12.0)


def test_a_date_with_shares_but_no_nav_never_borrows_an_earlier_nav() -> None:
    con = make_con(
        metrics=[
            metric(
                "SF", _WED, nav=10.0, shares=100.0, known_at=datetime(2026, 10, 28, 22, tzinfo=UTC)
            ),
            metric("SF", _THU, nav=None, shares=110.0, known_at=_THU_KNOWN),
            metric("SF", _FRI, nav=12.0, shares=120.0, known_at=_FRI_KNOWN),
        ]
    )
    frame = analytics.flows(con, "SF", _WED, _FRI, asof=AS_OF)
    rows = {row["as_of_date"]: row for row in frame.iter_rows(named=True)}
    assert rows[_THU]["nav_per_unit"] is None
    assert rows[_THU]["flow_estimate"] is None
    assert rows[_FRI]["prev_as_of_date"] == _THU
    assert rows[_FRI]["prev_shares"] == 110.0
    assert rows[_FRI]["flow_estimate"] == pytest.approx((120.0 - 110.0) * 12.0)


def test_a_republished_metrics_file_is_visible_only_after_its_known_at() -> None:
    con = make_con(
        metrics=[
            metric("RF", _THU, nav=10.0, shares=100.0, known_at=_THU_KNOWN, file_version=1),
            metric("RF", _FRI, nav=12.0, shares=150.0, known_at=_FRI_KNOWN),
            metric(
                "RF",
                _THU,
                nav=10.0,
                shares=140.0,
                known_at=datetime(2026, 11, 1, 22, tzinfo=UTC),
                file_version=2,
            ),
        ]
    )
    earlier = analytics.flows(con, "RF", _WED, date(2026, 10, 31), asof=date(2026, 10, 31))
    later = analytics.flows(con, "RF", _WED, date(2026, 10, 31), asof=date(2026, 11, 1))
    earlier_thursday = earlier.filter(earlier["as_of_date"] == _THU)["shares_outstanding"][0]
    later_thursday = later.filter(later["as_of_date"] == _THU)["shares_outstanding"][0]
    assert earlier_thursday == 100.0
    assert later_thursday == 140.0
    earlier_friday = earlier.filter(earlier["as_of_date"] == _FRI)["flow_estimate"][0]
    later_friday = later.filter(later["as_of_date"] == _FRI)["flow_estimate"][0]
    assert earlier_friday == pytest.approx((150.0 - 100.0) * 12.0)
    assert later_friday == pytest.approx((150.0 - 140.0) * 12.0)


def test_flows_defaults_asof_to_end() -> None:
    con = make_con(
        metrics=[
            metric("DF", _THU, nav=10.0, shares=100.0, known_at=_THU_KNOWN),
            metric("DF", _FRI, nav=12.0, shares=150.0, known_at=_FRI_KNOWN),
        ]
    )
    default = analytics.flows(con, "DF", _THU, _FRI)
    explicit = analytics.flows(con, "DF", _THU, _FRI, asof=_FRI)
    assert default.equals(explicit)
    # the latest version is known after the end, so the default cannot see it
    con_late = make_con(
        metrics=[
            metric("DF", _THU, nav=10.0, shares=100.0, known_at=_THU_KNOWN),
            metric(
                "DF",
                _THU,
                nav=10.0,
                shares=900.0,
                known_at=datetime(2026, 11, 1, 22, tzinfo=UTC),
                file_version=2,
            ),
        ]
    )
    frame = analytics.flows(con_late, "DF", _THU, _THU)
    assert frame["shares_outstanding"].to_list() == [100.0]


def test_flows_returns_an_empty_frame_for_an_empty_window() -> None:
    con = make_con(metrics=[metric("EF", _THU, nav=10.0, shares=100.0, known_at=_THU_KNOWN)])
    frame = analytics.flows(con, "EF", date(2026, 10, 1), date(2026, 10, 20))
    assert frame.is_empty()
    assert frame.columns == _columns()
    assert frame["gap_days"].dtype == pl.Int32
    reversed_window = analytics.flows(con, "EF", _FRI, _THU)
    assert reversed_window.is_empty()
    assert reversed_window.columns == _columns()


def test_flows_raises_for_a_fund_without_metrics() -> None:
    con = make_con()
    with pytest.raises(FundNotFoundError):
        analytics.flows(con, "NOPE", _THU, _FRI)


def test_flows_of_a_single_date_has_no_flow() -> None:
    con = make_con(metrics=[metric("OF", _THU, nav=10.0, shares=100.0, known_at=_THU_KNOWN)])
    frame = analytics.flows(con, "OF", _THU, _THU, asof=AS_OF)
    assert frame.height == 1
    assert frame["flow_estimate"].to_list() == [None]
    assert frame["prev_as_of_date"].to_list() == [None]
    assert frame["method"].to_list() == [FLOW_METHOD]


def test_flow_method_label_is_the_d67_text() -> None:
    assert FLOW_METHOD == ("Δshares × NAV (estimate; ignores distributions and in-kind effects)")
