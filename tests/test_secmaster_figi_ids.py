"""SecurityMaster FIGI-id assignment (issue #74, D47).

All tests are offline: an ``httpx.MockTransport`` answers the mapping API and a
fake clock keeps the rate limiter from sleeping.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import polars as pl
import pytest

from etfh.build import build
from etfh.secmaster import FigiLookup, Override, SecurityMaster
from tests._secmaster_helpers import RAW_SYNTHETIC, holdings_frame
from tests.conftest import FakeClock

JobKey = tuple[str, str, str | None, str | None]


class _Api:
    def __init__(self, answers: dict[JobKey, str | None]) -> None:
        self.requests: list[list[dict[str, str]]] = []
        self.transport = httpx.MockTransport(self._handle)
        self._answers = answers

    def _handle(self, request: httpx.Request) -> httpx.Response:
        jobs = json.loads(request.content)
        self.requests.append(jobs)
        entries = []
        for job in jobs:
            key = (
                job["idType"],
                job["idValue"],
                job.get("micCode"),
                job.get("exchCode"),
            )
            composite = self._answers.get(key)
            if composite is None:
                entries.append({"warning": "No identifier found."})
            else:
                entries.append({"data": [{"compositeFIGI": composite}]})
        return httpx.Response(200, json=entries)


def _lookup(
    tmp_path: Path,
    answers: dict[JobKey, str | None],
    fake_clock: FakeClock,
) -> tuple[FigiLookup, _Api]:
    api = _Api(answers)
    lookup = FigiLookup(
        tmp_path,
        transport=api.transport,
        clock=fake_clock.time,
        sleep=fake_clock.sleep,
    )
    return lookup, api


_ANSWERS: dict[JobKey, str | None] = {
    ("ID_ISIN", "CA600000AA15", None, None): "BBG000AAA111",
    ("TICKER", "NWM", "XTSE", None): "BBG000AAA111",
    ("TICKER", "FXN", None, "US"): "BBG000BBB222",
    ("ID_ISIN", "CA600008AA41", None, None): "BBG000CCC333",
}


def _frame() -> pl.DataFrame:
    return holdings_frame(
        [
            {
                "fund_ticker": "SYNA",
                "security_name": "Northwind Materials Inc.",
                "security_ticker": "NWM",
                "exchange": "XTSE",
                "isin": "CA600000AA15",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNA",
                "security_name": "Northwind Materials Inc.",
                "security_ticker": "NWM",
                "exchange": "XTSE",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNA",
                "security_name": "Foxglove Networks Inc.",
                "security_ticker": "FXN",
                "exchange": "US",
                "asset_class": "equity",
                "country": "US",
                "currency": "USD",
            },
            {
                "fund_ticker": "SYNA",
                "security_name": "Cedarline Energy Corp.",
                "exchange": "XTSE",
                "cusip": "600008AA4",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNA",
                "security_name": "Cash and Cash Equivalents",
                "asset_class": "cash",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNA",
                "security_name": "Northwind Materials Inc Call Option",
                "security_ticker": "NWMX",
                "asset_class": "derivative",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNA",
                "security_name": "Unmatched Holdings Inc.",
                "asset_class": "equity",
                "currency": "CAD",
            },
        ]
    )


def test_default_figi_none_ids_are_unchanged() -> None:
    assert SecurityMaster().assign(_frame())["security_id"].to_list() == [
        "ISIN:CA600000AA15",
        "TKR:XTSE:NWM",
        None,
        "ISIN:CA600008AA41",
        "CASH:CAD",
        "DERIV:SYNA:NWMX",
        None,
    ]


def test_prefetch_batches_unique_keys_and_assign_uses_the_cache(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    lookup, api = _lookup(tmp_path, _ANSWERS, fake_clock)
    master = SecurityMaster(figi=lookup)
    master.prefetch([_frame()])
    assert len(api.requests) == 1
    assert [len(request) for request in api.requests] == [4]
    request_keys = {
        (job["idType"], job["idValue"], job.get("micCode"), job.get("exchCode"))
        for job in api.requests[0]
    }
    assert request_keys == set(_ANSWERS)
    assert all(
        job["marketSecDes"] == "Equity" for job in api.requests[0] if job["idType"] == "TICKER"
    )

    assigned = master.assign(_frame())
    assert assigned["security_id"].to_list() == [
        "FIGI:BBG000AAA111",
        "FIGI:BBG000AAA111",
        "FIGI:BBG000BBB222",
        "FIGI:BBG000CCC333",
        "CASH:CAD",
        "DERIV:SYNA:NWMX",
        None,
    ]
    assert len(api.requests) == 1
    assert master.figi_assigned_count == 4
    assert master.figi_unmapped_count == 0
    assert master.figi_ambiguous_count == 0


def test_assign_without_prefetch_looks_up_lazily(tmp_path: Path, fake_clock: FakeClock) -> None:
    lookup, api = _lookup(tmp_path, _ANSWERS, fake_clock)
    SecurityMaster(figi=lookup).assign(_frame())
    assert len(api.requests) == 1
    assert len(api.requests[0]) == 4


def test_isin_wins_over_ticker_and_cusip_prefers_the_derived_isin(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    lookup, api = _lookup(tmp_path, _ANSWERS, fake_clock)
    master = SecurityMaster(figi=lookup)
    master.prefetch([_frame()])
    keys = {
        (job["idType"], job["idValue"], job.get("micCode"), job.get("exchCode"))
        for job in api.requests[0]
    }
    assert ("ID_ISIN", "CA600000AA15", None, None) in keys
    assert ("ID_ISIN", "CA600008AA41", None, None) in keys
    assert ("TICKER", "NWM", "XTSE", None) in keys
    assert ("TICKER", "FXN", None, "US") in keys


def test_prefetch_splits_large_frames_into_batches(tmp_path: Path, fake_clock: FakeClock) -> None:
    rows = [
        {
            "fund_ticker": "SYNZ",
            "security_name": f"Ticker Only {index}",
            "security_ticker": f"T{index:02d}",
            "exchange": "XTSE",
            "asset_class": "equity",
            "country": "CA",
            "currency": "CAD",
        }
        for index in range(12)
    ]
    lookup, api = _lookup(tmp_path, {}, fake_clock)
    SecurityMaster(figi=lookup).prefetch([holdings_frame(rows)])
    assert [len(request) for request in api.requests] == [10, 2]


def test_override_isin_is_looked_up_and_expressed_as_figi(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """D55: an id that came from a reviewed override joins the FIGI id space."""
    override = Override(
        match_type="ticker",
        match_value="XTSE:OVR",
        security_id="ISIN:CA600000AA99",
        reviewer="Reviewer",
        reviewed_on=date(2026, 10, 17),
    )
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNA",
                "security_name": "Override Holdings Inc.",
                "security_ticker": "OVR",
                "exchange": "XTSE",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            }
        ]
    )
    lookup, api = _lookup(
        tmp_path, {("ID_ISIN", "CA600000AA99", None, None): "BBG000OVR999"}, fake_clock
    )
    master = SecurityMaster([override], figi=lookup)
    master.prefetch([frame])
    assert [(job["idType"], job["idValue"]) for request in api.requests for job in request] == [
        ("ID_ISIN", "CA600000AA99")
    ]
    assert master.assign(frame)["security_id"].to_list() == ["FIGI:BBG000OVR999"]


def test_override_ticker_id_is_looked_up_as_a_ticker(tmp_path: Path, fake_clock: FakeClock) -> None:
    override = Override(
        match_type="name",
        match_value="SYNA|Override Holdings Inc.",
        security_id="TKR:XTSE:OVR",
        reviewer="Reviewer",
        reviewed_on=date(2026, 10, 17),
    )
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNA",
                "security_name": "Override Holdings Inc.",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            }
        ]
    )
    lookup, _ = _lookup(tmp_path, {("TICKER", "OVR", "XTSE", None): "BBG000OVR999"}, fake_clock)
    master = SecurityMaster([override], figi=lookup)
    assert master.assign(frame)["security_id"].to_list() == ["FIGI:BBG000OVR999"]


def test_override_to_a_non_identifier_id_is_kept_without_a_lookup(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    override = Override(
        match_type="name",
        match_value="SYNA|Override Holdings Inc.",
        security_id="CUSIP:037833100",
        reviewer="Reviewer",
        reviewed_on=date(2026, 10, 17),
    )
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNA",
                "security_name": "Override Holdings Inc.",
                "security_ticker": "OVR",
                "exchange": "XTSE",
                "asset_class": "equity",
                "country": "US",
                "currency": "USD",
            }
        ]
    )
    lookup, api = _lookup(tmp_path, {("TICKER", "OVR", "XTSE", None): "BBG000OVR999"}, fake_clock)
    master = SecurityMaster([override], figi=lookup)
    master.prefetch([frame])
    assert api.requests == []
    assert master.assign(frame)["security_id"].to_list() == ["CUSIP:037833100"]


def test_ambiguous_ticker_mapping_keeps_the_d23_id_and_is_counted(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "data": [
                        {"compositeFIGI": "BBG000AAA111"},
                        {"compositeFIGI": "BBG000ZZZ999"},
                    ]
                }
            ],
        )

    lookup = FigiLookup(
        tmp_path,
        transport=httpx.MockTransport(responder),
        clock=fake_clock.time,
        sleep=fake_clock.sleep,
    )
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNA",
                "security_name": "Northwind Materials Inc.",
                "security_ticker": "NWM",
                "exchange": "XTSE",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            }
        ]
    )
    master = SecurityMaster(figi=lookup)
    assert master.assign(frame)["security_id"].to_list() == ["TKR:XTSE:NWM"]
    assert master.figi_assigned_count == 0
    assert master.figi_ambiguous_count == 1
    assert lookup.ambiguous_count == 1


def test_isin_prefers_the_home_market_composite(tmp_path: Path, fake_clock: FakeClock) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "data": [
                        {"exchCode": "US", "compositeFIGI": "BBG000US0001"},
                        {"exchCode": "CN", "compositeFIGI": "BBG000CA0001"},
                        {"exchCode": "CT", "compositeFIGI": "BBG000CA0001"},
                    ]
                }
            ],
        )

    lookup = FigiLookup(
        tmp_path,
        transport=httpx.MockTransport(responder),
        clock=fake_clock.time,
        sleep=fake_clock.sleep,
    )
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNA",
                "security_name": "Northwind Materials Inc.",
                "isin": "CA600000AA15",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            }
        ]
    )
    assert SecurityMaster(figi=lookup).assign(frame)["security_id"].to_list() == [
        "FIGI:BBG000CA0001"
    ]
    assert lookup.ambiguous_count == 0


def test_isin_falls_back_to_the_first_listing_without_a_home_match(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "data": [
                        {"exchCode": "FP", "compositeFIGI": "BBG000FR0001"},
                        {"exchCode": "GR", "compositeFIGI": "BBG000DE0001"},
                    ]
                }
            ],
        )

    lookup = FigiLookup(
        tmp_path,
        transport=httpx.MockTransport(responder),
        clock=fake_clock.time,
        sleep=fake_clock.sleep,
    )
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNA",
                "security_name": "Northwind Materials Inc.",
                "isin": "NL0000235190",
                "asset_class": "equity",
                "country": "NL",
                "currency": "EUR",
            }
        ]
    )
    assert SecurityMaster(figi=lookup).assign(frame)["security_id"].to_list() == [
        "FIGI:BBG000FR0001"
    ]
    assert lookup.ambiguous_count == 0


def test_unmapped_rows_keep_their_d23_ids_and_are_counted(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
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
                "fund_ticker": "SYNA",
                "security_name": "Foxglove Networks Inc.",
                "security_ticker": "FXN",
                "exchange": "XTSE",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNA",
                "security_name": "Glenora Utilities Inc.",
                "security_ticker": "GNU",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
        ]
    )
    lookup, _ = _lookup(tmp_path, {}, fake_clock)
    master = SecurityMaster(figi=lookup)
    assert master.assign(frame)["security_id"].to_list() == [
        "ISIN:CA600000AA15",
        "TKR:XTSE:FXN",
        None,
    ]
    assert master.figi_assigned_count == 0
    assert master.figi_unmapped_count == 2
    assert master.figi_ambiguous_count == 0


def test_prefetch_without_figi_is_a_noop() -> None:
    SecurityMaster().prefetch([_frame()])


def test_prefetch_rejects_frames_missing_columns(tmp_path: Path, fake_clock: FakeClock) -> None:
    lookup, _ = _lookup(tmp_path, {}, fake_clock)
    with pytest.raises(ValueError, match="missing columns"):
        SecurityMaster(figi=lookup).prefetch([pl.DataFrame({"fund_ticker": ["SYNA"]})])


@pytest.mark.network
def test_live_openfigi_maps_canadian_isins_and_tsx_tickers(tmp_path: Path) -> None:
    """Smoke test against the real API (excluded from CI, D10).

    Three well-known Canadian ISINs and their TSX tickers must resolve to the
    same composite FIGI; the TSX ticker lookup goes through ``micCode=XTSE``.
    """
    lookup = FigiLookup(tmp_path / "openfigi-cache")
    pairs = (
        ("CA7800871021", "RY"),
        ("CA8911605092", "TD"),
        ("CA29250N1050", "ENB"),
    )
    for isin, ticker in pairs:
        by_isin = lookup.composite_figi("ID_ISIN", isin)
        by_ticker = lookup.composite_figi("TICKER", ticker, "XTSE")
        assert by_isin is not None, (isin, ticker)
        assert by_ticker == by_isin, (isin, ticker, by_isin, by_ticker)


def test_composite_code_ticker_row_joins_the_figi_id_space(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """A Parser sets ``"AAPL US"`` with a null exchange; FIGI still maps it (D57)."""
    frame = holdings_frame(
        [
            {
                "fund_ticker": "ESPX",
                "security_name": "Apple Inc.",
                "security_ticker": "AAPL US",
                "asset_class": "equity",
                "country": "US",
                "currency": "USD",
            },
            {
                "fund_ticker": "ESPX",
                "security_name": "Apple Inc.",
                "isin": "US0378331005",
                "asset_class": "equity",
                "country": "US",
                "currency": "USD",
            },
        ]
    )
    lookup, api = _lookup(
        tmp_path,
        {
            ("TICKER", "AAPL", None, "US"): "BBG000B9XRY4",
            ("ID_ISIN", "US0378331005", None, None): "BBG000B9XRY4",
        },
        fake_clock,
    )
    master = SecurityMaster(figi=lookup)
    master.prefetch([frame])
    request_keys = {
        (job["idType"], job["idValue"], job.get("micCode"), job.get("exchCode"))
        for job in api.requests[0]
    }
    assert request_keys == {
        ("TICKER", "AAPL", None, "US"),
        ("ID_ISIN", "US0378331005", None, None),
    }
    assert master.assign(frame)["security_id"].to_list() == [
        "FIGI:BBG000B9XRY4",
        "FIGI:BBG000B9XRY4",
    ]


def test_single_exchange_suffix_is_looked_up_by_mic_not_by_composite_code(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    frame = holdings_frame(
        [
            {
                "fund_ticker": "ESPX",
                "security_name": "Northwind Materials Inc.",
                "security_ticker": "NWM CT",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            }
        ]
    )
    lookup, api = _lookup(tmp_path, {("TICKER", "NWM", "XTSE", None): "BBG000AAA111"}, fake_clock)
    assert SecurityMaster(figi=lookup).assign(frame)["security_id"].to_list() == [
        "FIGI:BBG000AAA111"
    ]
    assert api.requests == [
        [{"idType": "TICKER", "idValue": "NWM", "marketSecDes": "Equity", "micCode": "XTSE"}]
    ]


def test_figi_keeps_cash_derivative_and_cusip_ids(tmp_path: Path, fake_clock: FakeClock) -> None:
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNX",
                "security_name": "Cash",
                "asset_class": "cash",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "Northwind Call",
                "security_ticker": "NWMX 255",
                "asset_class": "derivative",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "Foreign CUSIP Ltd.",
                "cusip": "037833100",
                "security_ticker": "AAPL",
                "exchange": "XNAS",
                "asset_class": "equity",
                "country": "GB",
                "currency": "GBP",
            },
        ]
    )
    lookup, api = _lookup(
        tmp_path,
        {
            ("TICKER", "AAPL", "XNAS", None): "BBG000B9XRY4",
            ("ID_CUSIP", "037833100", None, None): "BBG000B9XRY4",
        },
        fake_clock,
    )
    assert SecurityMaster(figi=lookup).assign(frame)["security_id"].to_list() == [
        "CASH:CAD",
        "DERIV:SYNX:NWMX 255",
        "CUSIP:037833100",
    ]
    assert api.requests == []


def test_figi_none_leaves_composite_rows_unmatched() -> None:
    frame = holdings_frame(
        [
            {
                "fund_ticker": "ESPX",
                "security_name": "Apple Inc.",
                "security_ticker": "AAPL US",
                "asset_class": "equity",
                "country": "US",
                "currency": "USD",
            }
        ]
    )
    assert SecurityMaster().assign(frame)["security_id"].to_list() == [None]


@pytest.mark.network
def test_live_openfigi_maps_three_us_tickers_and_their_isins_to_one_id(tmp_path: Path) -> None:
    """Smoke test against the real API (excluded from CI, D10).

    Three US-listed tickers published with Bloomberg's country-composite
    ``US`` code and their ISINs must resolve to the same composite FIGI, so an
    issuer that only publishes ``"AAPL US"`` overlaps with one that publishes
    ISINs (D55, D57).
    """
    lookup = FigiLookup(tmp_path / "openfigi-cache")
    pairs = (
        ("AAPL", "US0378331005"),
        ("MSFT", "US5949181045"),
        ("JNJ", "US4781601046"),
    )
    for ticker, isin in pairs:
        by_ticker = lookup.composite_figi("TICKER", ticker, exch_code="US")
        by_isin = lookup.composite_figi("ID_ISIN", isin)
        assert by_ticker is not None, (ticker, isin)
        assert by_isin == by_ticker, (ticker, isin, by_isin, by_ticker)


def test_build_with_an_explicit_figi_none_master_matches_the_default_build(
    tmp_path: Path,
) -> None:
    """Passing ``SecurityMaster()`` must not change the build by one byte."""
    default_out = tmp_path / "default"
    explicit_out = tmp_path / "explicit"
    build(RAW_SYNTHETIC, default_out)
    build(RAW_SYNTHETIC, explicit_out, security_master=SecurityMaster())
    for name in ("holdings", "file_versions", "qa_events", "holdings_changes"):
        default_bytes = (default_out / "parquet" / f"{name}.parquet").read_bytes()
        explicit_bytes = (explicit_out / "parquet" / f"{name}.parquet").read_bytes()
        assert default_bytes == explicit_bytes, name
