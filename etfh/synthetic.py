"""Deterministic synthetic issuer-format fixtures.

The generator writes a raw store (files plus sidecars through
:class:`etfh.storage.RawStore`) containing small issuer-like CSV, XLSX, JSON
and PDF holdings files for the seven synthetic funds, plus
``manifest.json`` and ``expected_overlap.json`` next to the store root.

Everything is byte-for-byte deterministic: no wall clock is read, the weight
arithmetic uses exact decimals (basis points), XLSX archives are rewritten
with fixed entry timestamps and fixed core properties, and the PDF is produced
with ReportLab's ``invariant`` mode.

Run it with::

    python -m etfh.synthetic --out tests/fixtures/raw_synthetic
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from pydantic import BaseModel, ConfigDict
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from etfh.storage import RawStore
from etfh.types import FileFormat, RawFile
from etfh.units import WeightUnit

__all__ = [
    "Manifest",
    "Security",
    "generate",
    "ground_truth_holdings",
    "main",
]

_BP_TOTAL = 10_000
_CASH_BP = 150
_MIN_CASH_BP = 25
_SEED = 20_261_016
_TORONTO = ZoneInfo("America/Toronto")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_CORE_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
    'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:dcterms="http://purl.org/dc/terms/" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    "<dc:creator>etfh synthetic fixtures</dc:creator>"
    "<cp:lastModifiedBy>etfh synthetic fixtures</cp:lastModifiedBy>"
    '<dcterms:created xsi:type="dcterms:W3CDTF">2026-10-16T00:00:00Z</dcterms:created>'
    '<dcterms:modified xsi:type="dcterms:W3CDTF">2026-10-16T00:00:00Z</dcterms:modified>'
    "<dc:title>Synthetic holdings</dc:title>"
    "</cp:coreProperties>"
)


class _Rng:
    """Tiny deterministic linear congruential generator (version-proof)."""

    def __init__(self, seed: int) -> None:
        self._state = seed & 0x7FFFFFFF

    def next(self) -> int:
        self._state = (1103515245 * self._state + 12345) & 0x7FFFFFFF
        return self._state

    def randint(self, low: int, high: int) -> int:
        return low + self.next() % (high - low + 1)


def _cusip_check_digit(base8: str) -> str:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ*@#"
    total = 0
    for index, character in enumerate(base8):
        value = alphabet.index(character)
        if index % 2 == 1:
            value *= 2
        total += value // 10 + value % 10
    return str((10 - total % 10) % 10)


def _cusip(base8: str) -> str:
    return base8 + _cusip_check_digit(base8)


def _isin_check_digit(payload: str) -> str:
    digits = "".join(str(int(character, 36)) for character in payload.upper())
    total = 0
    for index, character in enumerate(reversed(digits)):
        value = int(character)
        if index % 2 == 0:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return str((10 - total % 10) % 10)


def _isin(country: str, cusip9: str) -> str:
    payload = country + cusip9
    return payload + _isin_check_digit(payload)


@dataclass(frozen=True)
class Security:
    """Ground-truth identity of one synthetic security."""

    ticker: str
    name: str
    mic: str
    country: str
    currency: str
    sector: str
    cusip: str | None
    isin: str | None
    asset_class: str = "equity"

    @property
    def security_id(self) -> str:
        if self.asset_class == "cash":
            return f"CASH:{self.currency}"
        if self.isin:
            return f"ISIN:{self.isin}"
        if self.cusip:
            return f"CUSIP:{self.cusip}"
        return f"TKR:{self.mic}:{self.ticker}"


EmitIdentifiers = Literal["isin", "cusip", "ticker", "none"]


@dataclass(frozen=True)
class Holding:
    """One holding row of one file version."""

    security: Security
    weight_bp: int
    emit: EmitIdentifiers
    shares: Decimal
    market_value: Decimal

    @property
    def weight_fraction(self) -> Decimal:
        return (Decimal(self.weight_bp) / Decimal(_BP_TOTAL)).quantize(Decimal("0.0001"))

    @property
    def weight_percent(self) -> Decimal:
        return (Decimal(self.weight_bp) / Decimal(100)).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class FundVersion:
    """One generated file version of one fund."""

    ticker: str
    name: str
    source_id: str
    fmt: FileFormat
    weight_unit: WeightUnit
    as_of: date
    fetched_at: datetime
    published_at: datetime | None
    holdings: tuple[Holding, ...]
    declared_total_bp: int | None = None


_SECTORS = (
    "Materials",
    "Energy",
    "Financials",
    "Industrials",
    "Consumer Staples",
    "Utilities",
    "Information Technology",
    "Health Care",
    "Real Estate",
    "Communication Services",
)

_UNIVERSE = (
    ("NWM", "Northwind Materials Inc.", "XTSE", "CA", "CAD"),
    ("BIR", "Birchwood Timber Corp.", "XTSE", "CA", "CAD"),
    ("CDL", "Cedarline Energy Corp.", "XTSE", "CA", "CAD"),
    ("DUN", "Dunmore Mining Inc.", "XTSE", "CA", "CAD"),
    ("EVR", "Everfield Agriculture Corp.", "XTSE", "CA", "CAD"),
    ("FHV", "Fairhaven Financial Group", "XTSE", "CA", "CAD"),
    ("GLO", "Glenora Utilities Inc.", "XTSE", "CA", "CAD"),
    ("HRB", "Harbourside Logistics Corp.", "XTSE", "CA", "CAD"),
    ("IRO", "Ironbridge Steel Inc.", "XTSE", "CA", "CAD"),
    ("JUN", "Juniper Health Corp.", "XTSE", "CA", "CAD"),
    ("KES", "Kestrel Aerospace Inc.", "XTSE", "CA", "CAD"),
    ("LKS", "Lakeshore Foods Corp.", "XTSE", "CA", "CAD"),
    ("MPR", "Maplecrest Properties", "XTSE", "CA", "CAD"),
    ("NRG", "Northgate Telecom Corp.", "XTSE", "CA", "CAD"),
    ("OAK", "Oakhurst Insurance Group", "XTSE", "CA", "CAD"),
    ("PNV", "Pineview Software Corp.", "XTSE", "CA", "CAD"),
    ("QRS", "Quarrystone Cement Inc.", "XTSE", "CA", "CAD"),
    ("RVT", "Riverton Rail Corp.", "XTSE", "CA", "CAD"),
    ("STB", "Stonebrook Bank", "XTSE", "CA", "CAD"),
    ("THB", "Thornbury Retail Group", "XTSE", "CA", "CAD"),
    ("UPL", "Uplands Chemicals Inc.", "XTSE", "CA", "CAD"),
    ("VRM", "Vermilion Distillers Corp.", "XTSE", "CA", "CAD"),
    ("WST", "Westmount Power Corp.", "XTSE", "CA", "CAD"),
    ("YAR", "Yarrow Biotech Inc.", "XTSE", "CA", "CAD"),
    ("ALD", "Aldergrove Metals Ltd.", "XTSE", "CA", "CAD"),
    ("BRP", "Brighton Paper Corp.", "XTSE", "CA", "CAD"),
    ("CFR", "Cardinal Freight Ltd.", "XTSE", "CA", "CAD"),
    ("DVR", "Doverdale Textiles Ltd.", "XTSE", "CA", "CAD"),
    ("ELM", "Elmsworth Capital Corp.", "XNYS", "US", "USD"),
    ("FXN", "Foxglove Networks Inc.", "XNYS", "US", "USD"),
    ("GRP", "Granite Peak Resources", "XNYS", "US", "USD"),
    ("HWX", "Highwater Marine Corp.", "XNYS", "US", "USD"),
    ("IRW", "Ironwood Devices Inc.", "XNYS", "US", "USD"),
    ("JRX", "Juniper Ridge Pharma", "XNYS", "US", "USD"),
    ("KSD", "Keystone Drilling Corp.", "XNYS", "US", "USD"),
    ("LKM", "Larkfield Media Inc.", "XNYS", "US", "USD"),
    ("MDO", "Meridian Optics Corp.", "XNYS", "US", "USD"),
    ("NWA", "Norwood Apparel Inc.", "XNYS", "US", "USD"),
    ("OVR", "Overland Robotics Corp.", "XNAS", "US", "USD"),
    ("PWE", "Prairie Wind Energy", "XNAS", "US", "USD"),
    ("QBX", "Quartz Bay Software", "XNAS", "US", "USD"),
    ("RSP", "Redstone Payments Inc.", "XNAS", "US", "USD"),
    ("SCD", "Silvercreek Diagnostics", "XNAS", "US", "USD"),
    ("TLF", "Timberline Foods Corp.", "XNAS", "US", "USD"),
)

_COMMON_ISIN_BOTH = ("NWM", "CDL", "FHV", "GLO")
_COMMON_ISIN_CUSIP = ("IRO", "KES")
_COMMON_TICKER_BOTH = ("ELM", "FXN")
_COMMON = _COMMON_ISIN_BOTH + _COMMON_ISIN_CUSIP + _COMMON_TICKER_BOTH

_MAPL_XTSE = Security(
    ticker="MAPL",
    name="Maple Ridge Resources Inc.",
    mic="XTSE",
    country="CA",
    currency="CAD",
    sector="Energy",
    cusip=None,
    isin=None,
)
_MAPL_XNYS = Security(
    ticker="MAPL",
    name="Maple Ridge Systems Corp.",
    mic="XNYS",
    country="US",
    currency="USD",
    sector="Information Technology",
    cusip=None,
    isin=None,
)
_CASH = Security(
    ticker="CASH",
    name="Cash and Cash Equivalents",
    mic="",
    country="",
    currency="CAD",
    sector="",
    cusip=None,
    isin=None,
    asset_class="cash",
)

_COMMON_EMIT_SYNA: dict[str, EmitIdentifiers] = {
    **dict.fromkeys(_COMMON_ISIN_BOTH, "isin"),
    **dict.fromkeys(_COMMON_ISIN_CUSIP, "isin"),
    **dict.fromkeys(_COMMON_TICKER_BOTH, "ticker"),
}
_COMMON_EMIT_SYNB: dict[str, EmitIdentifiers] = {
    **dict.fromkeys(_COMMON_ISIN_BOTH, "isin"),
    **dict.fromkeys(_COMMON_ISIN_CUSIP, "cusip"),
    **dict.fromkeys(_COMMON_TICKER_BOTH, "ticker"),
}


def _build_universe() -> dict[str, Security]:
    securities: dict[str, Security] = {}
    for index, (ticker, name, mic, country, currency) in enumerate(_UNIVERSE):
        cusip9 = _cusip(f"{600000 + index:06d}AA" if country == "CA" else f"{200000 + index:06d}BA")
        securities[ticker] = Security(
            ticker=ticker,
            name=name,
            mic=mic,
            country=country,
            currency=currency,
            sector=_SECTORS[index % len(_SECTORS)],
            cusip=cusip9,
            isin=_isin(country, cusip9),
        )
    return securities


_UNIVERSE_SECURITIES = _build_universe()


@dataclass(frozen=True)
class _Entry:
    security: Security
    emit: EmitIdentifiers


def _entries(
    tickers: Iterable[str],
    emit: Mapping[str, EmitIdentifiers] | EmitIdentifiers,
) -> tuple[_Entry, ...]:
    rows: list[_Entry] = []
    for ticker in tickers:
        mode = emit[ticker] if isinstance(emit, Mapping) else emit
        rows.append(_Entry(security=_UNIVERSE_SECURITIES[ticker], emit=mode))
    return tuple(rows)


def _allocate(count: int, rng: _Rng, target_bp: int) -> list[int]:
    raw = [rng.randint(50, 500) for _ in range(count)]
    total = sum(raw)
    scaled = [value * target_bp // total for value in raw]
    scaled[0] += target_bp - sum(scaled)
    if any(value <= 0 for value in scaled):
        raise ValueError("weight allocation produced a non-positive weight")
    return scaled


def _compose(
    entries: Sequence[_Entry],
    *,
    seed: int,
    total_bp: int = _BP_TOTAL,
    cash_bp: int = _CASH_BP,
    nav: Decimal,
    overrides: Mapping[str, int] | None = None,
) -> tuple[Holding, ...]:
    weights = _allocate(len(entries), _Rng(seed), total_bp - cash_bp)
    locked: set[int] = set()
    if overrides:
        applied: set[str] = set()
        for index, entry in enumerate(entries):
            ticker = entry.security.ticker
            if ticker in overrides and ticker not in applied:
                weights[index] = overrides[ticker]
                locked.add(index)
                applied.add(ticker)
    cash_weight = total_bp - sum(weights)
    if cash_weight < _MIN_CASH_BP:
        adjustable = [index for index in range(len(weights)) if index not in locked]
        pool = sum(weights[index] for index in adjustable)
        deficit = _MIN_CASH_BP - cash_weight
        if not adjustable or pool <= deficit:
            raise ValueError("composition cannot reach the minimum cash weight")
        target = pool - deficit
        scaled = [weights[index] * target // pool for index in adjustable]
        scaled[0] += target - sum(scaled)
        if any(value <= 0 for value in scaled):
            raise ValueError("rebalancing produced a non-positive weight")
        for index, value in zip(adjustable, scaled, strict=True):
            weights[index] = value
        cash_weight = total_bp - sum(weights)
    if cash_weight <= 0:
        raise ValueError("cash weight would be non-positive")

    holdings: list[Holding] = []
    for index, entry in enumerate(entries):
        bp = weights[index]
        market_value = (Decimal(bp) / Decimal(_BP_TOTAL) * nav).quantize(Decimal("0.01"))
        price = Decimal(20 + (index * 13) % 160)
        shares = (market_value / price).to_integral_value(rounding=ROUND_HALF_UP)
        holdings.append(
            Holding(
                security=entry.security,
                weight_bp=bp,
                emit=entry.emit,
                shares=shares,
                market_value=market_value,
            )
        )
    cash_value = (Decimal(cash_weight) / Decimal(_BP_TOTAL) * nav).quantize(Decimal("0.01"))
    holdings.append(
        Holding(
            security=_CASH,
            weight_bp=cash_weight,
            emit="none",
            shares=Decimal(0),
            market_value=cash_value,
        )
    )
    ordered = sorted(
        holdings,
        key=lambda holding: (
            holding.security.asset_class == "cash",
            -holding.weight_bp,
            holding.security.ticker,
        ),
    )
    return tuple(ordered)


def _fetched(on: date, hour: int = 22) -> datetime:
    return datetime(on.year, on.month, on.day, hour, 0, 0, tzinfo=UTC)


_SYNA_ONLY = (
    "BIR",
    "DUN",
    "EVR",
    "HRB",
    "JUN",
    "LKS",
    "MPR",
    "NRG",
    "OAK",
    "PNV",
    "QRS",
    "RVT",
    "STB",
    "THB",
    "UPL",
)
_SYNB_ONLY = ("VRM", "WST", "YAR", "ALD", "BRP", "CFR", "DVR", "GRP", "HWX", "IRW", "JRX")
_SYNB_EMIT: dict[str, EmitIdentifiers] = {
    "VRM": "cusip",
    "WST": "cusip",
    "YAR": "cusip",
    "ALD": "ticker",
    "BRP": "cusip",
    "CFR": "ticker",
    "DVR": "ticker",
    "GRP": "cusip",
    "HWX": "ticker",
    "IRW": "cusip",
    "JRX": "ticker",
}

_SYNA_ENTRIES = (
    _entries(_COMMON, _COMMON_EMIT_SYNA)
    + (_Entry(_MAPL_XTSE, "none"),)
    + _entries(_SYNA_ONLY, "isin")
)
_SYNB_ENTRIES = (
    _entries(_COMMON, _COMMON_EMIT_SYNB)
    + (_Entry(_MAPL_XNYS, "none"),)
    + _entries(_SYNB_ONLY, _SYNB_EMIT)
)


def _fund_versions() -> list[FundVersion]:
    nav_syna = Decimal(10_000_000)
    nav_synb = Decimal(5_000_000)
    versions: list[FundVersion] = []

    versions.append(
        FundVersion(
            "SYNA",
            "Synthetic Aurora Equity Fund",
            "syn_csv",
            FileFormat.csv,
            WeightUnit.percent,
            date(2026, 10, 29),
            _fetched(date(2026, 10, 29)),
            None,
            _compose(_SYNA_ENTRIES, seed=_SEED + 1, nav=nav_syna),
        )
    )
    versions.append(
        FundVersion(
            "SYNA",
            "Synthetic Aurora Equity Fund",
            "syn_csv",
            FileFormat.csv,
            WeightUnit.percent,
            date(2026, 10, 30),
            _fetched(date(2026, 10, 30)),
            None,
            _compose(
                _SYNA_ENTRIES, seed=_SEED + 1, nav=nav_syna, overrides={"NWM": 460, "OAK": 210}
            ),
        )
    )
    versions.append(
        FundVersion(
            "SYNA",
            "Synthetic Aurora Equity Fund",
            "syn_csv",
            FileFormat.csv,
            WeightUnit.percent,
            date(2026, 11, 2),
            _fetched(date(2026, 11, 2)),
            None,
            _compose(_SYNA_ENTRIES, seed=_SEED + 2, nav=nav_syna),
        )
    )
    lookahead_syna = (
        _entries(_COMMON, _COMMON_EMIT_SYNA)
        + (_Entry(_MAPL_XTSE, "none"),)
        + _entries(
            ("BIR", "DUN", "EVR", "HRB", "JUN", "LKS", "MPR", "NRG", "OAK", "PNV", "QRS"), "isin"
        )
        + _entries(("KSD", "LKM", "MDO"), "ticker")
    )
    versions.append(
        FundVersion(
            "SYNA",
            "Synthetic Aurora Equity Fund",
            "syn_csv",
            FileFormat.csv,
            WeightUnit.percent,
            date(2026, 11, 3),
            _fetched(date(2026, 11, 3)),
            None,
            _compose(
                lookahead_syna,
                seed=_SEED + 3,
                nav=nav_syna,
                overrides={"NWM": 900, "CDL": 60},
            ),
        )
    )

    versions.append(
        FundVersion(
            "SYNB",
            "Synthetic Borealis Equity Fund",
            "syn_xlsx",
            FileFormat.xlsx,
            WeightUnit.fraction,
            date(2026, 10, 29),
            _fetched(date(2026, 10, 29)),
            None,
            _compose(_SYNB_ENTRIES, seed=_SEED + 11, nav=nav_synb),
        )
    )
    versions.append(
        FundVersion(
            "SYNB",
            "Synthetic Borealis Equity Fund",
            "syn_xlsx",
            FileFormat.xlsx,
            WeightUnit.fraction,
            date(2026, 10, 30),
            _fetched(date(2026, 10, 30)),
            None,
            _compose(_SYNB_ENTRIES, seed=_SEED + 11, nav=nav_synb, overrides={"NWM": 430}),
        )
    )
    versions.append(
        FundVersion(
            "SYNB",
            "Synthetic Borealis Equity Fund",
            "syn_xlsx",
            FileFormat.xlsx,
            WeightUnit.fraction,
            date(2026, 10, 30),
            _fetched(date(2026, 10, 31), hour=14),
            None,
            _compose(_SYNB_ENTRIES, seed=_SEED + 11, nav=nav_synb, overrides={"NWM": 470}),
        )
    )
    versions.append(
        FundVersion(
            "SYNB",
            "Synthetic Borealis Equity Fund",
            "syn_xlsx",
            FileFormat.xlsx,
            WeightUnit.fraction,
            date(2026, 11, 2),
            _fetched(date(2026, 11, 2)),
            None,
            _compose(_SYNB_ENTRIES, seed=_SEED + 12, nav=nav_synb),
        )
    )
    lookahead_synb = (
        _entries(_COMMON, _COMMON_EMIT_SYNB)
        + (_Entry(_MAPL_XNYS, "none"),)
        + _entries(
            ("VRM", "WST", "YAR", "ALD", "BRP", "CFR", "DVR", "GRP", "HWX", "IRW"), _SYNB_EMIT
        )
    )
    versions.append(
        FundVersion(
            "SYNB",
            "Synthetic Borealis Equity Fund",
            "syn_xlsx",
            FileFormat.xlsx,
            WeightUnit.fraction,
            date(2026, 11, 2),
            _fetched(date(2026, 11, 3), hour=15),
            None,
            _compose(
                lookahead_synb,
                seed=_SEED + 12,
                nav=nav_synb,
                overrides={"CDL": 500, "NWM": 40},
            ),
        )
    )

    versions.append(
        FundVersion(
            "SYNC",
            "Synthetic Cascade Equity Fund",
            "syn_csv",
            FileFormat.csv,
            WeightUnit.fraction,
            date(2026, 10, 29),
            _fetched(date(2026, 10, 29)),
            None,
            _compose(
                _entries(
                    (
                        "NWM",
                        "BIR",
                        "CDL",
                        "DUN",
                        "EVR",
                        "FHV",
                        "GLO",
                        "HRB",
                        "IRO",
                        "JUN",
                        "KES",
                        "LKS",
                        "MPR",
                        "NRG",
                        "OAK",
                        "PNV",
                        "QRS",
                        "RVT",
                        "STB",
                    ),
                    "isin",
                ),
                seed=_SEED + 21,
                total_bp=8500,
                nav=Decimal(2_500_000),
            ),
        )
    )

    duplicate = _UNIVERSE_SECURITIES["NWM"]
    versions.append(
        FundVersion(
            "SYND",
            "Synthetic Dunes Equity Fund",
            "syn_xlsx",
            FileFormat.xlsx,
            WeightUnit.fraction,
            date(2026, 10, 29),
            _fetched(date(2026, 10, 29)),
            None,
            _compose(
                _entries(
                    ("NWM", "CDL", "FHV", "GLO", "IRO", "KES", "ELM", "FXN", "BIR", "DUN", "EVR"),
                    "isin",
                )
                + (_Entry(duplicate, "cusip"),)
                + _entries(("HRB", "JUN", "LKS", "MPR"), "isin"),
                seed=_SEED + 31,
                nav=Decimal(3_000_000),
            ),
        )
    )

    for as_of, seed, overrides in (
        (date(2026, 10, 29), _SEED + 41, None),
        (date(2026, 10, 30), _SEED + 41, {"NWM": 520, "CDL": 380}),
        (date(2026, 11, 2), _SEED + 42, {"NWM": 540}),
    ):
        versions.append(
            FundVersion(
                "SYNE",
                "Synthetic Ember Equity Fund",
                "syn_json",
                FileFormat.json,
                WeightUnit.fraction,
                as_of,
                _fetched(as_of),
                None,
                _compose(
                    _entries(
                        (
                            "NWM",
                            "CDL",
                            "FHV",
                            "GLO",
                            "VRM",
                            "WST",
                            "YAR",
                            "ALD",
                            "BRP",
                            "CFR",
                            "DVR",
                            "GRP",
                            "HWX",
                            "IRW",
                            "JRX",
                            "KSD",
                            "LKM",
                            "MDO",
                            "NWA",
                            "OVR",
                            "PWE",
                        ),
                        "isin",
                    ),
                    seed=seed,
                    nav=Decimal(1_500_000),
                    overrides=overrides,
                ),
            )
        )

    pdf_as_of = date(2026, 9, 30)
    versions.append(
        FundVersion(
            "SYNF",
            "Synthetic Fjord Equity Fund",
            "syn_pdf",
            FileFormat.pdf,
            WeightUnit.percent,
            pdf_as_of,
            _fetched(date(2026, 10, 28)),
            _fetched(date(2026, 10, 28)),
            _compose(
                _entries(
                    (
                        "NWM",
                        "CDL",
                        "FHV",
                        "GLO",
                        "IRO",
                        "KES",
                        "ELM",
                        "FXN",
                        "BIR",
                        "DUN",
                        "EVR",
                        "HRB",
                        "JUN",
                        "LKS",
                        "MPR",
                        "NRG",
                        "OAK",
                        "PNV",
                        "QRS",
                        "RVT",
                        "STB",
                        "THB",
                        "UPL",
                        "VRM",
                    ),
                    "isin",
                ),
                seed=_SEED + 51,
                total_bp=8235,
                cash_bp=735,
                nav=Decimal(4_000_000),
            ),
            declared_total_bp=8235,
        )
    )
    return versions


def _format_date(value: date) -> str:
    return f"{_MONTHS[value.month - 1]} {value.day}, {value.year}"


def _identifier_columns(holding: Holding) -> tuple[str, str]:
    security = holding.security
    if holding.emit == "isin":
        return security.isin or "", ""
    if holding.emit == "cusip":
        return "", security.cusip or ""
    return "", ""


def _asset_class_label(holding: Holding) -> str:
    return "Cash" if holding.security.asset_class == "cash" else "Equity"


def _weight_value(holding: Holding, unit: WeightUnit) -> Decimal:
    return holding.weight_percent if unit is WeightUnit.percent else holding.weight_fraction


def _weight_text(holding: Holding, unit: WeightUnit) -> str:
    value = _weight_value(holding, unit)
    return f"{value:.2f}" if unit is WeightUnit.percent else f"{value:.4f}"


def _render_csv(version: FundVersion) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([version.name])
    writer.writerow(["Fund Holdings as of", _format_date(version.as_of)])
    writer.writerow(["Number of Holdings", len(version.holdings)])
    writer.writerow([])
    writer.writerow(
        [
            "Ticker",
            "Name",
            "ISIN",
            "Asset Class",
            "Sector",
            "Country",
            "Currency",
            "Exchange",
            "Weight (%)" if version.weight_unit is WeightUnit.percent else "Weight",
            "Shares",
            "Market Value",
        ]
    )
    for holding in version.holdings:
        isin, _ = _identifier_columns(holding)
        writer.writerow(
            [
                holding.security.ticker,
                holding.security.name,
                isin,
                _asset_class_label(holding),
                holding.security.sector,
                holding.security.country,
                holding.security.currency,
                holding.security.mic,
                _weight_text(holding, version.weight_unit),
                f"{holding.shares:.0f}",
                f"{holding.market_value:.2f}",
            ]
        )
    return buffer.getvalue().encode("utf-8")


def _render_json(version: FundVersion) -> bytes:
    holdings: list[dict[str, object]] = []
    for holding in version.holdings:
        isin, cusip = _identifier_columns(holding)
        holdings.append(
            {
                "name": holding.security.name,
                "ticker": holding.security.ticker,
                "isin": isin or None,
                "cusip": cusip or None,
                "exchange": holding.security.mic or None,
                "asset_class": _asset_class_label(holding).lower(),
                "sector": holding.security.sector or None,
                "country": holding.security.country or None,
                "currency": holding.security.currency,
                "weight": float(_weight_value(holding, version.weight_unit)),
                "shares": float(holding.shares),
                "market_value": float(holding.market_value),
            }
        )
    payload = {
        "fund": {"ticker": version.ticker, "name": version.name},
        "portfolio": {"dt": version.as_of.isoformat(), "holdings": holdings},
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _normalize_xlsx_archive(data: bytes) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(data))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as target:
        for info in source.infolist():
            entry = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = info.external_attr
            entry.create_system = 0
            content = (
                _CORE_XML.encode("utf-8")
                if info.filename == "docProps/core.xml"
                else source.read(info.filename)
            )
            target.writestr(entry, content)
    return output.getvalue()


def _render_xlsx(version: FundVersion) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Holdings"
    sheet.append([version.name])
    sheet.append(["Holdings Extract"])
    sheet.append(["As of Date", version.as_of.isoformat()])
    sheet.append([])
    sheet.append(
        [
            "Security Name",
            "Ticker",
            "ISIN",
            "CUSIP",
            "Exchange",
            "Sector",
            "Country",
            "Currency",
            "Weight",
            "Shares",
            "Market Value",
        ]
    )
    for holding in version.holdings:
        isin, cusip = _identifier_columns(holding)
        sheet.append(
            [
                holding.security.name,
                holding.security.ticker,
                isin,
                cusip,
                holding.security.mic,
                holding.security.sector,
                holding.security.country,
                holding.security.currency,
                float(_weight_value(holding, version.weight_unit)),
                int(holding.shares),
                float(holding.market_value),
            ]
        )
    buffer = io.BytesIO()
    workbook.save(buffer)
    return _normalize_xlsx_archive(buffer.getvalue())


def _render_pdf(version: FundVersion) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter, invariant=1)
    pdf.setTitle("Summary of Investment Portfolio")
    pdf.setAuthor("etfh synthetic fixtures")
    pdf.setSubject("")
    pdf.setCreator("etfh synthetic fixtures")
    width, height = letter
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(
        72, height - 72, f"Summary of Investment Portfolio as at {version.as_of.isoformat()}"
    )
    pdf.setFont("Helvetica", 10)
    pdf.drawString(72, height - 90, version.name)
    if version.published_at is not None:
        pdf.drawString(72, height - 104, f"Published {version.published_at.date().isoformat()}")
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(72, height - 132, "Holding")
    pdf.drawRightString(width - 72, height - 132, "% of net asset value")
    pdf.setFont("Helvetica", 9)
    y = height - 148
    for holding in version.holdings:
        pdf.drawString(72, y, holding.security.name)
        pdf.drawRightString(width - 72, y, _weight_text(holding, version.weight_unit))
        y -= 13
    total = Decimal(version.declared_total_bp or 0) / Decimal(100)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawRightString(width - 72, y - 6, "-----")
    pdf.drawString(
        72, y - 24, f"Total of the holdings listed above: {total:.2f}% of net asset value"
    )
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _render(version: FundVersion) -> bytes:
    if version.fmt is FileFormat.csv:
        return _render_csv(version)
    if version.fmt is FileFormat.xlsx:
        return _render_xlsx(version)
    if version.fmt is FileFormat.json:
        return _render_json(version)
    return _render_pdf(version)


_CONTENT_TYPES = {
    FileFormat.csv: "text/csv",
    FileFormat.xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    FileFormat.json: "application/json",
    FileFormat.pdf: "application/pdf",
}


class CommonHoldingFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    security_id: str
    weight_a: str
    weight_b: str


class ExpectedOverlapFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fund_a: str
    fund_b: str
    asof: str
    overlap: str
    common_count: int
    common: list[CommonHoldingFixture]


class FundFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    format: str
    weight_unit: str
    as_of_dates: list[str]


class BadFileFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fund_ticker: str
    path: str
    failing_check: str
    detail: str


class VersionFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    fetched_at: str
    note: str


class RepublishedFileFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fund_ticker: str
    as_of_date: str
    versions: list[VersionFixture]


class LookaheadFileFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fund_ticker: str
    as_of_date: str
    path: str
    fetched_at: str
    note: str


class MaplRowFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fund_ticker: str
    exchange: str
    security_name: str


class PercentFractionPairFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fund_a: str
    unit_a: str
    fund_b: str
    unit_b: str


class PdfStatedTotalFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    as_of_date: str
    published_at: str
    stated_total: str
    table_sum: str


class Manifest(BaseModel):
    """Description of the generated fixture tree and its injected cases."""

    model_config = ConfigDict(extra="forbid")

    funds: dict[str, FundFixture]
    bad_files: list[BadFileFixture]
    republished_file: RepublishedFileFixture
    lookahead_files: list[LookaheadFileFixture]
    mapl_pair: list[MaplRowFixture]
    percent_fraction_pair: PercentFractionPairFixture
    pdf_stated_total: PdfStatedTotalFixture
    expected_overlap: ExpectedOverlapFixture


def _version_key(version: FundVersion) -> tuple[date, datetime]:
    return (version.as_of, version.fetched_at)


def _select_version(
    versions: Sequence[FundVersion],
    as_of: date,
    known_before: datetime | None = None,
) -> FundVersion:
    cutoff = known_before
    if cutoff is None:
        cutoff = datetime.combine(
            as_of + timedelta(days=1), time(0, 0), tzinfo=_TORONTO
        ).astimezone(UTC)
    candidates = [version for version in versions if version.fetched_at <= cutoff]
    if not candidates:
        raise LookupError(f"no version known before {cutoff.isoformat()}")
    return max(candidates, key=_version_key)


def ground_truth_holdings(
    ticker: str,
    as_of: date,
    known_before: datetime | None = None,
) -> dict[str, Decimal]:
    """Security-id to fraction weights for the version used as of ``as_of``.

    ``known_before`` defaults to the start of the next calendar day in
    America/Toronto (D30). Duplicate rows in a file collapse to one entry.
    """
    versions = [version for version in _fund_versions() if version.ticker == ticker]
    if not versions:
        raise KeyError(f"unknown synthetic fund {ticker!r}")
    version = _select_version(versions, as_of, known_before)
    weights: dict[str, Decimal] = {}
    for holding in version.holdings:
        weights[_resolved_security_id(holding)] = holding.weight_fraction
    return weights


def _is_non_security(security_id: str) -> bool:
    return security_id.startswith("CASH:") or security_id.startswith("DERIV:")


def _resolved_security_id(holding: Holding) -> str:
    """The D23 security id for a holding given the identifiers its row emits.

    A CUSIP-only row links to the ISIN seen for the same security elsewhere in
    the fixture set; a ticker-only row resolves to ``TKR:<MIC>:<ticker>``.
    """
    security = holding.security
    if security.asset_class == "cash":
        return f"CASH:{security.currency}"
    if holding.emit == "isin" and security.isin:
        return f"ISIN:{security.isin}"
    if holding.emit == "cusip":
        if security.isin:
            return f"ISIN:{security.isin}"
        if security.cusip:
            return f"CUSIP:{security.cusip}"
    if holding.emit == "ticker":
        return f"TKR:{security.mic}:{security.ticker}"
    if security.isin:
        return f"ISIN:{security.isin}"
    if security.cusip:
        return f"CUSIP:{security.cusip}"
    return f"TKR:{security.mic}:{security.ticker}"


def _expected_overlap(as_of: date = date(2026, 11, 2)) -> ExpectedOverlapFixture:
    weights_a = ground_truth_holdings("SYNA", as_of)
    weights_b = ground_truth_holdings("SYNB", as_of)
    common_ids = sorted(
        security_id
        for security_id in set(weights_a) & set(weights_b)
        if not _is_non_security(security_id)
    )
    quantize = Decimal("0.0001")
    overlap = sum((min(weights_a[sid], weights_b[sid]) for sid in common_ids), Decimal(0)).quantize(
        quantize
    )
    return ExpectedOverlapFixture(
        fund_a="SYNA",
        fund_b="SYNB",
        asof=as_of.isoformat(),
        overlap=str(overlap),
        common_count=len(common_ids),
        common=[
            CommonHoldingFixture(
                security_id=sid,
                weight_a=str(weights_a[sid].quantize(quantize)),
                weight_b=str(weights_b[sid].quantize(quantize)),
            )
            for sid in common_ids
        ],
    )


def _relative_path(root: Path, raw_path: Path) -> str:
    return raw_path.relative_to(root).as_posix()


def _build_manifest(root: Path, raws: Sequence[tuple[FundVersion, RawFile]]) -> Manifest:
    paths: dict[tuple[str, str, str], str] = {}
    for version, raw in raws:
        stamp = version.fetched_at.strftime("%Y%m%dT%H%M%SZ")
        key = (version.ticker, version.as_of.isoformat(), stamp)
        paths[key] = _relative_path(root, raw.path)

    def path_of(ticker: str, as_of: date, fetched_at: datetime) -> str:
        return paths[(ticker, as_of.isoformat(), fetched_at.strftime("%Y%m%dT%H%M%SZ"))]

    funds = {
        ticker: FundFixture(
            source_id=versions[0].source_id,
            format=versions[0].fmt.value,
            weight_unit=versions[0].weight_unit.value,
            as_of_dates=sorted({version.as_of.isoformat() for version in versions}),
        )
        for ticker, versions in sorted(_versions_by_ticker().items())
    }
    bad_files = [
        BadFileFixture(
            fund_ticker="SYNC",
            path=path_of("SYNC", date(2026, 10, 29), _fetched(date(2026, 10, 29))),
            failing_check="weight_sum",
            detail="weights sum to 0.8500 for an equity fund (expected 1.0000)",
        ),
        BadFileFixture(
            fund_ticker="SYND",
            path=path_of("SYND", date(2026, 10, 29), _fetched(date(2026, 10, 29))),
            failing_check="duplicate_security",
            detail=(
                "Northwind Materials Inc. appears twice after normalization "
                "(ISIN row and CUSIP row)"
            ),
        ),
    ]
    republished = RepublishedFileFixture(
        fund_ticker="SYNB",
        as_of_date=date(2026, 10, 30).isoformat(),
        versions=[
            VersionFixture(
                path=path_of("SYNB", date(2026, 10, 30), _fetched(date(2026, 10, 30))),
                fetched_at=_fetched(date(2026, 10, 30)).isoformat(),
                note="file_version 1",
            ),
            VersionFixture(
                path=path_of("SYNB", date(2026, 10, 30), _fetched(date(2026, 10, 31), hour=14)),
                fetched_at=_fetched(date(2026, 10, 31), hour=14).isoformat(),
                note="file_version 2; one weight corrected",
            ),
        ],
    )
    lookahead = [
        LookaheadFileFixture(
            fund_ticker="SYNB",
            as_of_date=date(2026, 11, 2).isoformat(),
            path=path_of("SYNB", date(2026, 11, 2), _fetched(date(2026, 11, 3), hour=15)),
            fetched_at=_fetched(date(2026, 11, 3), hour=15).isoformat(),
            note="republished after the as-of cutoff with materially different weights; "
            "must not affect the 2026-11-02 result",
        ),
        LookaheadFileFixture(
            fund_ticker="SYNA",
            as_of_date=date(2026, 11, 3).isoformat(),
            path=path_of("SYNA", date(2026, 11, 3), _fetched(date(2026, 11, 3))),
            fetched_at=_fetched(date(2026, 11, 3)).isoformat(),
            note="as-of date after the 2026-11-02 cutoff; must not affect the result",
        ),
    ]
    mapl_pair = [
        MaplRowFixture(fund_ticker="SYNA", exchange="XTSE", security_name=_MAPL_XTSE.name),
        MaplRowFixture(fund_ticker="SYNB", exchange="XNYS", security_name=_MAPL_XNYS.name),
    ]
    pair = PercentFractionPairFixture(
        fund_a="SYNA",
        unit_a=WeightUnit.percent.value,
        fund_b="SYNB",
        unit_b=WeightUnit.fraction.value,
    )
    pdf_version = next(version for version in _fund_versions() if version.fmt is FileFormat.pdf)
    pdf_total = Decimal(pdf_version.declared_total_bp or 0) / Decimal(100)
    pdf_path = path_of(pdf_version.ticker, pdf_version.as_of, pdf_version.fetched_at)
    pdf_case = PdfStatedTotalFixture(
        path=pdf_path,
        as_of_date=pdf_version.as_of.isoformat(),
        published_at=(pdf_version.published_at or pdf_version.fetched_at).date().isoformat(),
        stated_total=f"{pdf_total:.2f}",
        table_sum=f"{pdf_total:.2f}",
    )
    return Manifest(
        funds=funds,
        bad_files=bad_files,
        republished_file=republished,
        lookahead_files=lookahead,
        mapl_pair=mapl_pair,
        percent_fraction_pair=pair,
        pdf_stated_total=pdf_case,
        expected_overlap=_expected_overlap(),
    )


def _versions_by_ticker() -> dict[str, list[FundVersion]]:
    grouped: dict[str, list[FundVersion]] = {}
    for version in _fund_versions():
        grouped.setdefault(version.ticker, []).append(version)
    return grouped


def generate(out: Path) -> Manifest:
    """Write the fixture tree rooted at ``out`` and return its manifest.

    The raw store is written under ``out``; ``manifest.json`` and
    ``expected_overlap.json`` are written next to it (``out.parent``).
    """
    out = Path(out)
    store = RawStore(out)
    written: list[tuple[FundVersion, RawFile]] = []
    for version in _fund_versions():
        raw = store.write(
            _render(version),
            source_id=version.source_id,
            issuer="synthetic",
            fund_ticker=version.ticker,
            url=f"synthetic://fixtures/{version.ticker}.{version.fmt.value}",
            fetched_at=version.fetched_at,
            ext=version.fmt.value,
            content_type=_CONTENT_TYPES[version.fmt],
            as_of_hint=version.as_of,
            http_status=200,
        )
        written.append((version, raw))
    manifest = _build_manifest(out, written)
    out.parent.mkdir(parents=True, exist_ok=True)
    (out.parent / "manifest.json").write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (out.parent / "expected_overlap.json").write_text(
        manifest.expected_overlap.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: ``python -m etfh.synthetic --out DIR``."""
    parser = argparse.ArgumentParser(description="Generate deterministic synthetic fixtures.")
    parser.add_argument("--out", required=True, type=Path, help="raw store directory to write")
    arguments = parser.parse_args(argv)
    manifest = generate(arguments.out)
    file_count = sum(len(versions) for versions in _versions_by_ticker().values())
    print(
        f"wrote {file_count} files for {len(manifest.funds)} funds to {arguments.out} "
        f"(overlap {manifest.expected_overlap.overlap})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
