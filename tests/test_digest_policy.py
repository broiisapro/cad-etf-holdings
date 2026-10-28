"""Publication policy (D79) applied while building the digest."""

from __future__ import annotations

import json
from datetime import date

import pytest

from etfh.digest import build_digest, validate_digest
from etfh.digest.policy import securities_disclosed
from tests._digest_helpers import (
    DAY,
    WEEK_START,
    fund,
    holding,
    make_con,
    source,
    sources_registry,
)

SYNTHETIC = "syn_daily"
BROKER = "broker_daily"
OPEN = "open_daily"

REGISTRY = sources_registry(
    source(SYNTHETIC, issuer="synthetic"),
    source(BROKER, issuer="bmo", redistribution=False),
    source(OPEN, issuer="open_issuer", redistribution=True),
)

SECRET_ID = "ISIN:RE0000000001"
SECRET_NAME = "Confidential Holdings Incorporated"
OPEN_ID = "ISIN:OP0000000001"
OPEN_NAME = "Open Holdings Limited"
SYNTH_ID = "ISIN:SY0000000001"
SYNTH_NAME = "Synthetic Aurora Holdings"


def _rows(fund_ticker: str, security_id: str, name: str, source_id: str) -> list[dict]:
    return [
        holding(fund_ticker, security_id, 0.80, as_of=WEEK_START, source_id=source_id, name=name),
        holding(fund_ticker, security_id, 0.40, as_of=DAY, source_id=source_id, name=name),
        holding(
            fund_ticker,
            "ISIN:XX0000000002",
            0.20,
            as_of=WEEK_START,
            source_id=source_id,
            name=f"{name} Two",
        ),
        holding(
            fund_ticker,
            "ISIN:XX0000000002",
            0.60,
            as_of=DAY,
            source_id=source_id,
            name=f"{name} Two",
        ),
    ]


def _funds() -> list:
    return [
        fund("SYNTH", issuer="synthetic", source_ids=(SYNTHETIC,)),
        fund("REAL", issuer="bmo", source_ids=(BROKER,), name="Broker Real Fund"),
    ]


def _store() -> list[dict]:
    return [
        *_rows("SYNTH", SYNTH_ID, SYNTH_NAME, SYNTHETIC),
        *_rows("REAL", SECRET_ID, SECRET_NAME, BROKER),
    ]


def test_public_document_keeps_real_security_lines_out_of_the_whole_document() -> None:
    doc = build_digest(make_con(_store()), DAY, public=True, sources=REGISTRY, funds=_funds())
    validate_digest(doc)
    serialized = json.dumps(doc, sort_keys=True)

    assert SECRET_NAME not in serialized
    assert SECRET_ID not in serialized
    assert SYNTH_NAME in serialized
    assert SYNTH_ID in serialized

    entries = {entry["ticker"]: entry for entry in doc["funds"]}
    real = entries["REAL"]
    assert real["securities_disclosed"] is False
    assert real["changes"] == []
    assert real["counts"] == {"adds": 0, "drops": 0, "weight_changes": 2}
    assert real["turnover"] == pytest.approx(0.4, abs=1e-9)
    assert real["top10_weight_start"] == pytest.approx(1.0, abs=1e-9)

    synth = entries["SYNTH"]
    assert synth["securities_disclosed"] is True
    assert len(synth["changes"]) == 2


def test_internal_document_discloses_every_fund() -> None:
    doc = build_digest(make_con(_store()), DAY, public=False, sources=REGISTRY, funds=_funds())
    validate_digest(doc)
    assert SECRET_NAME in json.dumps(doc, sort_keys=True)
    entries = {entry["ticker"]: entry for entry in doc["funds"]}
    assert entries["REAL"]["securities_disclosed"] is True
    assert len(entries["REAL"]["changes"]) == 2


def test_a_source_that_permits_redistribution_discloses_a_real_fund() -> None:
    rows = _rows("OPEN", OPEN_ID, OPEN_NAME, OPEN)
    funds = [fund("OPEN", issuer="open_issuer", source_ids=(OPEN,))]
    doc = build_digest(make_con(rows), DAY, public=True, sources=REGISTRY, funds=funds)
    validate_digest(doc)
    assert OPEN_NAME in json.dumps(doc, sort_keys=True)
    assert doc["funds"][0]["securities_disclosed"] is True


def test_every_source_must_permit_redistribution() -> None:
    rows = _rows("MIXED", SECRET_ID, SECRET_NAME, BROKER)
    funds = [fund("MIXED", issuer="bmo", source_ids=(OPEN, BROKER))]
    doc = build_digest(make_con(rows), DAY, public=True, sources=REGISTRY, funds=funds)
    assert doc["funds"][0]["securities_disclosed"] is False
    assert doc["funds"][0]["changes"] == []


def test_a_source_missing_from_the_registry_fails_closed() -> None:
    rows = _rows("LOST", SECRET_ID, SECRET_NAME, BROKER)
    funds = [fund("LOST", issuer="bmo", source_ids=("not_in_registry",))]
    doc = build_digest(make_con(rows), DAY, public=True, sources=REGISTRY, funds=funds)
    assert doc["funds"][0]["securities_disclosed"] is False


def test_policy_helper_prefers_internal_and_synthetic_funds() -> None:
    synthetic = fund("SYNTH", issuer="synthetic", source_ids=(BROKER,))
    covered = fund("OPEN", issuer="open_issuer", source_ids=(OPEN,))
    broker = fund("REAL", issuer="bmo", source_ids=(BROKER,))

    assert securities_disclosed(synthetic, REGISTRY, public=True) is True
    assert securities_disclosed(covered, REGISTRY, public=True) is True
    assert securities_disclosed(broker, REGISTRY, public=True) is False
    assert securities_disclosed(broker, REGISTRY, public=False) is True


def test_a_public_document_is_not_marked_internal() -> None:
    doc = build_digest(make_con(_store()), DAY, public=True, sources=REGISTRY, funds=_funds())
    assert doc["public"] is True
    assert doc["demo"] is False
    internal = build_digest(make_con(_store()), DAY, public=False, sources=REGISTRY, funds=_funds())
    assert internal["public"] is False
    assert internal["week_start"] == date(2026, 10, 23).isoformat()
