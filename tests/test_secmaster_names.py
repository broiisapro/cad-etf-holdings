"""Name normalization and review proposals (issue #104, D54).

The classic bug this file guards against is a bond matching its issuer's
equity: "Royal Bank of Canada" must never absorb "Royal Bank of Canada 4.5%
2030". Every test is offline and uses synthetic names.
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest
from rapidfuzz import fuzz

from etfh.secmaster import SecurityMaster
from etfh.secmaster.names import (
    infer_kind,
    name_proposals,
    normalize_country,
    normalize_name,
    reference_names,
)
from tests._secmaster_helpers import fixture_frame, holdings_frame

AS_OF = date(2026, 11, 2)

PROPOSAL_COLUMNS = [
    "normalized_name",
    "example_name",
    "country",
    "n_funds",
    "total_weight",
    "candidate_security_id",
    "candidate_name",
    "score",
    "confidence",
]


@pytest.mark.parametrize(
    "name",
    [
        "Northwind Materials Inc.",
        "Northwind Materials Inc",
        "Northwind Materials Incorporated",
        "Northwind Materials Corp.",
        "Northwind Materials Corporation",
        "Northwind Materials Ltd.",
        "Northwind Materials Limited",
        "Northwind Materials Co.",
        "Northwind Materials Company",
        "Northwind Materials PLC",
        "Northwind Materials SA",
        "Northwind Materials AG",
        "Northwind Materials NV",
        "Northwind Materials ULC",
    ],
)
def test_equity_normalization_drops_legal_forms(name: str) -> None:
    assert normalize_name(name) == "NORTHWIND MATERIALS"


@pytest.mark.parametrize(
    "name",
    [
        "The Northwind Materials Inc.",
        "Northwind Materials Inc. (The)",
        "NORTHWIND MATERIALS/THE",
    ],
)
def test_equity_normalization_drops_the(name: str) -> None:
    assert normalize_name(name) == "NORTHWIND MATERIALS"


@pytest.mark.parametrize(
    "name",
    [
        "Northwind Materials Inc. Class A",
        "NORTHWIND MATERIALS INC CL A",
        "Northwind Materials Inc. Series 1",
        "Northwind Materials Inc. COM SHS",
        "Northwind Materials Inc. Common Shares",
        "Northwind Materials Inc. SUB VTG",
    ],
)
def test_equity_normalization_drops_share_class_and_form_words(name: str) -> None:
    assert normalize_name(name) == "NORTHWIND MATERIALS"


def test_equity_normalization_folds_case_punctuation_ampersands_and_accents() -> None:
    assert normalize_name("Johnson & Johnson") == normalize_name("Johnson and Johnson")
    assert normalize_name("Johnson & Johnson") == "JOHNSON AND JOHNSON"
    assert normalize_name("Café Rouge Inc.") == normalize_name("CAFE ROUGE") == "CAFE ROUGE"
    assert normalize_name("Toronto-Dominion Bank") == "TORONTO DOMINION BANK"
    assert normalize_name("TORONTO-DOMINION BANK/THE") == "TORONTO DOMINION BANK"
    assert normalize_name("Bank of Nova Scotia (The)") == "BANK OF NOVA SCOTIA"


def test_bond_normalization_keeps_coupon_and_maturity() -> None:
    assert normalize_name("Royal Bank of Canada 4.5% 2030", kind="bond") == (
        "ROYAL BANK OF CANADA 4.5 2030"
    )
    assert normalize_name("ROYAL BANK OF CANADA 4.50 % 2030", kind="bond") == (
        "ROYAL BANK OF CANADA 4.5 2030"
    )
    assert normalize_name("XYZ Corp 5% 2031", kind="bond") == normalize_name(
        "XYZ 5% 2031 Bond", kind="bond"
    )


def test_bond_does_not_normalize_to_its_issuer_equity() -> None:
    equity = normalize_name("Royal Bank of Canada")
    bond = normalize_name("Royal Bank of Canada 4.5% 2030", kind="bond")
    assert equity == "ROYAL BANK OF CANADA"
    assert bond == "ROYAL BANK OF CANADA 4.5 2030"
    assert equity != bond
    assert fuzz.token_sort_ratio(equity, bond) < 92


def test_infer_kind_marks_coupons_years_and_bond_words() -> None:
    assert infer_kind("Royal Bank of Canada 4.5% 2030") == "bond"
    assert infer_kind("Canada 2.75% 2032") == "bond"
    assert infer_kind("Province of Ontario 2031") == "bond"
    assert infer_kind("XYZ Debenture") == "bond"
    assert infer_kind("Royal Bank of Canada") == "equity"
    assert infer_kind("Alphabet Inc. Class A") == "equity"


def test_other_kind_only_folds_case_and_punctuation() -> None:
    assert normalize_name("XYZ Fund Units Class A", kind="other") == "XYZ FUND UNITS CLASS A"


def test_normalize_name_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError):
        normalize_name("XYZ", kind="fund")  # type: ignore[arg-type]


def test_normalize_country_maps_names_and_treats_unknown_as_none() -> None:
    assert normalize_country("United States") == "US"
    assert normalize_country("usa") == "US"
    assert normalize_country(" Canada ") == "CA"
    assert normalize_country("United Kingdom") == "GB"
    assert normalize_country("Unknown") is None
    assert normalize_country(None) is None
    assert normalize_country("CA") == "CA"


def _reference_frame() -> pl.DataFrame:
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNA",
                "security_name": "Northwind Materials Inc.",
                "isin": "CA600000AA15",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNB",
                "security_name": "NORTHWIND MATERIALS/THE",
                "security_ticker": "NWM",
                "exchange": "XTSE",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNB",
                "security_name": "Foxglove Networks Inc.",
                "security_ticker": "FXN",
                "exchange": "XNYS",
                "asset_class": "equity",
                "country": "United States",
                "currency": "USD",
            },
            {
                "fund_ticker": "SYNB",
                "security_name": "Royal Bank of Canada",
                "isin": "CA7800871021",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNB",
                "security_name": "Royal Bank of Canada 4.5% 2030",
                "cusip": "780087AT9",
                "asset_class": "fixed_income",
                "country": "CA",
                "currency": "CAD",
            },
        ]
    )
    return SecurityMaster().assign(frame)


def _with_weight(frame: pl.DataFrame, weights: list[float]) -> pl.DataFrame:
    """Add the optional ``weight`` column ``holdings_frame`` does not build."""
    return frame.with_columns(pl.Series("weight", weights, dtype=pl.Float64))


class _Reference:
    """Reference set plus the raw frames that produced it."""

    def __init__(self) -> None:
        self.frames = [_reference_frame()]
        self.table = reference_names(self.frames)


def test_reference_names_keeps_identifier_backed_rows_only() -> None:
    reference = _Reference().table
    assert reference.columns == [
        "normalized_name",
        "country",
        "security_id",
        "example_name",
        "n_rows",
    ]
    assert reference.height == 5
    assert reference["security_id"].to_list() == [
        "TKR:XNYS:FXN",
        "ISIN:CA600000AA15",
        "TKR:XTSE:NWM",
        "ISIN:CA7800871021",
        "ISIN:CA780087AT93",
    ]


def test_reference_names_excludes_name_override_only_rows_and_non_securities() -> None:
    from etfh.secmaster import Override

    override = Override(
        match_type="name",
        match_value="SYNA|Ghost Holdings",
        security_id="ISIN:CA600000AA99",
        reviewer="Reviewer",
        reviewed_on=date(2026, 10, 17),
    )
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNA",
                "security_name": "Ghost Holdings",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNA",
                "security_name": "Cash",
                "asset_class": "cash",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNA",
                "security_name": "Northwind Call",
                "security_ticker": "NWMX 255",
                "asset_class": "derivative",
                "currency": "CAD",
            },
        ]
    )
    assigned = SecurityMaster([override]).assign(frame)
    assert assigned["security_id"].to_list()[0] == "ISIN:CA600000AA99"
    assert reference_names([assigned]).height == 0


def test_reference_names_aggregates_rows_and_picks_a_stable_example() -> None:
    reference = _Reference().table
    northwind = reference.filter(pl.col("normalized_name") == "NORTHWIND MATERIALS")
    assert northwind.height == 2
    assert northwind["n_rows"].to_list() == [1, 1]
    assert set(northwind["example_name"].to_list()) == {
        "Northwind Materials Inc.",
        "NORTHWIND MATERIALS/THE",
    }
    assert reference["normalized_name"].to_list() == sorted(reference["normalized_name"].to_list())


def test_reference_names_rejects_frames_missing_columns() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        reference_names([pl.DataFrame({"security_name": ["X"]})])


def test_exact_requires_exactly_one_candidate_id() -> None:
    reference = pl.DataFrame(
        {
            "normalized_name": ["NORTHWIND MATERIALS", "NORTHWIND MATERIALS"],
            "country": ["CA", "CA"],
            "security_id": ["ISIN:CA600000AA15", "ISIN:CA600000AA99"],
            "example_name": ["Northwind Materials Inc.", "Northwind Materials Ltd."],
            "n_rows": [3, 1],
        }
    )
    unmatched = holdings_frame(
        [
            {
                "fund_ticker": "PURP",
                "security_name": "Northwind Materials",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            }
        ]
    )
    proposals = name_proposals(unmatched, reference)
    assert proposals.columns == PROPOSAL_COLUMNS
    assert proposals["confidence"].to_list() == ["fuzzy", "fuzzy"]
    assert proposals["candidate_security_id"].to_list() == [
        "ISIN:CA600000AA15",
        "ISIN:CA600000AA99",
    ]
    assert proposals["score"].to_list() == [100.0, 100.0]


def test_exact_proposal_for_one_candidate() -> None:
    proposals = name_proposals(
        holdings_frame(
            [
                {
                    "fund_ticker": "PURP",
                    "security_name": "Foxglove Networks",
                    "asset_class": "equity",
                    "country": "United States",
                    "currency": "USD",
                }
            ]
        ),
        _Reference().table,
    )
    assert proposals.height == 1
    row = proposals.row(0, named=True)
    assert row["normalized_name"] == "FOXGLOVE NETWORKS"
    assert row["candidate_security_id"] == "TKR:XNYS:FXN"
    assert row["confidence"] == "exact"
    assert row["score"] == 100.0
    assert row["n_funds"] == 1


def test_a_name_with_two_identifier_backed_ids_is_ambiguous() -> None:
    """Northwind appears by ISIN in one fund and by ticker in another."""
    proposals = name_proposals(
        holdings_frame(
            [
                {
                    "fund_ticker": "PURP",
                    "security_name": "Northwind Materials",
                    "asset_class": "equity",
                    "country": "CA",
                    "currency": "CAD",
                }
            ]
        ),
        _Reference().table,
    )
    assert proposals.height == 2
    assert proposals["candidate_security_id"].to_list() == [
        "ISIN:CA600000AA15",
        "TKR:XTSE:NWM",
    ]
    assert proposals["confidence"].to_list() == ["fuzzy", "fuzzy"]
    assert proposals["score"].to_list() == [100.0, 100.0]


def test_exact_requires_countries_to_agree_when_both_are_known() -> None:
    reference = pl.DataFrame(
        {
            "normalized_name": ["NORTHWIND MATERIALS", "NORTHWIND MATERIALS"],
            "country": ["CA", "US"],
            "security_id": ["ISIN:CA600000AA15", "US0378331005"],
            "example_name": ["Northwind Materials Inc.", "Northwind Materials Inc."],
            "n_rows": [3, 1],
        }
    )
    ca = name_proposals(
        holdings_frame(
            [
                {
                    "fund_ticker": "PURP",
                    "security_name": "Northwind Materials",
                    "asset_class": "equity",
                    "country": "Canada",
                    "currency": "CAD",
                }
            ]
        ),
        reference,
    )
    assert ca["candidate_security_id"].to_list() == ["ISIN:CA600000AA15"]
    assert ca["confidence"].to_list() == ["exact"]

    unknown = name_proposals(
        holdings_frame(
            [
                {
                    "fund_ticker": "PURP",
                    "security_name": "Northwind Materials",
                    "asset_class": "equity",
                    "country": "Unknown",
                    "currency": "CAD",
                }
            ]
        ),
        reference,
    )
    assert unknown["confidence"].to_list() == ["fuzzy", "fuzzy"]
    assert unknown.height == 2


def test_bond_name_is_not_proposed_for_its_issuer_equity() -> None:
    proposals = name_proposals(
        holdings_frame(
            [
                {
                    "fund_ticker": "PURP",
                    "security_name": "Royal Bank of Canada 4.5% 2030",
                    "asset_class": "fixed_income",
                    "country": "CA",
                    "currency": "CAD",
                },
                {
                    "fund_ticker": "PURP",
                    "security_name": "Royal Bank of Canada",
                    "asset_class": "equity",
                    "country": "CA",
                    "currency": "CAD",
                },
            ]
        ),
        _Reference().table,
    )
    bond = proposals.filter(pl.col("normalized_name") == "ROYAL BANK OF CANADA 4.5 2030")
    assert bond["candidate_security_id"].to_list() == ["ISIN:CA780087AT93"]
    assert "ISIN:CA7800871021" not in bond["candidate_security_id"].to_list()
    equity = proposals.filter(pl.col("normalized_name") == "ROYAL BANK OF CANADA")
    assert equity["candidate_security_id"].to_list() == ["ISIN:CA7800871021"]


def test_fuzzy_threshold_is_inclusive_and_deterministic() -> None:
    reference = pl.DataFrame(
        {
            "normalized_name": ["NORTHWIND MATERIAL"],
            "country": ["CA"],
            "security_id": ["ISIN:CA600000AA15"],
            "example_name": ["Northwind Material Inc."],
            "n_rows": [1],
        }
    )
    unmatched = holdings_frame(
        [
            {
                "fund_ticker": "PURP",
                "security_name": "Northwind Materials",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            }
        ]
    )
    score = fuzz.token_sort_ratio("NORTHWIND MATERIALS", "NORTHWIND MATERIAL")
    assert score < 100
    inclusive = name_proposals(unmatched, reference, fuzzy_threshold=int(score))
    assert inclusive.height == 1
    assert inclusive["score"].to_list() == [score]
    assert inclusive["confidence"].to_list() == ["fuzzy"]
    excluded = name_proposals(unmatched, reference, fuzzy_threshold=int(score) + 1)
    assert excluded.height == 0
    assert excluded.columns == PROPOSAL_COLUMNS


def test_proposals_sort_by_weight_then_name_and_are_input_order_independent() -> None:
    reference = _Reference().table
    rows = [
        {
            "fund_ticker": "PURP",
            "security_name": "Royal Bank of Canada",
            "asset_class": "equity",
            "country": "CA",
            "currency": "CAD",
            "weight": 0.01,
        },
        {
            "fund_ticker": "PURB",
            "security_name": "Foxglove Networks",
            "asset_class": "equity",
            "country": "US",
            "currency": "USD",
            "weight": 0.04,
        },
    ]
    unmatched = _with_weight(holdings_frame(rows), [0.01, 0.04])
    forward = name_proposals(unmatched, reference)
    backward = name_proposals(unmatched.reverse(), reference)
    assert forward.equals(backward)
    assert forward["normalized_name"].to_list() == [
        "FOXGLOVE NETWORKS",
        "ROYAL BANK OF CANADA",
    ]
    assert forward["total_weight"].to_list() == pytest.approx([0.04, 0.01])
    assert forward["n_funds"].to_list() == [1, 1]


def test_name_proposals_counts_funds_and_sums_weights() -> None:
    unmatched = holdings_frame(
        [
            {
                "fund_ticker": "PURP",
                "security_name": "Foxglove Networks",
                "asset_class": "equity",
                "country": "US",
                "currency": "USD",
                "weight": 0.02,
            },
            {
                "fund_ticker": "PURA",
                "security_name": "FOXGLOVE NETWORKS/THE",
                "asset_class": "equity",
                "country": "United States",
                "currency": "USD",
                "weight": 0.03,
            },
        ]
    )
    proposals = name_proposals(_with_weight(unmatched, [0.02, 0.03]), _Reference().table)
    assert proposals.height == 1
    row = proposals.row(0, named=True)
    assert row["n_funds"] == 2
    assert row["total_weight"] == pytest.approx(0.05)
    assert row["example_name"] == "FOXGLOVE NETWORKS/THE"


def test_name_proposals_rejects_invalid_input() -> None:
    reference = _Reference().table
    unmatched = holdings_frame(
        [
            {
                "fund_ticker": "PURP",
                "security_name": "Northwind Materials",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            }
        ]
    )
    with pytest.raises(ValueError, match="fuzzy_threshold"):
        name_proposals(unmatched, reference, fuzzy_threshold=101)
    with pytest.raises(ValueError, match="security_name"):
        name_proposals(pl.DataFrame({"security_id": [None]}), reference)
    with pytest.raises(ValueError, match="reference frame"):
        name_proposals(unmatched, pl.DataFrame({"security_name": ["X"]}))


def test_empty_inputs_return_empty_frames_with_the_contract_columns() -> None:
    empty_reference = reference_names([])
    assert empty_reference.height == 0
    assert empty_reference.columns == [
        "normalized_name",
        "country",
        "security_id",
        "example_name",
        "n_rows",
    ]
    unmatched = holdings_frame(
        [
            {
                "fund_ticker": "PURP",
                "security_name": "Northwind Materials",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            }
        ]
    )
    proposals = name_proposals(unmatched, empty_reference)
    assert proposals.height == 0
    assert proposals.columns == PROPOSAL_COLUMNS


def test_reference_from_the_synthetic_store_proposes_without_merging() -> None:
    """End to end on committed fixtures: a name-only row gets a proposal."""
    frames = [
        SecurityMaster().assign(fixture_frame(ticker, as_of=AS_OF)) for ticker in ("SYNA", "SYNB")
    ]
    reference = reference_names(frames)
    assert reference.height > 0
    unmatched = holdings_frame(
        [
            {
                "fund_ticker": "PURP",
                "security_name": "Northwind Materials",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            }
        ]
    )
    proposals = name_proposals(unmatched, reference)
    assert proposals.filter(pl.col("candidate_security_id") == "ISIN:CA600000AA15").height == 1
    # Proposals change nothing about the resolver: the source frame still has
    # a null id until a human approves an override.
    assert SecurityMaster().assign(unmatched)["security_id"].to_list() == [None]
