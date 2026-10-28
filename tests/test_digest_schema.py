"""Schema validation of the digest document (interfaces-08 §3)."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import date

import pytest

from etfh.digest import build_digest, validate_digest
from etfh.digest.schema import SCHEMA_VERSION
from tests._digest_helpers import DAY, fund, make_con, overlapping_store, sources_registry

REGISTRY = sources_registry()
FUNDS = [fund("TESTA"), fund("TESTB"), fund("TESTC"), fund("TESTZ")]

Mutator = Callable[[dict], None]


def _build(public: bool = False) -> dict:
    return build_digest(
        make_con(overlapping_store(), excluded=[("TESTZ", DAY)]),
        DAY,
        public=public,
        sources=REGISTRY,
        funds=FUNDS,
    )


@pytest.fixture()
def document() -> dict:
    doc = _build()
    validate_digest(doc)
    return doc


def _assert_invalid(doc: dict, expected_path: str) -> None:
    mutated = copy.deepcopy(doc)
    with pytest.raises(ValueError) as error:
        validate_digest(mutated)
    assert expected_path in str(error.value), str(error.value)


def test_a_built_document_is_valid_and_json_stable(document: dict) -> None:
    assert document["schema_version"] == SCHEMA_VERSION
    assert [entry["ticker"] for entry in document["funds"]] == ["TESTA", "TESTB", "TESTC"]
    assert len(document["overlap_shifts"]) == 3
    round_tripped = json.loads(json.dumps(document, sort_keys=True))
    validate_digest(round_tripped)


def test_a_public_document_is_valid() -> None:
    doc = _build(public=True)
    validate_digest(doc)


@pytest.mark.parametrize(
    "key",
    [
        "schema_version",
        "week_start",
        "week_end",
        "known_by",
        "public",
        "demo",
        "funds",
        "overlap_shifts",
        "quality",
    ],
)
def test_every_top_level_key_is_required(document: dict, key: str) -> None:
    mutated = copy.deepcopy(document)
    mutated.pop(key)
    with pytest.raises(ValueError, match=f"document: missing key '{key}'"):
        validate_digest(mutated)


def test_an_unknown_top_level_key_fails(document: dict) -> None:
    mutated = copy.deepcopy(document)
    mutated["extra"] = 1
    with pytest.raises(ValueError, match="unexpected key 'extra'"):
        validate_digest(mutated)


def test_an_unknown_nested_key_fails(document: dict) -> None:
    mutated = copy.deepcopy(document)
    mutated["funds"][0]["counts"]["other"] = 0
    with pytest.raises(ValueError, match="funds\\[0\\].counts: unexpected key 'other'"):
        validate_digest(mutated)


MUTATIONS: list[tuple[str, Mutator, str]] = [
    ("schema version", lambda doc: doc.update({"schema_version": 2}), "schema_version"),
    ("schema version type", lambda doc: doc.update({"schema_version": 1.0}), "schema_version"),
    (
        "week_start not a week earlier",
        lambda doc: doc.update({"week_start": "2026-10-22"}),
        "week_start",
    ),
    ("week_end format", lambda doc: doc.update({"week_end": "20261030"}), "week_end"),
    ("week_end impossible", lambda doc: doc.update({"week_end": "2026-02-30"}), "week_end"),
    (
        "known_by time",
        lambda doc: doc.update({"known_by": "2026-10-31T03:00:00+00:00"}),
        "known_by",
    ),
    (
        "known_by not UTC",
        lambda doc: doc.update({"known_by": "2026-10-31T00:00:00-04:00"}),
        "known_by",
    ),
    ("known_by text", lambda doc: doc.update({"known_by": "tomorrow"}), "known_by"),
    ("public type", lambda doc: doc.update({"public": "true"}), "public"),
    ("demo type", lambda doc: doc.update({"demo": 0}), "demo"),
    ("funds type", lambda doc: doc.update({"funds": {}}), "funds"),
    ("funds order", lambda doc: doc["funds"].reverse(), "funds[0]"),
    ("duplicate ticker", lambda doc: doc["funds"][1].update({"ticker": "TESTA"}), "funds[1]"),
    ("empty ticker", lambda doc: doc["funds"][0].update({"ticker": ""}), "funds[0].ticker"),
    ("missing name", lambda doc: doc["funds"][0].pop("name"), "missing key 'name'"),
    ("status enum", lambda doc: doc["funds"][0].update({"status": "changed"}), "funds[0].status"),
    (
        "portfolio date format",
        lambda doc: doc["funds"][0].update({"portfolio_date_start": "23-10-2026"}),
        "funds[0].portfolio_date_start",
    ),
    (
        "portfolio date impossible",
        lambda doc: doc["funds"][0].update({"portfolio_date_end": "2026-13-01"}),
        "funds[0].portfolio_date_end",
    ),
    (
        "negative count",
        lambda doc: doc["funds"][0]["counts"].update({"adds": -1}),
        "funds[0].counts.adds",
    ),
    (
        "float count",
        lambda doc: doc["funds"][0]["counts"].update({"drops": 1.0}),
        "funds[0].counts.drops",
    ),
    (
        "negative turnover",
        lambda doc: doc["funds"][0].update({"turnover": -0.1}),
        "funds[0].turnover",
    ),
    (
        "unrounded turnover",
        lambda doc: doc["funds"][0].update({"turnover": 0.1234567}),
        "not rounded to 6 decimal places",
    ),
    (
        "unrounded concentration",
        lambda doc: doc["funds"][0].update({"top10_weight_end": 0.9999999}),
        "funds[0].top10_weight_end",
    ),
    (
        "flow sum type",
        lambda doc: doc["funds"][0].update({"flow_estimate_sum": "1250000.0"}),
        "funds[0].flow_estimate_sum",
    ),
    (
        "flow days without a sum",
        lambda doc: doc["funds"][0].update({"flow_estimate_sum": None, "flow_days": 2}),
        "funds[0].flow_days",
    ),
    (
        "flow method label",
        lambda doc: doc["funds"][0].update({"flow_method": "shares x NAV"}),
        "funds[0].flow_method",
    ),
    ("changes type", lambda doc: doc["funds"][0].update({"changes": {}}), "funds[0].changes"),
    (
        "change type enum",
        lambda doc: doc["funds"][0]["changes"][0].update({"change_type": "move"}),
        "funds[0].changes[0].change_type",
    ),
    (
        "delta inconsistent",
        lambda doc: doc["funds"][0]["changes"][0].update({"delta": 0.25}),
        "funds[0].changes[0].delta",
    ),
    (
        "add with a starting weight",
        lambda doc: doc["funds"][0]["changes"][2].update({"weight_before": 0.1, "delta": 0.0}),
        "funds[0].changes[2].weight_before",
    ),
    (
        "drop with an ending weight",
        lambda doc: doc["funds"][0]["changes"][0].update({"weight_after": 0.1, "delta": -0.4}),
        "funds[0].changes[0].weight_after",
    ),
    (
        "weight change with no move",
        lambda doc: doc["funds"][0]["changes"][1].update({"weight_after": 0.3, "delta": 0.0}),
        "funds[0].changes[1].delta",
    ),
    (
        "duplicate change identifier",
        lambda doc: doc["funds"][0]["changes"][1].update(
            {"security_id": doc["funds"][0]["changes"][0]["security_id"]}
        ),
        "duplicate security_id",
    ),
    (
        "changes out of order",
        lambda doc: doc["funds"][0]["changes"].reverse(),
        "funds[0].changes[0]",
    ),
    (
        "more change lines than counts",
        lambda doc: doc["funds"][0]["counts"].update({"adds": 0}),
        "lists 1 add line(s) but counts.adds is 0",
    ),
    (
        "overlap delta inconsistent",
        lambda doc: doc["overlap_shifts"][0].update({"delta": 0.5}),
        "overlap_shifts[0].delta",
    ),
    (
        "overlap pair reversed",
        lambda doc: doc["overlap_shifts"][0].update({"fund_a": "TESTB", "fund_b": "TESTA"}),
        "fund_a must sort before fund_b",
    ),
    (
        "overlap fund unknown",
        lambda doc: doc["overlap_shifts"][0].update({"fund_b": "ZZZZ"}),
        "fund 'ZZZZ' is not in funds",
    ),
    (
        "overlap order",
        lambda doc: doc["overlap_shifts"].reverse(),
        "overlap_shifts[0]",
    ),
    (
        "overlap values unrounded",
        lambda doc: doc["overlap_shifts"][0].update(
            {"overlap_start": 0.6500001, "delta": 0.0999999}
        ),
        "not rounded",
    ),
    (
        "negative file count",
        lambda doc: doc["quality"].update({"files_excluded": -1}),
        "quality.files_excluded",
    ),
    (
        "quality extra key",
        lambda doc: doc["quality"].update({"note": ""}),
        "quality: unexpected key 'note'",
    ),
    (
        "funds without data unsorted",
        lambda doc: doc["quality"].update({"funds_without_data": ["ZZZ", "AAA"]}),
        "quality.funds_without_data[0]",
    ),
    (
        "funds without data duplicated",
        lambda doc: doc["quality"].update({"funds_without_data": ["TESTZ", "TESTZ"]}),
        "quality.funds_without_data[1]",
    ),
    (
        "fund both present and missing",
        lambda doc: doc["quality"].update({"funds_without_data": ["TESTA"]}),
        "fund 'TESTA' also appears in funds",
    ),
    (
        "no change data with counts",
        lambda doc: doc["funds"][0].update({"status": "no_change_data"}),
        "funds[0].status",
    ),
]


@pytest.mark.parametrize(
    ("description", "mutate", "expected"),
    MUTATIONS,
    ids=[mutation[0] for mutation in MUTATIONS],
)
def test_a_mutated_document_fails_with_a_precise_message(
    document: dict, description: str, mutate: Mutator, expected: str
) -> None:
    mutated = copy.deepcopy(document)
    mutate(mutated)
    with pytest.raises(ValueError) as error:
        validate_digest(mutated)
    assert expected in str(error.value), f"{description}: {error.value}"


def test_an_internal_document_must_disclose_every_fund() -> None:
    doc = _build()
    doc["funds"][0]["securities_disclosed"] = False
    doc["funds"][0]["changes"] = []
    with pytest.raises(ValueError, match="funds\\[0\\].securities_disclosed: must be true"):
        validate_digest(doc)


def test_a_public_document_may_not_list_changes_for_a_hidden_fund() -> None:
    doc = _build(public=True)
    doc["funds"][0]["securities_disclosed"] = False
    doc["funds"][0]["changes"] = []
    validate_digest(doc)
    # The same document with a change line is a D79 violation, whatever the
    # rest of the entry says.
    doc["funds"][0]["changes"] = [
        {
            "change_type": "add",
            "security_id": "ISIN:AA0000000004",
            "security_name": "Delta Corp",
            "weight_before": 0.0,
            "weight_after": 0.1,
            "delta": 0.1,
        }
    ]
    with pytest.raises(ValueError, match="must be empty when securities_disclosed is false"):
        validate_digest(doc)


def test_the_week_must_be_seven_days() -> None:
    doc = _build()
    doc["week_start"] = (date(2026, 10, 16)).isoformat()
    with pytest.raises(
        ValueError,
        match=r"week_start: expected 2026-10-23 \(week_end minus 7 days\), got 2026-10-16",
    ):
        validate_digest(doc)
