"""Tests for the validation config, QA finding types and schema check."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest
from pydantic import ValidationError

from etfh.registry import Source
from etfh.types import AssetClass, ParsedFile, RawFile
from etfh.validate import QaFinding, ValidationConfig, is_excluded, validate_file

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "validation.yaml"
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
        "weight": 0.985,
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


def _parsed(frame: pl.DataFrame) -> ParsedFile:
    raw = RawFile(
        source_id="syn_csv",
        issuer="synthetic",
        fund_ticker="SYNA",
        as_of_hint=date(2026, 10, 29),
        fetched_at=datetime(2026, 10, 29, 22, 0, tzinfo=UTC),
        url="synthetic://fixtures/SYNA.csv",
        path=Path("tests/fixtures/raw_synthetic/synthetic/SYNA/2026-10-29/20261029T220000Z.csv"),
        sha256="a" * 64,
        content_type="text/csv",
        ext="csv",
    )
    return ParsedFile(raw=raw, as_of_date=date(2026, 10, 29), published_at=None, frame=frame)


def _checks(findings: list[QaFinding]) -> list[str]:
    return [finding.check for finding in findings]


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
