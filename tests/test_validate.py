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
from etfh.units import WeightUnit, check_unit
from etfh.validate import (
    TOLERANCE_CAP,
    QaFinding,
    ValidationConfig,
    is_excluded,
    tolerance_for,
    validate_file,
    validate_metrics,
)
from tests._validate_helpers import (
    load_manifest,
    metrics_frame,
    metrics_row,
    parse_fixture,
    parsed_metrics,
    raw_files,
)

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


def _weighted_frame(weights: list[float]) -> pl.DataFrame:
    """A frame of fictional, unmatched rows carrying exactly ``weights``."""
    rows = [
        _row(
            security_name=f"Fictional Holding {index:03d}",
            security_ticker=None,
            exchange=None,
            isin=None,
            cusip=None,
            weight=weight,
        )
        for index, weight in enumerate(weights)
    ]
    return _with_security_id(_frame(rows), [None] * len(weights))


def _weights_summing_to(total: float, n_rows: int) -> list[float]:
    """``n_rows`` weights (each <= 1.0) whose sum is ``total``."""
    baseline = [0.01] * (n_rows - 2)
    remaining = total - sum(baseline)
    return [*baseline, remaining / 2.0, remaining / 2.0]


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
    single_row = _weighted_frame([1.0])
    for asset_class in AssetClass:
        tolerance = tolerance_for(single_row, [], asset_class, _CONFIG)
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
        assert config.aum_tolerance == 0.02

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

    def test_aum_tolerance_defaults_and_rejects_negative(self, tmp_path: Path) -> None:
        base = (
            "weight_sum_tolerance:\n"
            "  equity: 0.005\n"
            "  fixed_income: 0.01\n"
            "  balanced: 0.02\n"
            "  commodity: 0.02\n"
            "  other: 0.02\n"
        )
        default_path = tmp_path / "default.yaml"
        default_path.write_text(base, encoding="utf-8")
        assert ValidationConfig.load(default_path).aum_tolerance == 0.02
        negative_path = tmp_path / "negative.yaml"
        negative_path.write_text(base + "aum_tolerance: -0.01\n", encoding="utf-8")
        with pytest.raises(ValidationError, match="aum_tolerance"):
            ValidationConfig.load(negative_path)


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


class TestRoundingTolerance:
    """D56: tolerance = asset-class floor + printed rounding bound, capped."""

    def test_tolerance_adds_the_rounding_bound_to_the_floor(self) -> None:
        frame = _weighted_frame([0.01] * 100)
        tolerance = tolerance_for(
            frame,
            ["declared_unit=percent", "weight_decimals=2"],
            AssetClass.equity,
            _CONFIG,
        )
        assert tolerance == pytest.approx(0.005 + 100 * 0.00005)
        assert tolerance == pytest.approx(0.01)

    def test_tolerance_is_capped_at_two_points(self) -> None:
        frame = _weighted_frame([0.001] * 1000)
        tolerance = tolerance_for(
            frame,
            ["declared_unit=percent", "weight_decimals=2"],
            AssetClass.equity,
            _CONFIG,
        )
        assert tolerance == TOLERANCE_CAP
        assert tolerance == 0.02

    def test_tolerance_never_drops_below_the_asset_class_floor(self) -> None:
        frame = _weighted_frame([1.0])
        assert tolerance_for(frame, [], AssetClass.equity, _CONFIG) == pytest.approx(
            0.005 + 0.00005
        )
        assert tolerance_for(frame, [], AssetClass.fixed_income, _CONFIG) == pytest.approx(
            0.01 + 0.00005
        )
        assert tolerance_for(frame, [], AssetClass.balanced, _CONFIG) == TOLERANCE_CAP

    def test_missing_or_bad_weight_decimals_falls_back_to_the_unit_default(self) -> None:
        frame = _weighted_frame([0.01] * 10)
        expected = 0.005 + 10 * 0.00005
        for notes in (
            ["declared_unit=percent"],
            ["declared_unit=percent", "weight_decimals=n/a"],
            ["declared_unit=percent", "weight_decimals=-1"],
        ):
            assert tolerance_for(frame, notes, AssetClass.equity, _CONFIG) == pytest.approx(
                expected
            )

    def test_100_rows_of_two_decimal_percents_summing_to_100_51_pass(
        self, source_factory: Callable[..., Source]
    ) -> None:
        weights = [0.01] * 99 + [0.0151]
        assert sum(weights) == pytest.approx(1.0051)
        findings = validate_file(
            _parsed(
                _weighted_frame(weights),
                notes=["declared_unit=percent", "weight_decimals=2"],
            ),
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        assert findings == []

    def test_rounding_tolerance_boundary_both_sides(
        self, source_factory: Callable[..., Source]
    ) -> None:
        tolerance = 0.01
        below = 1.0 + tolerance - 1e-9
        above = 1.0 + tolerance + 1e-9
        for total, expect_error in ((below, False), (above, True)):
            findings = validate_file(
                _parsed(
                    _weighted_frame(_weights_summing_to(total, 100)),
                    notes=["declared_unit=percent", "weight_decimals=2"],
                ),
                source=source_factory(),
                fund_asset_class=AssetClass.equity,
                config=_CONFIG,
            )
            assert ("weight_sum" in _checks(findings)) is expect_error

    def test_a_quarterly_file_summing_to_307_is_over_one(
        self, source_factory: Callable[..., Source]
    ) -> None:
        parsed = _parsed(
            _weighted_frame([1.0, 1.0, 1.0, 0.07]),
            notes=[
                "declared_unit=percent",
                "weight_decimals=2",
                "stated_total_fraction=3.07",
                "partial_list=true",
            ],
        )
        findings = validate_file(
            parsed,
            source=_SOURCES["syn_pdf"],
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        over_one = [finding for finding in findings if finding.check == "weight_sum_over_one"]
        assert len(over_one) == 1
        assert over_one[0].severity == "error"
        assert "sum=3.0700" in over_one[0].detail
        assert "exceeds 1+tolerance" in over_one[0].detail
        assert "weight_sum" not in _checks(findings)
        assert is_excluded(findings)

    def test_a_partial_daily_file_over_one_is_still_an_error(
        self, source_factory: Callable[..., Source]
    ) -> None:
        findings = validate_file(
            _parsed(
                _weighted_frame(_weights_summing_to(1.03, 100)),
                notes=[
                    "declared_unit=percent",
                    "weight_decimals=2",
                    "partial_list=true",
                ],
            ),
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        assert _error_checks(findings) == {"weight_sum_over_one"}
        assert any(
            finding.check == "weight_sum" and finding.severity == "warning" for finding in findings
        )


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
            "sum=0.8500" in detail and "expected=1±0.006" in detail and "rows=20" in detail
            for detail in details["SYNC"]
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
    def test_synd_style_double_listing_is_an_error(
        self, source_factory: Callable[..., Source]
    ) -> None:
        rows = [
            _row(weight=0.5),
            _row(weight=0.5, isin=None, cusip="600000AA1"),
        ]
        frame = _with_security_id(_frame(rows), ["ISIN:CA600000AA15", "ISIN:CA600000AA15"])
        findings = validate_file(
            _parsed(frame),
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        errors = [f for f in findings if f.check == "duplicate_security"]
        assert len(errors) == 1
        assert errors[0].severity == "error"
        assert "different identifier forms: CUSIP, ISIN" in errors[0].detail

    def test_same_isin_on_two_lines_is_information_not_an_error(
        self, source_factory: Callable[..., Source]
    ) -> None:
        rows = [
            _row(weight=0.5, shares=1614.0),
            _row(weight=0.5, shares=12435.0),
        ]
        frame = _with_security_id(_frame(rows), ["ISIN:CA600000AA15", "ISIN:CA600000AA15"])
        findings = validate_file(
            _parsed(frame),
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        infos = [f for f in findings if f.check == "duplicate_lines"]
        assert len(infos) == 1
        assert infos[0].severity == "info"
        assert "ISIN:CA600000AA15 appears 2 times (rows 0, 1)" in infos[0].detail
        assert "same identifier form (ISIN), rows kept separate" in infos[0].detail
        assert not is_excluded(findings)

    def test_two_identical_rows_are_an_error(self, source_factory: Callable[..., Source]) -> None:
        rows = [_row(weight=0.5, shares=100.0), _row(weight=0.5, shares=100.0)]
        frame = _with_security_id(_frame(rows), ["ISIN:CA600000AA15", "ISIN:CA600000AA15"])
        findings = validate_file(
            _parsed(frame),
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        errors = [f for f in findings if f.check == "duplicate_security"]
        assert len(errors) == 1
        assert errors[0].severity == "error"
        assert "identical row(s): 0, 1" in errors[0].detail

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

    def test_same_cash_currency_twice_is_a_repeated_line(
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
        assert _error_checks(findings) == set()
        infos = [f for f in findings if f.check == "duplicate_lines"]
        assert len(infos) == 1
        assert "same identifier form (CASH)" in infos[0].detail

    def test_identical_cash_rows_are_an_error(self, source_factory: Callable[..., Source]) -> None:
        rows = [
            _row(security_name="Cash", asset_class="cash", isin=None, weight=0.5),
            _row(security_name="Cash", asset_class="cash", isin=None, weight=0.5),
        ]
        frame = _with_security_id(_frame(rows), ["CASH:CAD", "CASH:CAD"])
        findings = validate_file(
            _parsed(frame),
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        errors = [f for f in findings if f.check == "duplicate_security"]
        assert len(errors) == 1
        assert "identical row(s): 0, 1" in errors[0].detail


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
        note = check_unit(pl.Series("weight", [100.0]), WeightUnit.fraction).detail()
        assert "; total matches unit percent" in note
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

    def test_bad_sum_that_fits_neither_unit_is_only_weight_sum(
        self, source_factory: Callable[..., Source]
    ) -> None:
        note = check_unit(pl.Series("weight", [0.85]), WeightUnit.fraction).detail()
        assert "; total matches unit" not in note
        parsed = _parsed(
            _single_row_frame(0.85), notes=["declared_unit=fraction", f"unit_check={note}"]
        )
        findings = validate_file(
            parsed,
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        assert _checks(findings) == ["weight_sum"]
        assert findings[0].severity == "error"
        assert "sum=0.8500" in findings[0].detail


class TestPartialList:
    def test_partial_daily_file_weight_sum_is_a_warning(
        self, source_factory: Callable[..., Source]
    ) -> None:
        parsed = _parsed(
            _single_row_frame(0.85),
            notes=["declared_unit=fraction", "partial_list=true"],
        )
        findings = validate_file(
            parsed,
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        warnings = [f for f in findings if f.check == "weight_sum"]
        assert len(warnings) == 1
        assert warnings[0].severity == "warning"
        assert "sum=0.8500" in warnings[0].detail
        assert "partial_list=true" in warnings[0].detail
        assert not is_excluded(findings)

    def test_partial_daily_file_within_tolerance_records_nothing(
        self, source_factory: Callable[..., Source]
    ) -> None:
        parsed = _parsed(
            _single_row_frame(1.0),
            notes=["declared_unit=fraction", "partial_list=true"],
        )
        findings = validate_file(
            parsed,
            source=source_factory(),
            fund_asset_class=AssetClass.equity,
            config=_CONFIG,
        )
        assert findings == []


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


class TestValidateMetrics:
    """Fund-metrics checks (interfaces wave 06 §4, D66)."""

    def _findings(
        self,
        frame: pl.DataFrame,
        source_factory: Callable[..., Source],
        *,
        as_of: date | None = None,
        fetched_at: datetime | None = None,
        config: ValidationConfig = _CONFIG,
    ) -> list[QaFinding]:
        return validate_metrics(
            parsed_metrics(frame, as_of=as_of, fetched_at=fetched_at),
            source=source_factory("syn_metrics"),
            config=config,
        )

    def _only(self, findings: list[QaFinding], check: str) -> list[QaFinding]:
        return [finding for finding in findings if finding.check == check]

    def test_clean_consistent_file_has_no_findings(
        self, source_factory: Callable[..., Source]
    ) -> None:
        assert self._findings(metrics_frame(), source_factory) == []

    def test_a_history_of_dates_is_not_a_duplicate(
        self, source_factory: Callable[..., Source]
    ) -> None:
        rows = [
            metrics_row(as_of_date=date(2026, 10, 27)),
            metrics_row(as_of_date=date(2026, 10, 28)),
            metrics_row(as_of_date=date(2026, 10, 29)),
        ]
        assert self._findings(metrics_frame(rows), source_factory) == []

    def test_missing_required_column_is_a_schema_error(
        self, source_factory: Callable[..., Source]
    ) -> None:
        findings = self._findings(metrics_frame().drop("aum"), source_factory)
        assert [finding.check for finding in findings] == ["schema"]
        assert findings[0].severity == "error"
        assert "aum" in findings[0].detail
        assert "syn_metrics" in findings[0].detail

    def test_bad_file_hash_is_a_schema_error(self, source_factory: Callable[..., Source]) -> None:
        frame = metrics_frame([metrics_row(raw_file_sha256="not-a-sha")])
        findings = self._findings(frame, source_factory)
        assert [finding.check for finding in findings] == ["schema"]
        assert findings[0].severity == "error"

    def test_missing_date_column_is_a_schema_error_without_crashing(
        self, source_factory: Callable[..., Source]
    ) -> None:
        findings = self._findings(metrics_frame().drop("as_of_date"), source_factory)
        assert [finding.check for finding in findings] == ["schema"]
        assert "as_of_date" in findings[0].detail

    def test_nav_must_be_positive_and_finite(self, source_factory: Callable[..., Source]) -> None:
        rows = [
            metrics_row(nav_per_unit=0.0),
            metrics_row(nav_per_unit=-3.5),
            metrics_row(nav_per_unit=float("nan")),
            metrics_row(nav_per_unit=12.0),
        ]
        findings = self._only(self._findings(metrics_frame(rows), source_factory), "nav_positive")
        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert "3 of 4 nav_per_unit" in findings[0].detail

    def test_null_nav_is_allowed(self, source_factory: Callable[..., Source]) -> None:
        frame = metrics_frame([metrics_row(nav_per_unit=None)])
        assert self._findings(frame, source_factory) == []

    def test_shares_must_be_nonnegative_and_finite(
        self, source_factory: Callable[..., Source]
    ) -> None:
        rows = [
            metrics_row(shares_outstanding=-1.0),
            metrics_row(shares_outstanding=float("-inf")),
        ]
        findings = self._only(
            self._findings(metrics_frame(rows), source_factory), "shares_nonnegative"
        )
        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert "2 of 2 shares_outstanding" in findings[0].detail

    def test_zero_shares_and_null_shares_are_allowed(
        self, source_factory: Callable[..., Source]
    ) -> None:
        rows = [
            metrics_row(shares_outstanding=0.0, aum=0.0),
            metrics_row(as_of_date=date(2026, 10, 28), shares_outstanding=None),
        ]
        assert self._findings(metrics_frame(rows), source_factory) == []

    def test_aum_gap_is_a_warning_not_an_error(self, source_factory: Callable[..., Source]) -> None:
        frame = metrics_frame([metrics_row(aum=25_600_000.0)])
        findings = self._only(self._findings(frame, source_factory), "aum_consistency")
        assert len(findings) == 1
        assert findings[0].severity == "warning"
        assert not is_excluded(self._findings(frame, source_factory))
        assert "aum=25600000.0000" in findings[0].detail
        assert "nav*shares=25000000.0000" in findings[0].detail

    def test_aum_gap_within_tolerance_is_silent(
        self, source_factory: Callable[..., Source]
    ) -> None:
        frame = metrics_frame([metrics_row(aum=25_400_000.0)])
        assert self._only(self._findings(frame, source_factory), "aum_consistency") == []

    def test_aum_tolerance_boundary_is_strict(self, source_factory: Callable[..., Source]) -> None:
        config = _CONFIG.model_copy(update={"aum_tolerance": 0.5})
        at_boundary = metrics_frame(
            [metrics_row(nav_per_unit=1.0, shares_outstanding=50.0, aum=100.0)]
        )
        assert (
            self._only(
                self._findings(at_boundary, source_factory, config=config), "aum_consistency"
            )
            == []
        )
        above = metrics_frame([metrics_row(nav_per_unit=1.0, shares_outstanding=49.0, aum=100.0)])
        warnings = self._only(
            self._findings(above, source_factory, config=config), "aum_consistency"
        )
        assert len(warnings) == 1
        assert warnings[0].severity == "warning"
        assert "relative=51.00%" in warnings[0].detail

    def test_zero_aum_with_positive_nav_times_shares_warns(
        self, source_factory: Callable[..., Source]
    ) -> None:
        frame = metrics_frame([metrics_row(nav_per_unit=10.0, shares_outstanding=100.0, aum=0.0)])
        findings = self._only(self._findings(frame, source_factory), "aum_consistency")
        assert len(findings) == 1
        assert findings[0].severity == "warning"
        assert "undefined (aum=0)" in findings[0].detail

    def test_zero_aum_with_zero_shares_is_consistent(
        self, source_factory: Callable[..., Source]
    ) -> None:
        frame = metrics_frame([metrics_row(shares_outstanding=0.0, aum=0.0)])
        assert self._only(self._findings(frame, source_factory), "aum_consistency") == []

    def test_missing_figure_skips_aum_check(self, source_factory: Callable[..., Source]) -> None:
        frame = metrics_frame([metrics_row(aum=None)])
        assert self._only(self._findings(frame, source_factory), "aum_consistency") == []

    def test_duplicate_fund_date_is_an_error(self, source_factory: Callable[..., Source]) -> None:
        rows = [metrics_row(), metrics_row(shares_outstanding=2_000_000.0, aum=50_000_000.0)]
        findings = self._only(self._findings(metrics_frame(rows), source_factory), "duplicate_date")
        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert "SYNM 2026-10-29 x2" in findings[0].detail
        assert is_excluded(self._findings(metrics_frame(rows), source_factory))

    def test_same_date_for_two_funds_is_not_a_duplicate(
        self, source_factory: Callable[..., Source]
    ) -> None:
        rows = [metrics_row(), metrics_row(fund_ticker="SYNQ")]
        assert (
            self._only(self._findings(metrics_frame(rows), source_factory), "duplicate_date") == []
        )

    def test_future_as_of_compares_in_toronto(self, source_factory: Callable[..., Source]) -> None:
        frame = metrics_frame([metrics_row(as_of_date=date(2026, 11, 3))])
        findings = self._only(
            self._findings(
                frame,
                source_factory,
                as_of=date(2026, 11, 3),
                fetched_at=datetime(2026, 11, 3, 2, 0, tzinfo=UTC),
            ),
            "future_as_of",
        )
        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert "as_of_date=2026-11-03" in findings[0].detail
        assert "fetch date 2026-11-02" in findings[0].detail

    def test_every_future_row_is_reported_not_only_the_latest(
        self, source_factory: Callable[..., Source]
    ) -> None:
        rows = [
            metrics_row(as_of_date=date(2026, 10, 28)),
            metrics_row(as_of_date=date(2026, 11, 3)),
            metrics_row(as_of_date=date(2026, 11, 4)),
        ]
        findings = self._only(
            self._findings(
                metrics_frame(rows),
                source_factory,
                as_of=date(2026, 11, 4),
                fetched_at=datetime(2026, 11, 3, 2, 0, tzinfo=UTC),
            ),
            "future_as_of",
        )
        assert len(findings) == 1
        assert "as_of_date=2026-11-03, 2026-11-04" in findings[0].detail
        assert "2026-10-28" not in findings[0].detail

    def test_future_as_of_day_tolerance(self, source_factory: Callable[..., Source]) -> None:
        frame = metrics_frame([metrics_row(as_of_date=date(2026, 11, 3))])
        config = _CONFIG.model_copy(update={"future_as_of_days": 1})
        findings = self._findings(
            frame,
            source_factory,
            as_of=date(2026, 11, 3),
            fetched_at=datetime(2026, 11, 3, 2, 0, tzinfo=UTC),
            config=config,
        )
        assert findings == []
