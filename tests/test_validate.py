"""Tests for the validation config, QA finding types and per-file checks."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest
from pydantic import ValidationError

from etfh.registry import Source, load_funds, load_sources
from etfh.types import AssetClass, ParsedFile, RawFile
from etfh.validate import QaFinding, ValidationConfig, is_excluded, validate_file
from tests._validate_helpers import load_manifest, parse_fixture, raw_files

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
CONFIG_PATH = CONFIG_DIR / "validation.yaml"
_CONFIG = ValidationConfig.load(CONFIG_PATH)
_SOURCES = load_sources(CONFIG_DIR / "sources.yaml")
_FUNDS = {fund.ticker: fund for fund in load_funds(CONFIG_DIR / "funds.csv", _SOURCES)}
_STRING_COLUMNS = ("security_ticker", "exchange", "isin", "cusip", "currency")


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "fund_ticker": "SYNA",
        "as_of_date": date(2026, 10, 29),
        "security_name": "Northwind Materials Inc.",
        "security_ticker": "NWM",
        "exchange": "XTSE",
        "isin": "CA600000AA15",
        "cusip": None,
        "asset_class": "equity",
        "sector": "Materials",
        "country": "CA",
        "currency": "CAD",
        "weight": 1.0,
        "shares": 3038.0,
        "market_value": 60750.0,
        "source_id": "syn_csv",
        "raw_file_sha256": "a" * 64,
    }
    row.update(overrides)
    return row


def _frame(rows: list[dict[str, object]] | None = None) -> pl.DataFrame:
    return pl.DataFrame(
        rows if rows is not None else [_row()],
        schema_overrides={column: pl.String for column in _STRING_COLUMNS},
    )


def _with_security_id(frame: pl.DataFrame, values: list[str | None]) -> pl.DataFrame:
    return frame.with_columns(pl.Series("security_id", values, dtype=pl.String))


def _parsed(
    frame: pl.DataFrame,
    *,
    as_of: date | None = None,
    fetched_at: datetime | None = None,
    notes: list[str] | None = None,
) -> ParsedFile:
    raw = RawFile(
        source_id="syn_csv",
        issuer="synthetic",
        fund_ticker="SYNA",
        as_of_hint=date(2026, 10, 29),
        fetched_at=fetched_at or datetime(2026, 10, 29, 22, 0, tzinfo=UTC),
        url="synthetic://fixtures/SYNA.csv",
        path=Path("tests/fixtures/raw_synthetic/synthetic/SYNA/2026-10-29/20261029T220000Z.csv"),
        sha256="a" * 64,
        content_type="text/csv",
        ext="csv",
    )
    return ParsedFile(
        raw=raw,
        as_of_date=as_of or date(2026, 10, 29),
        published_at=None,
        frame=frame,
        notes=notes or [],
    )


def _checks(findings: list[QaFinding]) -> list[str]:
    return [finding.check for finding in findings]


def _error_checks(findings: list[QaFinding]) -> set[str]:
    return {finding.check for finding in findings if finding.severity == "error"}


def _single_row_frame(total: float) -> pl.DataFrame:
    return _with_security_id(_frame([_row(weight=total)]), ["ISIN:CA600000AA15"])


def _weight_sum_errors(asset_class: AssetClass, total: float) -> set[str]:
    findings = validate_file(
        _parsed(_single_row_frame(total)),
        source=_SOURCES["syn_csv"],
        fund_asset_class=asset_class,
        config=_CONFIG,
    )
    return {finding.check for finding in findings if finding.check == "weight_sum"}


def _tolerance_cases() -> list[tuple[AssetClass, float, bool]]:
    cases: list[tuple[AssetClass, float, bool]] = [
        (AssetClass.equity, 0.993, True),
        (AssetClass.fixed_income, 0.993, False),
        (AssetClass.balanced, 0.993, False),
    ]
    for asset_class in (AssetClass.equity, AssetClass.balanced):
        tolerance = _CONFIG.weight_sum_tolerance[asset_class]
        high = 1.0 + tolerance
        low = 1.0 - tolerance
        cases.extend(
            [
                (asset_class, math.nextafter(high, 0.0), False),
                (asset_class, math.nextafter(high, math.inf), True),
                (asset_class, math.nextafter(low, math.inf), False),
                (asset_class, math.nextafter(low, -math.inf), True),
            ]
        )
    return cases


class TestValidationConfig:
    def test_loads_committed_config(self) -> None:
        config = ValidationConfig.load(CONFIG_PATH)
        assert config.weight_sum_tolerance == {
            AssetClass.equity: 0.005,
            AssetClass.fixed_income: 0.01,
            AssetClass.balanced: 0.02,
            AssetClass.commodity: 0.02,
            AssetClass.other: 0.02,
        }
        assert config.qpd_total_tolerance == 0.005
        assert config.future_as_of_days == 0

    def test_missing_asset_class_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "validation.yaml"
        path.write_text(
            "weight_sum_tolerance:\n"
            "  equity: 0.005\n"
            "  fixed_income: 0.01\n"
            "  commodity: 0.02\n"
            "  other: 0.02\n"
            "qpd_total_tolerance: 0.005\n"
            "future_as_of_days: 0\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError, match="missing asset classes: balanced"):
            ValidationConfig.load(path)

    def test_negative_tolerance_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "validation.yaml"
        path.write_text(
            "weight_sum_tolerance:\n"
            "  equity: -0.001\n"
            "  fixed_income: 0.01\n"
            "  balanced: 0.02\n"
            "  commodity: 0.02\n"
            "  other: 0.02\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError, match=r"weight_sum_tolerance\[equity\]"):
            ValidationConfig.load(path)

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "validation.yaml"
        path.write_text(
            "weight_sum_tolerance:\n"
            "  equity: 0.005\n"
            "  fixed_income: 0.01\n"
            "  balanced: 0.02\n"
            "  commodity: 0.02\n"
            "  other: 0.02\n"
            "surprise: true\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError, match="surprise"):
            ValidationConfig.load(path)

    def test_non_mapping_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "validation.yaml"
        path.write_text("- equity\n- fixed_income\n", encoding="utf-8")
        with pytest.raises(ValueError, match="expected a mapping"):
            ValidationConfig.load(path)


class TestIsExcluded:
    def test_error_excludes(self) -> None:
        assert is_excluded([QaFinding("weight_sum", "error", "sum=0.8500 expected=1±0.005")])

    def test_warning_and_info_do_not_exclude(self) -> None:
        assert not is_excluded(
            [QaFinding("negative_weight", "warning", "weight=-0.01"), QaFinding("x", "info", "y")]
        )

    def test_empty_does_not_exclude(self) -> None:
        assert not is_excluded([])


class TestValidateFileSchemaCheck:
    def test_valid_frame_has_no_findings(self, source_factory: Callable[..., Source]) -> None:
        frame = _with_security_id(_frame(), ["ISIN:CA600000AA15"])
        findings = validate_file(
            _parsed(frame),
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=ValidationConfig.load(CONFIG_PATH),
        )
        assert findings == []

    def test_missing_security_id_column(self, source_factory: Callable[..., Source]) -> None:
        findings = validate_file(
            _parsed(_frame()),
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=ValidationConfig.load(CONFIG_PATH),
        )
        assert _checks(findings) == ["schema"]
        assert findings[0].severity == "error"
        assert "security_id column missing" in findings[0].detail

    def test_non_string_security_id_column(self, source_factory: Callable[..., Source]) -> None:
        frame = _frame().with_columns(pl.lit(1).alias("security_id"))
        findings = validate_file(
            _parsed(frame),
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=ValidationConfig.load(CONFIG_PATH),
        )
        assert _checks(findings) == ["schema"]
        assert findings[0].severity == "error"
        assert "security_id column has dtype" in findings[0].detail

    def test_missing_required_column(self, source_factory: Callable[..., Source]) -> None:
        frame = _with_security_id(_frame().drop("sector"), ["ISIN:CA600000AA15"])
        findings = validate_file(
            _parsed(frame),
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=ValidationConfig.load(CONFIG_PATH),
        )
        assert _checks(findings) == ["schema"]
        assert findings[0].severity == "error"
        assert "sector" in findings[0].detail


@pytest.mark.parametrize(("asset_class", "total", "expect_error"), _tolerance_cases())
def test_weights_sum_tolerance_by_asset_class(
    asset_class: AssetClass, total: float, expect_error: bool
) -> None:
    errors = _weight_sum_errors(asset_class, total)
    assert (errors == {"weight_sum"}) is expect_error


def _validate_raw(raw: RawFile) -> list[QaFinding]:
    return validate_file(
        parse_fixture(raw),
        source=_SOURCES[raw.source_id],
        fund_asset_class=_FUNDS[raw.fund_ticker].asset_class,
        config=_CONFIG,
    )


def _pdf_parsed() -> ParsedFile:
    return parse_fixture(next(raw for raw in raw_files() if raw.ext == "pdf"))


class TestFixtureFiles:
    def test_exactly_the_two_bad_files_fail(self) -> None:
        manifest = load_manifest()
        expected = {entry["fund_ticker"]: entry["failing_check"] for entry in manifest["bad_files"]}
        failing: dict[str, set[str]] = {}
        for raw in raw_files():
            errors = _error_checks(_validate_raw(raw))
            if errors:
                failing.setdefault(raw.fund_ticker, set()).update(errors)
        assert failing == {
            "SYNC": {"weight_sum"},
            "SYND": {"duplicate_security"},
        }
        assert set(expected) == set(failing)

    def test_bad_file_details_name_numbers(self) -> None:
        details: dict[str, list[str]] = {}
        for raw in raw_files():
            if raw.fund_ticker in {"SYNC", "SYND"}:
                details[raw.fund_ticker] = [
                    finding.detail for finding in _validate_raw(raw) if finding.severity == "error"
                ]
        assert any(
            "sum=0.8500" in detail and "expected=1±0.005" in detail for detail in details["SYNC"]
        )
        assert any(
            "ISIN:CA600000AA15" in detail and "appears 2 times" in detail
            for detail in details["SYND"]
        )

    def test_quarterly_disclosure_checked_against_stated_total_not_one(self) -> None:
        pdf_raw = next(raw for raw in raw_files() if raw.ext == "pdf")
        findings = _validate_raw(pdf_raw)
        assert _checks(findings) == []

    def test_daily_file_is_not_checked_against_stated_total(self) -> None:
        csv_raw = next(raw for raw in raw_files() if raw.ext == "csv" and raw.fund_ticker == "SYNA")
        assert "qpd_stated_total" not in _checks(_validate_raw(csv_raw))


class TestQpdStatedTotal:
    def _validate_pdf(self, notes: list[str]) -> list[QaFinding]:
        parsed = _pdf_parsed()
        parsed.notes = notes
        return validate_file(
            parsed,
            source=_SOURCES["syn_pdf"],
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )

    def test_mismatch_is_error(self) -> None:
        findings = self._validate_pdf(
            ["declared_unit=percent", "stated_total_fraction=0.9", "partial_list=true"]
        )
        errors = [f for f in findings if f.check == "qpd_stated_total"]
        assert len(errors) == 1
        assert errors[0].severity == "error"
        assert "sum=0.8235" in errors[0].detail
        assert "stated_total=0.9000" in errors[0].detail

    def test_missing_note_is_warning(self) -> None:
        findings = self._validate_pdf(["declared_unit=percent", "partial_list=true"])
        warnings = [f for f in findings if f.check == "qpd_stated_total"]
        assert len(warnings) == 1
        assert warnings[0].severity == "warning"
        assert not is_excluded(findings)

    def test_unparseable_note_is_error(self) -> None:
        findings = self._validate_pdf(
            ["declared_unit=percent", "stated_total_fraction=n/a", "partial_list=true"]
        )
        errors = [f for f in findings if f.check == "qpd_stated_total"]
        assert len(errors) == 1
        assert errors[0].severity == "error"
        assert "'n/a'" in errors[0].detail


class TestDuplicateSecurity:
    def test_two_cash_rows_of_different_currencies_pass(
        self, source_factory: Callable[..., Source]
    ) -> None:
        rows = [
            _row(security_name="Cash", asset_class="cash", isin=None, weight=0.99),
            _row(
                security_name="Cash",
                asset_class="cash",
                isin=None,
                currency="USD",
                weight=0.01,
            ),
        ]
        frame = _with_security_id(_frame(rows), ["CASH:CAD", "CASH:USD"])
        findings = validate_file(
            _parsed(frame),
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        assert findings == []

    def test_same_cash_currency_twice_is_duplicate(
        self, source_factory: Callable[..., Source]
    ) -> None:
        rows = [
            _row(security_name="Cash", asset_class="cash", isin=None, weight=0.99),
            _row(security_name="Cash equivalents", asset_class="cash", isin=None, weight=0.01),
        ]
        frame = _with_security_id(_frame(rows), ["CASH:CAD", "CASH:CAD"])
        findings = validate_file(
            _parsed(frame),
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        assert _error_checks(findings) == {"duplicate_security"}


class TestFutureAsOf:
    def test_same_toronto_day_passes(self, source_factory: Callable[..., Source]) -> None:
        parsed = _parsed(
            _single_row_frame(1.0),
            as_of=date(2026, 11, 3),
            fetched_at=datetime(2026, 11, 3, 12, 0, tzinfo=UTC),
        )
        findings = validate_file(
            parsed,
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        assert "future_as_of" not in _checks(findings)

    def test_previous_toronto_day_fails(self, source_factory: Callable[..., Source]) -> None:
        parsed = _parsed(
            _single_row_frame(1.0),
            as_of=date(2026, 11, 3),
            fetched_at=datetime(2026, 11, 3, 2, 0, tzinfo=UTC),
        )
        findings = validate_file(
            parsed,
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        errors = [f for f in findings if f.check == "future_as_of"]
        assert len(errors) == 1
        assert errors[0].severity == "error"
        assert "as_of_date=2026-11-03" in errors[0].detail
        assert "fetch date 2026-11-02" in errors[0].detail


class TestNumericAndUnit:
    def test_null_and_non_finite_weights(self, source_factory: Callable[..., Source]) -> None:
        rows = [
            _row(weight=None),
            _row(
                security_name="Other Corp.", isin=None, security_ticker="OTH", weight=float("nan")
            ),
        ]
        frame = _with_security_id(_frame(rows), ["ISIN:CA600000AA15", None])
        findings = validate_file(
            _parsed(frame),
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        numeric = [f for f in findings if f.check == "numeric"]
        assert len(numeric) == 1
        assert numeric[0].severity == "error"
        assert "1 null" in numeric[0].detail
        assert "1 non-finite" in numeric[0].detail

    def test_unit_contradiction_note(self, source_factory: Callable[..., Source]) -> None:
        note = (
            "declared unit fraction but weights total 100.0000 "
            "(expected ~1.00 within 1.0%); total matches unit percent"
        )
        parsed = _parsed(
            _single_row_frame(1.0), notes=["declared_unit=fraction", f"unit_check={note}"]
        )
        findings = validate_file(
            parsed,
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        assert _checks(findings) == ["unit_contradiction"]
        assert findings[0].severity == "error"
        assert findings[0].detail == note


class TestNegativeWeight:
    def test_security_row_warns_and_cash_row_does_not(
        self, source_factory: Callable[..., Source]
    ) -> None:
        rows = [
            _row(security_name="Short Alpha Corp.", isin=None, security_ticker="SHT", weight=-0.02),
            _row(
                security_name="Cash and Cash Equivalents",
                asset_class="cash",
                isin=None,
                security_ticker=None,
                exchange=None,
                weight=-0.01,
            ),
        ]
        frame = _with_security_id(_frame(rows), ["TKR:XTSE:SHT", "CASH:CAD"])
        findings = validate_file(
            _parsed(frame),
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        warnings = [f for f in findings if f.check == "negative_weight"]
        assert len(warnings) == 1
        assert warnings[0].severity == "warning"
        assert "-0.0200" in warnings[0].detail
        assert "Short Alpha Corp." in warnings[0].detail
