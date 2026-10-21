"""Reviewed global name overrides and the review workflow (issue #105, D54).

The workflow is proposals -> human approval -> appended overrides, and the
build must then resolve the formerly unmatched names. The tests also pin the
two things most likely to go wrong: rewriting or reordering existing override
rows, and appending duplicate decisions.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from etfh.secmaster import Override, SecurityMaster, load_overrides
from etfh.secmaster.names import name_proposals, reference_names
from etfh.secmaster.review import append_overrides, approve
from tests._secmaster_helpers import holdings_frame

REVIEWED_ON = date(2026, 10, 21)
HEADER = "match_type,match_value,security_id,reviewer,reviewed_on,note"


def _proposals(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={
            "normalized_name": pl.String,
            "example_name": pl.String,
            "country": pl.String,
            "n_funds": pl.Int64,
            "total_weight": pl.Float64,
            "candidate_security_id": pl.String,
            "candidate_name": pl.String,
            "score": pl.Float64,
            "confidence": pl.String,
        },
    )


def _proposal(
    normalized: str,
    security_id: str,
    *,
    confidence: str = "exact",
    score: float = 100.0,
) -> dict[str, object]:
    return {
        "normalized_name": normalized,
        "example_name": normalized.title(),
        "country": "CA",
        "n_funds": 2,
        "total_weight": 0.05,
        "candidate_security_id": security_id,
        "candidate_name": normalized.title(),
        "score": score,
        "confidence": confidence,
    }


def test_approve_exact_class_returns_global_name_overrides() -> None:
    proposals = _proposals(
        [
            _proposal("NORTHWIND MATERIALS", "ISIN:CA600000AA15"),
            _proposal("FOXGLOVE NETWORKS", "TKR:XNYS:FXN"),
            _proposal("CHINA SHENHUA ENERGY", "ISIN:CNE1000002R0", confidence="fuzzy", score=97.0),
        ]
    )
    overrides = approve(
        proposals, confidence="exact", reviewer="  Moksh  ", reviewed_on=REVIEWED_ON
    )
    assert [override.match_value for override in overrides] == [
        "*|FOXGLOVE NETWORKS",
        "*|NORTHWIND MATERIALS",
    ]
    assert [override.security_id for override in overrides] == [
        "TKR:XNYS:FXN",
        "ISIN:CA600000AA15",
    ]
    assert all(override.match_type == "name" for override in overrides)
    assert all(override.reviewer == "Moksh" for override in overrides)
    assert all(override.reviewed_on == REVIEWED_ON for override in overrides)
    assert overrides[0].note == "approved name proposal (confidence=exact, score=100.0)"


def test_approve_by_name_selects_only_that_name() -> None:
    proposals = _proposals(
        [
            _proposal("NORTHWIND MATERIALS", "ISIN:CA600000AA15"),
            _proposal("FOXGLOVE NETWORKS", "TKR:XNYS:FXN"),
        ]
    )
    overrides = approve(
        proposals, names=[" NORTHWIND MATERIALS "], reviewer="Moksh", reviewed_on=REVIEWED_ON
    )
    assert [override.match_value for override in overrides] == ["*|NORTHWIND MATERIALS"]


def test_approve_intersects_confidence_and_names() -> None:
    proposals = _proposals(
        [
            _proposal("NORTHWIND MATERIALS", "ISIN:CA600000AA15"),
            _proposal("CHINA SHENHUA ENERGY", "ISIN:CNE1000002R0", confidence="fuzzy", score=97.0),
        ]
    )
    assert (
        approve(
            proposals,
            confidence="exact",
            names=["CHINA SHENHUA ENERGY"],
            reviewer="Moksh",
            reviewed_on=REVIEWED_ON,
        )
        == []
    )


def test_approve_requires_a_non_empty_reviewer() -> None:
    proposals = _proposals([_proposal("NORTHWIND MATERIALS", "ISIN:CA600000AA15")])
    for reviewer in ("", "   "):
        with pytest.raises(ValueError, match="reviewer"):
            approve(proposals, confidence="exact", reviewer=reviewer, reviewed_on=REVIEWED_ON)


def test_approve_requires_a_selector() -> None:
    proposals = _proposals([_proposal("NORTHWIND MATERIALS", "ISIN:CA600000AA15")])
    with pytest.raises(ValueError, match="confidence"):
        approve(proposals, reviewer="Moksh", reviewed_on=REVIEWED_ON)


def test_approve_rejects_an_unknown_confidence() -> None:
    proposals = _proposals([_proposal("NORTHWIND MATERIALS", "ISIN:CA600000AA15")])
    with pytest.raises(ValueError, match="confidence"):
        approve(proposals, confidence="maybe", reviewer="Moksh", reviewed_on=REVIEWED_ON)


def test_approve_refuses_a_name_with_two_candidate_ids() -> None:
    proposals = _proposals(
        [
            _proposal("CHINA SHENHUA ENERGY", "ISIN:CNE1000002R0", confidence="fuzzy"),
            _proposal("CHINA SHENHUA ENERGY", "ISIN:CNE100000767", confidence="fuzzy"),
        ]
    )
    with pytest.raises(ValueError, match="ambiguous"):
        approve(proposals, confidence="fuzzy", reviewer="Moksh", reviewed_on=REVIEWED_ON)
    with pytest.raises(ValueError, match="ambiguous"):
        approve(
            proposals, names=["CHINA SHENHUA ENERGY"], reviewer="Moksh", reviewed_on=REVIEWED_ON
        )


def test_approve_missing_columns_raises() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        approve(
            pl.DataFrame({"normalized_name": ["X"]}),
            confidence="exact",
            reviewer="Moksh",
            reviewed_on=REVIEWED_ON,
        )


def test_append_overrides_creates_the_file_with_the_contract_header(tmp_path: Path) -> None:
    path = tmp_path / "config" / "security_overrides.csv"
    added = append_overrides(
        path,
        [
            Override(
                match_type="name",
                match_value="*|NORTHWIND MATERIALS",
                security_id="ISIN:CA600000AA15",
                reviewer="Moksh",
                reviewed_on=REVIEWED_ON,
            )
        ],
    )
    assert added == 1
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == [
        HEADER,
        "name,*|NORTHWIND MATERIALS,ISIN:CA600000AA15,Moksh,2026-10-21,",
    ]
    assert load_overrides(path)[0].match_value == "*|NORTHWIND MATERIALS"


def test_append_overrides_keeps_existing_rows_byte_identical(tmp_path: Path) -> None:
    path = tmp_path / "security_overrides.csv"
    existing = (
        HEADER
        + "\n"
        + 'ticker,XTSE:NWM,ISIN:CA600000AA15,Zoe,2026-10-01,"kept, exactly"\n'
        + "isin,CA600000AA15,ISIN:CA600000AA15,Ann,2026-10-02,\n"
    )
    path.write_bytes(existing.encode("utf-8"))
    added = append_overrides(
        path,
        [
            Override(
                match_type="name",
                match_value="*|FOXGLOVE NETWORKS",
                security_id="TKR:XNYS:FXN",
                reviewer="Moksh",
                reviewed_on=REVIEWED_ON,
            )
        ],
    )
    assert added == 1
    after = path.read_bytes().decode("utf-8")
    assert after.startswith(existing)
    assert after == existing + "name,*|FOXGLOVE NETWORKS,TKR:XNYS:FXN,Moksh,2026-10-21,\n"


def test_append_overrides_is_idempotent_and_never_rewrites(tmp_path: Path) -> None:
    path = tmp_path / "security_overrides.csv"
    override = Override(
        match_type="name",
        match_value="*|NORTHWIND MATERIALS",
        security_id="ISIN:CA600000AA15",
        reviewer="Moksh",
        reviewed_on=REVIEWED_ON,
    )
    assert append_overrides(path, [override]) == 1
    before = path.read_bytes()
    assert append_overrides(path, [override]) == 0
    assert path.read_bytes() == before


def test_append_overrides_sorts_new_rows_and_counts_them(tmp_path: Path) -> None:
    path = tmp_path / "security_overrides.csv"
    added = append_overrides(
        path,
        [
            Override(
                match_type="name",
                match_value="*|ZETA SYSTEMS",
                security_id="ISIN:CA600000AA99",
                reviewer="Moksh",
                reviewed_on=REVIEWED_ON,
            ),
            Override(
                match_type="ticker",
                match_value="XTSE:NWM",
                security_id="ISIN:CA600000AA15",
                reviewer="Moksh",
                reviewed_on=REVIEWED_ON,
            ),
            Override(
                match_type="name",
                match_value="*|ALPHA INDUSTRIES",
                security_id="ISIN:CA600000AA88",
                reviewer="Moksh",
                reviewed_on=REVIEWED_ON,
            ),
        ],
    )
    assert added == 3
    keys = [(item.match_type, item.match_value) for item in load_overrides(path)]
    assert keys == [
        ("name", "*|ALPHA INDUSTRIES"),
        ("name", "*|ZETA SYSTEMS"),
        ("ticker", "XTSE:NWM"),
    ]


def test_append_overrides_refuses_to_change_an_existing_decision(tmp_path: Path) -> None:
    path = tmp_path / "security_overrides.csv"
    path.write_text(
        HEADER + "\nname,*|NORTHWIND MATERIALS,ISIN:CA600000AA15,Ann,2026-10-01,\n",
        encoding="utf-8",
    )
    conflicting = Override(
        match_type="name",
        match_value="*|NORTHWIND MATERIALS",
        security_id="TKR:XTSE:NWM",
        reviewer="Moksh",
        reviewed_on=REVIEWED_ON,
    )
    with pytest.raises(ValueError, match="refusing to change"):
        append_overrides(path, [conflicting])
    assert path.read_text(encoding="utf-8").endswith("Ann,2026-10-01,\n")


def _reference_frames() -> list[pl.DataFrame]:
    return [
        SecurityMaster().assign(
            holdings_frame(
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
                        "security_name": "Foxglove Networks Inc.",
                        "security_ticker": "FXN",
                        "exchange": "XNYS",
                        "asset_class": "equity",
                        "country": "United States",
                        "currency": "USD",
                    },
                ]
            )
        )
    ]


def _name_only_frame() -> pl.DataFrame:
    return holdings_frame(
        [
            {
                "fund_ticker": "PURP",
                "security_name": "Northwind Materials",
                "asset_class": "equity",
                "country": "Canada",
                "currency": "CAD",
            },
            {
                "fund_ticker": "PURA",
                "security_name": "FOXGLOVE NETWORKS/THE",
                "asset_class": "equity",
                "country": "US",
                "currency": "USD",
            },
        ]
    )


def test_round_trip_proposals_to_approved_ids(tmp_path: Path) -> None:
    """Proposals → approve exact → append → overrides resolve the names (D54)."""
    reference = reference_names(_reference_frames())
    unmatched = _name_only_frame()
    proposals = name_proposals(unmatched, reference)
    assert set(proposals["confidence"].to_list()) == {"exact"}
    assert proposals.height == 2

    path = tmp_path / "security_overrides.csv"
    approved = approve(proposals, confidence="exact", reviewer="Moksh", reviewed_on=REVIEWED_ON)
    assert append_overrides(path, approved) == 2

    master = SecurityMaster(load_overrides(path))
    assert master.assign(unmatched)["security_id"].to_list() == [
        "ISIN:CA600000AA15",
        "TKR:XNYS:FXN",
    ]

    before = path.read_bytes()
    assert append_overrides(path, approved) == 0
    assert path.read_bytes() == before

    # The approved overrides are the review artifact: the proposal report is
    # untouched and re-resolving still yields the same ids.
    again = name_proposals(unmatched, reference)
    assert again.equals(proposals)


def test_global_name_override_matches_normalized_names_in_any_fund() -> None:
    override = Override(
        match_type="name",
        match_value="*|Northwind Materials Inc.",
        security_id="ISIN:CA600000AA15",
        reviewer="Moksh",
        reviewed_on=REVIEWED_ON,
    )
    assert override.match_value == "*|Northwind Materials Inc."
    frame = holdings_frame(
        [
            {
                "fund_ticker": "PURP",
                "security_name": "Northwind Materials",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "PURA",
                "security_name": "NORTHWIND MATERIALS/THE",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "PURA",
                "security_name": "Northwind Materials Inc. 4.5% 2030",
                "asset_class": "fixed_income",
                "country": "CA",
                "currency": "CAD",
            },
        ]
    )
    assert SecurityMaster([override]).assign(frame)["security_id"].to_list() == [
        "ISIN:CA600000AA15",
        "ISIN:CA600000AA15",
        None,
    ]


def test_a_global_name_override_matches_a_bond_and_not_the_equity() -> None:
    """Bond normalization is used on both sides, so mid-name legal forms agree."""
    override = Override(
        match_type="name",
        match_value="*|XYZ Corp 5% 2031",
        security_id="ISIN:CA600030AA84",
        reviewer="Moksh",
        reviewed_on=REVIEWED_ON,
    )
    frame = holdings_frame(
        [
            {
                "fund_ticker": "PURB",
                "security_name": "XYZ 5.00% 2031 Bond",
                "asset_class": "fixed_income",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "PURB",
                "security_name": "XYZ Corp",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
        ]
    )
    assert SecurityMaster([override]).assign(frame)["security_id"].to_list() == [
        "ISIN:CA600030AA84",
        None,
    ]


def test_a_fund_specific_name_override_wins_over_a_global_one() -> None:
    global_override = Override(
        match_type="name",
        match_value="*|NORTHWIND MATERIALS",
        security_id="ISIN:CA600000AA15",
        reviewer="Moksh",
        reviewed_on=REVIEWED_ON,
    )
    fund_override = Override(
        match_type="name",
        match_value="PURP|Northwind Materials",
        security_id="ISIN:CA600000AA99",
        reviewer="Moksh",
        reviewed_on=REVIEWED_ON,
    )
    frame = holdings_frame(
        [
            {
                "fund_ticker": "PURP",
                "security_name": "Northwind Materials",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "PURA",
                "security_name": "Northwind Materials",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
        ]
    )
    assert SecurityMaster([global_override, fund_override]).assign(frame)[
        "security_id"
    ].to_list() == ["ISIN:CA600000AA99", "ISIN:CA600000AA15"]


def test_load_overrides_accepts_global_and_fund_name_rows(tmp_path: Path) -> None:
    path = tmp_path / "security_overrides.csv"
    path.write_text(
        HEADER
        + "\n"
        + "name,*|Northwind Materials Inc.,ISIN:CA600000AA15,Moksh,2026-10-21,\n"
        + "name,PURP|Exact Name,ISIN:CA600000AA99,Moksh,2026-10-21,\n",
        encoding="utf-8",
    )
    overrides = load_overrides(path)
    assert [item.match_value for item in overrides] == [
        "*|Northwind Materials Inc.",
        "PURP|Exact Name",
    ]


def test_load_overrides_rejects_a_global_name_without_tokens(tmp_path: Path) -> None:
    path = tmp_path / "security_overrides.csv"
    path.write_text(HEADER + "\nname,*|!!!,ISIN:CA600000AA15,Moksh,2026-10-21,\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no name tokens"):
        load_overrides(path)
