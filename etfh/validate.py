"""Per-file validation checks and their QA findings.

Every check is a pure function over a :class:`~etfh.types.ParsedFile` whose
frame already carries the ``security_id`` column assigned by the security
master. The checks return :class:`QaFinding` records; the build writes them to
``qa_events`` and excludes the file from derived tables when any finding has
severity ``error``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

import polars as pl
import yaml
from pandera.errors import SchemaError, SchemaErrors
from pydantic import BaseModel, ConfigDict, Field, model_validator

from etfh.registry import Source
from etfh.schema import ParsedHoldings
from etfh.types import AssetClass, ParsedFile

__all__ = [
    "QaFinding",
    "Severity",
    "ValidationConfig",
    "is_excluded",
    "validate_file",
]

Severity = Literal["error", "warning", "info"]


class ValidationConfig(BaseModel):
    """Tolerances for the per-file validation checks.

    Loaded from ``config/validation.yaml``; every asset class must have a
    tolerance and no tolerance may be negative, so a malformed config fails
    loudly at load time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    weight_sum_tolerance: dict[AssetClass, float]
    qpd_total_tolerance: float = Field(default=0.005, ge=0)
    future_as_of_days: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _require_all_asset_classes(self) -> Self:
        missing = [
            asset_class.value
            for asset_class in AssetClass
            if asset_class not in self.weight_sum_tolerance
        ]
        if missing:
            raise ValueError(f"weight_sum_tolerance missing asset classes: {', '.join(missing)}")
        for asset_class, tolerance in self.weight_sum_tolerance.items():
            if tolerance < 0:
                raise ValueError(
                    f"weight_sum_tolerance[{asset_class.value}] must be >= 0, got {tolerance}"
                )
        return self

    @classmethod
    def load(cls, path: Path) -> ValidationConfig:
        """Load and validate a YAML config, failing loudly on bad records."""
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a mapping of validation settings")
        return cls.model_validate(data)


@dataclass(frozen=True, slots=True)
class QaFinding:
    """One validation finding for one raw file."""

    check: str
    severity: Severity
    detail: str


def is_excluded(findings: Sequence[QaFinding]) -> bool:
    """True iff any finding is an error, so the file must not enter derived tables."""
    return any(finding.severity == "error" for finding in findings)


def _schema_failure_detail(error: SchemaError | SchemaErrors) -> str:
    cases = getattr(error, "failure_cases", None)
    if cases is None or cases.is_empty():
        return f"pandera schema validation failed: {error}"
    pairs = (
        cases.select("check", "column", "failure_case")
        .unique()
        .sort(["check", "column", "failure_case"])
        .iter_rows()
    )
    rendered_parts: list[str] = []
    for check, column, failure_case in pairs:
        label = f"{check} [{'?' if column is None else column}]"
        if isinstance(failure_case, str) and failure_case not in label:
            label += f": {failure_case}"
        rendered_parts.append(label)
    return f"{cases.height} schema failure case(s): {'; '.join(rendered_parts)}"


def _check_schema(parsed: ParsedFile) -> list[QaFinding]:
    """The frame must match ``ParsedHoldings`` plus a nullable Utf8 ``security_id``."""
    frame = parsed.frame
    findings: list[QaFinding] = []
    security_id_dtype = frame.schema.get("security_id")
    if security_id_dtype is None:
        findings.append(
            QaFinding(
                "schema",
                "error",
                "security_id column missing; run the security master before validation",
            )
        )
    elif security_id_dtype != pl.String:
        findings.append(
            QaFinding(
                "schema",
                "error",
                f"security_id column has dtype {security_id_dtype}, expected String",
            )
        )
    try:
        ParsedHoldings.validate(frame, lazy=True)
    except (SchemaError, SchemaErrors) as error:
        findings.append(QaFinding("schema", "error", _schema_failure_detail(error)))
    return findings


def validate_file(
    parsed: ParsedFile,
    *,
    source: Source,
    fund_asset_class: AssetClass,
    config: ValidationConfig,
) -> list[QaFinding]:
    """Run all applicable checks over one parsed file, in a fixed order."""
    findings: list[QaFinding] = []
    findings.extend(_check_schema(parsed))
    return findings
