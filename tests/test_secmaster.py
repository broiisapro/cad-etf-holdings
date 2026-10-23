"""Tests for security id resolution, overrides and fixture identity (issue #22)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from etfh.secmaster import Override, SecurityMaster, load_overrides
from etfh.secmaster.overrides import MatchType
from tests._secmaster_helpers import EXPECTED_OVERLAP, fixture_frame, holdings_frame

AS_OF = date(2026, 11, 2)
SYND_AS_OF = date(2026, 10, 29)
OVERRIDES_PATH = Path(__file__).resolve().parent.parent / "config" / "security_overrides.csv"
REVIEWED_ON = date(2026, 10, 17)


def _override(
    match_type: MatchType,
    match_value: str,
    security_id: str = "ISIN:CA600000AA15",
) -> Override:
    return Override(
        match_type=match_type,
        match_value=match_value,
        security_id=security_id,
        reviewer="Reviewer",
        reviewed_on=REVIEWED_ON,
    )


def _write_overrides(tmp_path: Path, rows: str) -> Path:
    path = tmp_path / "security_overrides.csv"
    path.write_text(
        "match_type,match_value,security_id,reviewer,reviewed_on,note\n" + rows,
        encoding="utf-8",
    )
    return path


def test_same_ticker_different_exchange_not_merged() -> None:
    master = SecurityMaster()
    syna = master.assign(fixture_frame("SYNA", as_of=AS_OF))
    synb = master.assign(fixture_frame("SYNB", as_of=AS_OF))
    assert syna.filter(pl.col("security_ticker") == "MAPL")["security_id"].to_list() == [
        "TKR:XTSE:MAPL"
    ]
    assert synb.filter(pl.col("security_ticker") == "MAPL")["security_id"].to_list() == [
        "TKR:XNYS:MAPL"
    ]

    rows = holdings_frame(
        [
            {
                "fund_ticker": "SYNA",
                "security_name": "Maple Ridge Resources Inc.",
                "security_ticker": "MAPL",
                "exchange": "XTSE",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNA",
                "security_name": "Maple Ridge Resources Inc.",
                "security_ticker": "MAPL",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
        ]
    )
    assert master.assign(rows)["security_id"].to_list() == ["TKR:XTSE:MAPL", None]


def test_assign_appends_security_id_column_and_preserves_the_frame() -> None:
    master = SecurityMaster()
    frame = fixture_frame("SYNA", as_of=AS_OF)
    assigned = master.assign(frame)
    assert assigned.columns == [*frame.columns, "security_id"]
    assert assigned.schema["security_id"] == pl.String
    assert assigned.select(frame.columns).equals(frame)


def test_assign_preserves_row_count_and_order() -> None:
    master = SecurityMaster()
    frame = fixture_frame("SYNA", as_of=AS_OF)
    forward = master.assign(frame)["security_id"].to_list()
    backward = master.assign(frame.reverse())["security_id"].to_list()
    assert len(forward) == frame.height
    assert backward == list(reversed(forward))


def test_assign_rejects_frames_missing_columns() -> None:
    master = SecurityMaster()
    with pytest.raises(ValueError, match="missing columns"):
        master.assign(pl.DataFrame({"fund_ticker": ["SYNA"]}))


def test_expected_overlap_securities_resolve_in_both_funds() -> None:
    expected = json.loads(EXPECTED_OVERLAP.read_text(encoding="utf-8"))
    as_of = date.fromisoformat(expected["asof"])
    master = SecurityMaster()
    frames = {
        ticker: master.assign(fixture_frame(ticker, as_of=as_of)) for ticker in ("SYNA", "SYNB")
    }
    assert expected["common"]
    for entry in expected["common"]:
        for ticker, weight_key in (("SYNA", "weight_a"), ("SYNB", "weight_b")):
            rows = frames[ticker].filter(pl.col("security_id") == entry["security_id"])
            assert rows.height == 1, (ticker, entry["security_id"])
            assert float(rows["weight"][0]) == pytest.approx(float(entry[weight_key]), abs=1e-9)


@pytest.mark.parametrize(
    ("ticker", "derived_isin"),
    [
        ("GRP", "US200030BA15"),
        ("IRW", "US200032BA70"),
        ("IRO", "CA600008AA41"),
        ("BRP", "CA600025AA81"),
        ("WST", "CA600022AA50"),
        ("YAR", "CA600023AA34"),
        ("KES", "CA600010AA05"),
        ("VRM", "CA600021AA77"),
    ],
)
def test_synb_cusip_only_rows_derive_isins(ticker: str, derived_isin: str) -> None:
    master = SecurityMaster()
    frame = master.assign(fixture_frame("SYNB", as_of=AS_OF))
    rows = frame.filter(pl.col("security_ticker") == ticker)
    assert rows.height == 1
    assert rows["isin"][0] is None
    assert rows["cusip"][0] is not None
    assert rows["security_id"][0] == f"ISIN:{derived_isin}"


def test_synd_duplicate_isin_and_cusip_rows_resolve_to_the_same_id() -> None:
    master = SecurityMaster()
    frame = master.assign(fixture_frame("SYND", as_of=SYND_AS_OF))
    rows = frame.filter(pl.col("security_ticker") == "NWM")
    assert rows.height == 2
    assert rows["security_id"].to_list() == ["ISIN:CA600000AA15", "ISIN:CA600000AA15"]


def test_invalid_identifiers_are_ignored_and_counted() -> None:
    master = SecurityMaster()
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNX",
                "security_name": "Acme Corp.",
                "security_ticker": "AAPL",
                "exchange": "XNAS",
                "isin": "US0378331006",
                "asset_class": "equity",
                "country": "US",
                "currency": "USD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "Beta Ltd.",
                "cusip": "03783310",
                "asset_class": "equity",
                "country": "US",
                "currency": "USD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "Gamma Inc.",
                "isin": "US0378331006",
                "cusip": "037833100",
                "asset_class": "equity",
                "country": "US",
                "currency": "USD",
            },
        ]
    )
    assigned = master.assign(frame)
    assert assigned["security_id"].to_list() == [
        "TKR:XNAS:AAPL",
        None,
        "ISIN:US0378331005",
    ]
    assert master.invalid_identifier_count == 3


def test_cash_rows_get_currency_ids() -> None:
    master = SecurityMaster()
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNA",
                "security_name": "Cash and Cash Equivalents",
                "asset_class": "cash",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNA",
                "security_name": "Cash and Cash Equivalents",
                "asset_class": "Cash",
                "currency": "usd",
            },
            {
                "fund_ticker": "SYNA",
                "security_name": "Cash and Cash Equivalents",
                "asset_class": "cash",
            },
        ]
    )
    assert master.assign(frame)["security_id"].to_list() == ["CASH:CAD", "CASH:USD", "CASH:XXX"]


def test_fx_and_derivative_rows_get_deriv_ids() -> None:
    master = SecurityMaster()
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNX",
                "security_name": "CAD/USD Forward",
                "asset_class": "fx",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "Swap: TSX 60",
                "asset_class": "derivative",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNA",
                "security_name": "CAD/USD Forward",
                "asset_class": "fx",
                "currency": "CAD",
            },
        ]
    )
    assert master.assign(frame)["security_id"].to_list() == [
        "DERIV:SYNX:CAD USD FORWARD",
        "DERIV:SYNX:SWAP TSX 60",
        "DERIV:SYNA:CAD USD FORWARD",
    ]


def test_derivative_rows_never_become_securities() -> None:
    master = SecurityMaster()
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNX",
                "security_name": "TSX 60 Index Future",
                "isin": "US0378331005",
                "security_ticker": "AAPL",
                "exchange": "XNAS",
                "asset_class": "derivative",
                "currency": "CAD",
            }
        ]
    )
    assert master.assign(frame)["security_id"].to_list() == ["DERIV:SYNX:AAPL"]


def test_cash_rows_without_currency_get_the_iso_no_currency_code() -> None:
    master = SecurityMaster()
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNA",
                "security_name": "Cash and Cash Equivalents",
                "asset_class": "cash",
            },
            {
                "fund_ticker": "SYNA",
                "security_name": "Cash",
                "asset_class": "Cash",
                "currency": "   ",
            },
        ]
    )
    assert master.assign(frame)["security_id"].to_list() == ["CASH:XXX", "CASH:XXX"]


def test_derivative_ids_prefer_the_instrument_ticker_over_the_name() -> None:
    master = SecurityMaster()
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNX",
                "security_name": "Northwind Materials Inc Call Option",
                "security_ticker": "NWMX 255 12/18/26",
                "asset_class": "derivative",
                "currency": "USD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "Northwind Materials Inc Call Option",
                "security_ticker": "NWMX 260 12/18/26",
                "asset_class": "derivative",
                "currency": "USD",
            },
            {
                "fund_ticker": "SYNX",
                "security_name": "Northwind Materials Inc Call Option",
                "asset_class": "derivative",
                "currency": "USD",
            },
        ]
    )
    assert master.assign(frame)["security_id"].to_list() == [
        "DERIV:SYNX:NWMX 255 12/18/26",
        "DERIV:SYNX:NWMX 260 12/18/26",
        "DERIV:SYNX:NORTHWIND MATERIALS INC CALL OPTION",
    ]


def test_two_same_name_option_rows_without_tickers_get_unique_ids() -> None:
    master = SecurityMaster()
    frame = holdings_frame(
        [
            {
                "fund_ticker": "ESPX",
                "security_name": "Northwind Materials Inc Call Option",
                "asset_class": "derivative",
                "currency": "USD",
            },
            {
                "fund_ticker": "ESPX",
                "security_name": "Northwind Materials Inc Call Option",
                "asset_class": "derivative",
                "currency": "USD",
            },
        ]
    )
    assert master.assign(frame)["security_id"].to_list() == [
        "DERIV:ESPX:NORTHWIND MATERIALS INC CALL OPTION",
        "DERIV:ESPX:NORTHWIND MATERIALS INC CALL OPTION#2",
    ]


def test_derivative_collision_suffixes_follow_file_order() -> None:
    master = SecurityMaster()

    def row(name: str) -> dict[str, object]:
        return {
            "fund_ticker": "ESPX",
            "security_name": name,
            "asset_class": "derivative",
            "currency": "USD",
        }

    frame = holdings_frame(
        [
            row("Northwind Materials Inc Call Option"),
            row("Foxglove Networks Inc Put Option"),
            row("Northwind Materials Inc Call Option"),
        ]
    )
    assert master.assign(frame)["security_id"].to_list() == [
        "DERIV:ESPX:NORTHWIND MATERIALS INC CALL OPTION",
        "DERIV:ESPX:FOXGLOVE NETWORKS INC PUT OPTION",
        "DERIV:ESPX:NORTHWIND MATERIALS INC CALL OPTION#2",
    ]


def test_derivative_collisions_are_scoped_to_one_file() -> None:
    master = SecurityMaster()
    rows = [
        {
            "fund_ticker": "ESPX",
            "security_name": "Northwind Materials Inc Call Option",
            "asset_class": "derivative",
            "currency": "USD",
        }
    ]
    first = master.assign(holdings_frame(rows))["security_id"].to_list()
    second = master.assign(holdings_frame(rows))["security_id"].to_list()
    assert first == ["DERIV:ESPX:NORTHWIND MATERIALS INC CALL OPTION"]
    assert second == first


def test_fx_and_derivative_ids_are_scoped_per_fund() -> None:
    master = SecurityMaster()
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNX",
                "security_name": "CAD/USD Forward",
                "security_ticker": "CADUSD",
                "asset_class": "fx",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNA",
                "security_name": "CAD/USD Forward",
                "security_ticker": "CADUSD",
                "asset_class": "fx",
                "currency": "CAD",
            },
        ]
    )
    assert master.assign(frame)["security_id"].to_list() == [
        "DERIV:SYNX:CADUSD",
        "DERIV:SYNA:CADUSD",
    ]


def test_committed_overrides_file_is_loadable_and_reviewed() -> None:
    overrides = load_overrides(OVERRIDES_PATH)
    data_rows = sum(
        1 for line in OVERRIDES_PATH.read_text(encoding="utf-8").splitlines()[1:] if line.strip()
    )
    assert len(overrides) == data_rows
    assert all(override.reviewer.strip() and override.reviewed_on for override in overrides)


def test_load_overrides_parses_every_match_type(tmp_path: Path) -> None:
    path = _write_overrides(
        tmp_path,
        "isin,US0378331005,ISIN:CA600000AA15,Reviewer,2026-10-17,\n"
        "cusip,037833100,ISIN:CA600000AA15,Reviewer,2026-10-17,\n"
        "ticker,XTSE:ry.to,ISIN:CA600000AA15,Reviewer,2026-10-17,\n"
        "name,SYNA|Northwind Materials Inc.,ISIN:CA600000AA15,Reviewer,2026-10-17,note\n",
    )
    overrides = load_overrides(path)
    assert [override.match_type for override in overrides] == ["isin", "cusip", "ticker", "name"]
    assert overrides[2].match_value == "XTSE:RY"
    assert overrides[3].match_value == "SYNA|Northwind Materials Inc."
    assert overrides[0].reviewer == "Reviewer"
    assert overrides[0].reviewed_on == REVIEWED_ON


@pytest.mark.parametrize(
    "row",
    [
        "isin,US0378331005,ISIN:CA600000AA15,,2026-10-17,",
        "isin,US0378331005,ISIN:CA600000AA15,Reviewer,,",
        "isin,US0378331006,ISIN:CA600000AA15,Reviewer,2026-10-17,",
        "cusip,600000AA,ISIN:CA600000AA15,Reviewer,2026-10-17,",
        "ticker,RY,ISIN:CA600000AA15,Reviewer,2026-10-17,",
        "ticker,XTSE:,ISIN:CA600000AA15,Reviewer,2026-10-17,",
        "name,Northwind Materials Inc.,ISIN:CA600000AA15,Reviewer,2026-10-17,",
        "bogus,US0378331005,ISIN:CA600000AA15,Reviewer,2026-10-17,",
    ],
)
def test_load_overrides_rejects_invalid_rows(tmp_path: Path, row: str) -> None:
    with pytest.raises(ValueError, match="invalid override"):
        load_overrides(_write_overrides(tmp_path, row))


def test_load_overrides_rejects_wrong_columns(tmp_path: Path) -> None:
    path = tmp_path / "security_overrides.csv"
    path.write_text("match_type,match_value,security_id,reviewer\n", encoding="utf-8")
    with pytest.raises(ValueError, match="columns"):
        load_overrides(path)


def test_load_overrides_rejects_duplicate_match_keys(tmp_path: Path) -> None:
    rows = (
        "isin,US0378331005,ISIN:CA600000AA15,Reviewer,2026-10-17,\n"
        "isin,US0378331005,CUSIP:037833100,Reviewer,2026-10-17,\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_overrides(_write_overrides(tmp_path, rows))


def test_load_overrides_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_overrides(tmp_path / "absent.csv")


def test_isin_override_takes_precedence_over_provided_isin() -> None:
    master = SecurityMaster([_override("isin", "US0378331005", "CUSIP:037833100")])
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNX",
                "security_name": "Apple Inc.",
                "security_ticker": "AAPL",
                "exchange": "XNAS",
                "isin": "US0378331005",
                "asset_class": "equity",
                "country": "US",
                "currency": "USD",
            }
        ]
    )
    assert master.assign(frame)["security_id"].to_list() == ["CUSIP:037833100"]


def test_cusip_override_beats_structural_derivation() -> None:
    master = SecurityMaster([_override("cusip", "600000AA1", "CUSIP:600000AA1")])
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYND",
                "security_name": "Northwind Materials Inc.",
                "security_ticker": "NWM",
                "exchange": "XTSE",
                "cusip": "600000AA1",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            }
        ]
    )
    assert master.assign(frame)["security_id"].to_list() == ["CUSIP:600000AA1"]


def test_ticker_override_requires_the_same_exchange() -> None:
    master = SecurityMaster([_override("ticker", "XTSE:MAPL", "ISIN:CA600000AA15")])
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNA",
                "security_name": "Maple Ridge Resources Inc.",
                "security_ticker": "MAPL",
                "exchange": "XTSE",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
            {
                "fund_ticker": "SYNB",
                "security_name": "Maple Ridge Systems Corp.",
                "security_ticker": "MAPL",
                "exchange": "XNYS",
                "asset_class": "equity",
                "country": "US",
                "currency": "USD",
            },
        ]
    )
    assert master.assign(frame)["security_id"].to_list() == ["ISIN:CA600000AA15", "TKR:XNYS:MAPL"]


def test_name_override_matches_exact_name_and_fund() -> None:
    master = SecurityMaster([_override("name", "SYND|Northwind Materials Inc.")])
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYND",
                "security_name": "Northwind Materials Inc.",
                "security_ticker": "NWM",
                "exchange": "XTSE",
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
                "fund_ticker": "SYND",
                "security_name": "northwind materials inc.",
                "security_ticker": "NWM",
                "exchange": "XTSE",
                "asset_class": "equity",
                "country": "CA",
                "currency": "CAD",
            },
        ]
    )
    assert master.assign(frame)["security_id"].to_list() == [
        "ISIN:CA600000AA15",
        "TKR:XTSE:NWM",
        "TKR:XTSE:NWM",
    ]


def test_override_takes_precedence_over_cash_classification() -> None:
    master = SecurityMaster([_override("name", "SYNA|Cash and Cash Equivalents", "CASH:CAD")])
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNA",
                "security_name": "Cash and Cash Equivalents",
                "asset_class": "cash",
                "currency": "USD",
            }
        ]
    )
    assert master.assign(frame)["security_id"].to_list() == ["CASH:CAD"]


def test_override_leaves_other_rows_unchanged() -> None:
    frame = holdings_frame(
        [
            {
                "fund_ticker": "SYNA",
                "security_name": "Maple Ridge Resources Inc.",
                "security_ticker": "MAPL",
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
                "country": "US",
                "currency": "USD",
            },
        ]
    )
    plain = SecurityMaster().assign(frame)["security_id"].to_list()
    overridden = (
        SecurityMaster([_override("ticker", "XTSE:MAPL")]).assign(frame)["security_id"].to_list()
    )
    assert overridden[0] == "ISIN:CA600000AA15"
    assert overridden[1] == plain[1]
