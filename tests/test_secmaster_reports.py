"""Tests for the unmatched report and fuzzy match proposals (issue #23)."""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from etfh.secmaster import SecurityMaster, fuzzy_proposals, normalize_name, unmatched_report
from tests._secmaster_helpers import fixture_frame, holdings_frame

AS_OF = date(2026, 11, 2)

REPORT_COLUMNS = [
    "fund_ticker",
    "as_of_date",
    "security_name",
    "security_ticker",
    "exchange",
    "source_id",
    "raw_file_sha256",
]
PROPOSAL_COLUMNS = ["security_name", "candidate_security_id", "candidate_name", "score"]
PROPOSAL_SCHEMA = {
    "security_name": pl.String,
    "candidate_security_id": pl.String,
    "candidate_name": pl.String,
    "score": pl.Float64,
}


def _unmatched_row(fund_ticker: str, security_name: str) -> dict[str, object]:
    return {
        "fund_ticker": fund_ticker,
        "security_name": security_name,
        "asset_class": "equity",
        "country": "CA",
        "currency": "CAD",
    }


def _assigned_unmatched_rows() -> pl.DataFrame:
    frame = holdings_frame(
        [
            _unmatched_row("SYNX", "Zeta Corp."),
            _unmatched_row("SYNX", "Alpha Corp."),
            {
                "fund_ticker": "SYNA",
                "security_name": "Northwind Materials Inc.",
                "isin": "CA600000AA15",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
        ]
    )
    return SecurityMaster().assign(frame)


def test_unmatched_report_columns_and_rows() -> None:
    report = unmatched_report(_assigned_unmatched_rows())
    assert report.columns == REPORT_COLUMNS
    assert report.height == 2
    assert report["security_name"].to_list() == ["Alpha Corp.", "Zeta Corp."]
    assert "security_id" not in report.columns


def test_unmatched_report_sorts_by_fund_and_name() -> None:
    frame = holdings_frame(
        [
            _unmatched_row("SYNB", "Zeta"),
            _unmatched_row("SYNA", "Beta"),
            _unmatched_row("SYNA", "Alpha"),
        ]
    )
    report = unmatched_report(SecurityMaster().assign(frame))
    assert report.select(["fund_ticker", "security_name"]).rows() == [
        ("SYNA", "Alpha"),
        ("SYNA", "Beta"),
        ("SYNB", "Zeta"),
    ]


def test_unmatched_report_is_deterministic() -> None:
    assigned = _assigned_unmatched_rows()
    assert unmatched_report(assigned).equals(unmatched_report(assigned.reverse()))


def test_unmatched_report_without_security_id_raises() -> None:
    with pytest.raises(ValueError, match="security_id"):
        unmatched_report(holdings_frame([_unmatched_row("SYNX", "Alpha Corp.")]))


def test_unmatched_report_missing_columns_raises() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        unmatched_report(pl.DataFrame({"security_id": [None]}))


def test_unmatched_report_empty_for_a_fully_resolved_fixture() -> None:
    report = unmatched_report(SecurityMaster().assign(fixture_frame("SYNA", as_of=AS_OF)))
    assert report.height == 0
    assert report.columns == REPORT_COLUMNS


def test_fuzzy_proposals_find_near_duplicates() -> None:
    known = SecurityMaster().assign(fixture_frame("SYNA", as_of=AS_OF))
    unmatched = holdings_frame(
        [
            _unmatched_row("SYNX", "Northwind Materials"),
            _unmatched_row("SYNX", "Totally Unrelated Widget"),
        ]
    )
    proposals = fuzzy_proposals(unmatched, known, threshold=92)
    assert proposals.columns == PROPOSAL_COLUMNS
    assert (
        "ISIN:CA600000AA15"
        in proposals.filter(pl.col("security_name") == "Northwind Materials")[
            "candidate_security_id"
        ].to_list()
    )
    assert proposals.filter(pl.col("security_name") == "Totally Unrelated Widget").height == 0


def test_fuzzy_proposals_respect_the_threshold() -> None:
    known = pl.DataFrame(
        {"security_id": ["ISIN:CA600000AA15"], "security_name": ["Northwind Materials Inc."]}
    )
    unmatched = pl.DataFrame({"security_name": ["Northwind Material"]})
    assert fuzzy_proposals(unmatched, known, threshold=98).height == 0
    proposals = fuzzy_proposals(unmatched, known, threshold=97)
    assert proposals.height == 1
    assert float(proposals["score"][0]) == pytest.approx(97.3, abs=0.05)


def test_fuzzy_proposals_columns_and_scores() -> None:
    known = pl.DataFrame(
        {"security_id": ["ISIN:CA600000AA15"], "security_name": ["Northwind Materials Inc."]}
    )
    unmatched = pl.DataFrame({"security_name": ["Northwind Materials"]})
    proposals = fuzzy_proposals(unmatched, known, threshold=92)
    assert proposals.rows() == [
        ("Northwind Materials", "ISIN:CA600000AA15", "Northwind Materials Inc.", 100.0)
    ]


def test_fuzzy_proposals_ties_are_broken_by_candidate_id() -> None:
    known = pl.DataFrame(
        {
            "security_id": ["ISIN:CA600000AA15", "CUSIP:600000AA1"],
            "security_name": ["Northwind Materials Inc.", "Northwind Materials Inc."],
        }
    )
    unmatched = pl.DataFrame({"security_name": ["Northwind Materials"]})
    proposals = fuzzy_proposals(unmatched, known, threshold=92)
    assert proposals["candidate_security_id"].to_list() == ["CUSIP:600000AA1", "ISIN:CA600000AA15"]


def test_fuzzy_proposals_are_deterministic() -> None:
    known = SecurityMaster().assign(fixture_frame("SYNA", as_of=AS_OF))
    unmatched = holdings_frame(
        [
            _unmatched_row("SYNX", "Northwind Materials"),
            _unmatched_row("SYNX", "Maple Ridge Resources"),
            _unmatched_row("SYNX", "Oakhurst Insurance"),
        ]
    )
    first = fuzzy_proposals(unmatched, known, threshold=90)
    second = fuzzy_proposals(unmatched.reverse(), known.reverse(), threshold=90)
    assert not first.is_empty()
    assert first.equals(second)


def test_fuzzy_proposals_exclude_cash_and_derivative_candidates() -> None:
    known = pl.DataFrame(
        {
            "security_id": ["CASH:CAD", "DERIV:SYNA:CAD USD FORWARD", "ISIN:CA600000AA15"],
            "security_name": [
                "Cash and Cash Equivalents",
                "CAD/USD Forward",
                "Northwind Materials Inc.",
            ],
        }
    )
    unmatched = pl.DataFrame({"security_name": ["Cash and Cash Equivalents", "CAD/USD Forward"]})
    proposals = fuzzy_proposals(unmatched, known, threshold=92)
    assert proposals.height == 0


def test_fuzzy_proposals_never_change_security_ids() -> None:
    master = SecurityMaster()
    frame = holdings_frame(
        [
            _unmatched_row("SYNX", "Northwind Materials"),
            _unmatched_row("SYNX", "Fairhaven Financial Group"),
        ]
    )
    assigned = master.assign(frame)
    before = assigned["security_id"].to_list()
    assert before == [None, None]

    known = SecurityMaster().assign(fixture_frame("SYNA", as_of=AS_OF))
    proposals = fuzzy_proposals(unmatched_report(assigned), known, threshold=92)
    assert not proposals.is_empty()

    after = master.assign(frame)["security_id"].to_list()
    assert after == before


def test_fuzzy_proposals_require_known_columns() -> None:
    with pytest.raises(ValueError, match="known frame"):
        fuzzy_proposals(
            pl.DataFrame({"security_name": ["Alpha"]}),
            pl.DataFrame({"security_name": ["Alpha Inc."]}),
        )
    with pytest.raises(ValueError, match="unmatched frame"):
        fuzzy_proposals(
            pl.DataFrame({"other": [1]}),
            pl.DataFrame({"security_id": ["ISIN:X"], "security_name": ["Alpha Inc."]}),
        )


def test_fuzzy_proposals_reject_bad_threshold() -> None:
    known = pl.DataFrame({"security_id": ["ISIN:X"], "security_name": ["Alpha Inc."]})
    unmatched = pl.DataFrame({"security_name": ["Alpha"]})
    with pytest.raises(ValueError, match="threshold"):
        fuzzy_proposals(unmatched, known, threshold=101)
    with pytest.raises(ValueError, match="threshold"):
        fuzzy_proposals(unmatched, known, threshold=-1)


def test_fuzzy_proposals_empty_result_keeps_the_schema() -> None:
    proposals = fuzzy_proposals(
        pl.DataFrame({"security_name": ["Unrelated Widget"]}),
        pl.DataFrame({"security_id": ["ISIN:X"], "security_name": ["Alpha Inc."]}),
        threshold=92,
    )
    assert proposals.height == 0
    assert proposals.columns == PROPOSAL_COLUMNS
    assert proposals.schema == PROPOSAL_SCHEMA


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Northwind Materials Inc.", "NORTHWIND MATERIALS"),
        ("Acme Holdings Ltd.", "ACME"),
        ("Acme Corp.", "ACME"),
        ("Acme Limited", "ACME"),
        ("Compagnie Limitée", "COMPAGNIE"),
        ("O'Hara & Sons Co.", "O HARA SONS"),
        ("Maple-Ridge   Resources", "MAPLE RIDGE RESOURCES"),
        ("3M Co.", "3M"),
        ("Holdings Inc.", "HOLDINGS INC"),
    ],
)
def test_normalize_name(name: str, expected: str) -> None:
    assert normalize_name(name) == expected
