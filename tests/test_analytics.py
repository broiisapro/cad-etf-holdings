"""Tests for the as-of-aware analytics (issue #28)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest

from etfh import analytics
from etfh.analytics import FundNotFoundError
from tests._analytics_helpers import AS_OF, change, holding, make_con, standard_rows
from tests._build_helpers import FIXTURES, build_fixture_db


def test_overlap_uses_only_files_known_by_asof(tmp_path: Path) -> None:
    out_dir = tmp_path / "build"
    build_fixture_db(out_dir)
    expected = json.loads((FIXTURES / "expected_overlap.json").read_text(encoding="utf-8"))
    connection = duckdb.connect(str(out_dir / "etfh.duckdb"), read_only=True)
    try:
        result = analytics.overlap(connection, "SYNA", "SYNB", date(2026, 11, 2))
        leaked = analytics.overlap(connection, "SYNA", "SYNB", date(2026, 11, 3))
    finally:
        connection.close()
    assert result.fund_a == "SYNA"
    assert result.fund_b == "SYNB"
    assert result.asof == date(2026, 11, 2)
    assert result.partial is False
    assert f"{result.overlap:.4f}" == expected["overlap"]
    assert result.common.height == expected["common_count"]
    got = [
        (row["security_id"], f"{row['weight_a']:.4f}", f"{row['weight_b']:.4f}")
        for row in result.common.iter_rows(named=True)
    ]
    wanted = [
        (entry["security_id"], entry["weight_a"], entry["weight_b"]) for entry in expected["common"]
    ]
    assert got == wanted
    assert abs(leaked.overlap - result.overlap) >= 0.05


def test_overlap_is_symmetric_with_a_known_value() -> None:
    con = make_con(standard_rows())
    forward = analytics.overlap(con, "XFUND", "YFUND", AS_OF)
    backward = analytics.overlap(con, "YFUND", "XFUND", AS_OF)
    assert forward.overlap == pytest.approx(0.5)
    assert backward.overlap == pytest.approx(forward.overlap)
    assert forward.common["security_id"].to_list() == ["ISIN:A", "ISIN:B"]
    assert forward.common["security_id"].to_list() == backward.common["security_id"].to_list()
    assert forward.common["weight_a"].to_list() == backward.common["weight_b"].to_list()
    assert forward.common["weight_b"].to_list() == backward.common["weight_a"].to_list()


def test_overlap_of_a_fund_with_itself_is_its_non_cash_weight() -> None:
    con = make_con(standard_rows())
    result = analytics.overlap(con, "XFUND", "XFUND", AS_OF)
    assert result.overlap == pytest.approx(0.95)
    assert result.common.height == 3
    assert "CASH:CAD" not in result.common["security_id"].to_list()


def test_overlap_excludes_cash_and_derivative_ids() -> None:
    rows = [
        holding("DFUND", "ISIN:A", 0.60),
        holding("DFUND", "ISIN:B", 0.35),
        holding("DFUND", "DERIV:DFUND:FUTURE", -0.05, asset_class="derivative"),
        holding("DFUND", "CASH:CAD", 0.10, asset_class="cash"),
        holding("EFUND", "ISIN:A", 0.30),
        holding("EFUND", "ISIN:B", 0.55),
        holding("EFUND", "DERIV:EFUND:FUTURE", 0.10, asset_class="derivative"),
        holding("EFUND", "CASH:CAD", 0.05, asset_class="cash"),
    ]
    con = make_con(rows)
    result = analytics.overlap(con, "DFUND", "EFUND", AS_OF)
    assert result.common["security_id"].to_list() == ["ISIN:A", "ISIN:B"]
    assert result.overlap == pytest.approx(0.65)


def test_concentration_known_values() -> None:
    con = make_con(standard_rows())
    metrics = analytics.concentration(con, "XFUND", AS_OF)
    assert metrics.fund == "XFUND"
    assert metrics.as_of_date == AS_OF
    assert metrics.n_holdings == 3
    assert metrics.top10_weight == pytest.approx(0.95)
    assert metrics.hhi == pytest.approx(0.5**2 + 0.3**2 + 0.15**2)
    assert metrics.partial is False


def test_concentration_is_zero_when_nothing_is_known_by_asof() -> None:
    con = make_con(standard_rows())
    metrics = analytics.concentration(con, "XFUND", date(2026, 10, 1))
    assert metrics.as_of_date is None
    assert metrics.n_holdings == 0
    assert metrics.top10_weight == 0.0
    assert metrics.hhi == 0.0
    assert metrics.partial is False


def test_partial_flag_for_quarterly_only_funds() -> None:
    con = make_con(standard_rows())
    metrics = analytics.concentration(con, "ZFUND", AS_OF)
    assert metrics.partial is True
    assert metrics.as_of_date == AS_OF
    result = analytics.overlap(con, "XFUND", "ZFUND", AS_OF)
    assert result.partial is True
    assert result.overlap == pytest.approx(0.6)


def test_daily_holdings_win_over_a_newer_quarterly_disclosure() -> None:
    con = make_con(standard_rows())
    selected = analytics.holdings_asof(con, "PFUND", date(2026, 11, 5))
    assert selected["as_of_date"].unique().to_list() == [date(2026, 10, 29)]
    assert "quarterly" not in selected["source_id"].to_list()
    metrics = analytics.concentration(con, "PFUND", date(2026, 11, 5))
    assert metrics.as_of_date == date(2026, 10, 29)
    assert metrics.partial is False


def test_late_known_version_does_not_leak_into_an_earlier_asof() -> None:
    con = make_con(standard_rows())
    before = analytics.holdings_asof(con, "QFUND", date(2026, 11, 5))
    after = analytics.holdings_asof(con, "QFUND", date(2026, 11, 6))
    assert before["as_of_date"].unique().to_list() == [date(2026, 11, 1)]
    assert after["as_of_date"].unique().to_list() == [date(2026, 11, 5)]
    assert before.filter(before["security_id"] == "ISIN:A")["weight"][0] == 0.70


def test_breakdown_sums_by_sector_and_country() -> None:
    con = make_con(standard_rows())
    sectors = analytics.breakdown(con, "XFUND", AS_OF, "sector")
    assert sectors.columns == ["key", "weight"]
    assert dict(zip(sectors["key"], sectors["weight"], strict=True)) == pytest.approx(
        {"Technology": 0.65, "Energy": 0.30, "Unknown": 0.05}
    )
    assert sectors["weight"].sum() == pytest.approx(1.0)
    countries = analytics.breakdown(con, "XFUND", AS_OF, "country")
    assert dict(zip(countries["key"], countries["weight"], strict=True)) == pytest.approx(
        {"CA": 0.80, "US": 0.15, "Unknown": 0.05}
    )


def test_breakdown_is_empty_when_nothing_is_known() -> None:
    con = make_con(standard_rows())
    frame = analytics.breakdown(con, "XFUND", date(2026, 10, 1), "sector")
    assert frame.is_empty()
    assert frame.columns == ["key", "weight"]


def test_overlap_matrix_is_complete_and_preserves_input_order() -> None:
    con = make_con(standard_rows())
    funds = ["YFUND", "XFUND", "ZFUND"]
    matrix = analytics.overlap_matrix(con, funds, AS_OF)
    assert matrix.height == 9
    assert matrix["fund_a"].to_list() == ["YFUND"] * 3 + ["XFUND"] * 3 + ["ZFUND"] * 3
    assert matrix["fund_b"].to_list() == funds * 3
    values = {
        (row["fund_a"], row["fund_b"]): row["overlap"] for row in matrix.iter_rows(named=True)
    }
    assert values[("YFUND", "YFUND")] == pytest.approx(0.95)
    assert values[("XFUND", "XFUND")] == pytest.approx(0.95)
    assert values[("YFUND", "XFUND")] == values[("XFUND", "YFUND")] == pytest.approx(0.5)
    assert values[("XFUND", "ZFUND")] == pytest.approx(0.6)


def test_changes_filters_by_fund_and_date_range() -> None:
    con = make_con(
        [],
        changes=[
            change(
                "XFUND", date(2026, 10, 29), date(2026, 10, 30), "ISIN:A", "add", None, 0.5, None
            ),
            change(
                "XFUND",
                date(2026, 10, 30),
                date(2026, 11, 2),
                "ISIN:B",
                "weight_change",
                0.2,
                0.3,
                0.1,
            ),
            change(
                "YFUND", date(2026, 10, 30), date(2026, 11, 2), "ISIN:A", "drop", 0.4, None, None
            ),
        ],
    )
    rows = analytics.changes(con, "XFUND", date(2026, 10, 30), date(2026, 11, 2))
    assert rows.height == 1
    assert rows["security_id"].to_list() == ["ISIN:B"]
    assert rows["from_as_of"].to_list() == [date(2026, 10, 30)]
    assert analytics.changes(con, "XFUND", date(2026, 10, 29), date(2026, 10, 29)).height == 0
    assert analytics.changes(con, "XFUND", date(2026, 10, 1), date(2026, 10, 30)).height == 1


def test_funds_available_and_unknown_fund() -> None:
    con = make_con(standard_rows())
    assert analytics.funds_available(con) == ["PFUND", "QFUND", "XFUND", "YFUND", "ZFUND"]
    with pytest.raises(FundNotFoundError):
        analytics.holdings_asof(con, "NOPE", AS_OF)


def test_holdings_asof_returns_only_the_selected_version() -> None:
    con = make_con(standard_rows())
    frame = analytics.holdings_asof(con, "XFUND", AS_OF)
    assert frame.height == 4
    assert frame["file_version"].unique().to_list() == [1]
    assert frame["known_at"][0] == datetime(2026, 10, 30, 22, tzinfo=UTC)
    assert frame["weight"].to_list() == [0.5, 0.3, 0.15, 0.05]
