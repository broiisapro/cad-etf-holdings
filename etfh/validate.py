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
from datetime import timedelta
from pathlib import Path
from typing import Literal, Self, cast
from zoneinfo import ZoneInfo

import polars as pl
import yaml
from pandera.errors import SchemaError, SchemaErrors
from pydantic import BaseModel, ConfigDict, Field, model_validator

from etfh.registry import Source
from etfh.schema import ParsedHoldings
from etfh.types import AssetClass, FileFormat, ParsedFile, SourceKind

__all__ = [
    "QaFinding",
    "Severity",
    "ValidationConfig",
    "is_excluded",
    "validate_file",
]

Severity = Literal["error", "warning", "info"]

_TORONTO = ZoneInfo("America/Toronto")
_NON_SECURITY_ASSET_CLASSES = ("cash", "fx", "derivative")
_NON_SECURITY_ID_PREFIXES = ("CASH:", "DERIV:")
_UNIT_CHECK_PREFIX = "unit_check="
_UNIT_SUGGESTION_MARKER = "; total matches unit "


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


def _weight_dtype(frame: pl.DataFrame) -> pl.DataType | None:
    dtype = frame.schema.get("weight")
    if dtype is None or not dtype.is_numeric():
        return None
    return dtype


def _finite_weight_sum(frame: pl.DataFrame) -> float:
    weights = frame["weight"].drop_nulls()
    return float(weights.filter(weights.is_finite()).sum())


def _is_quarterly(source: Source) -> bool:
    """True for quarterly portfolio disclosures (real or the syn_pdf fixture)."""
    return source.kind is SourceKind.quarterly_disclosure or source.format is FileFormat.pdf


def _note_value(notes: Sequence[str], key: str) -> str | None:
    prefix = f"{key}="
    for note in notes:
        if note.startswith(prefix):
            return note[len(prefix) :]
    return None


def _check_numeric(parsed: ParsedFile) -> list[QaFinding]:
    """Weights must be present and finite."""
    if _weight_dtype(parsed.frame) is None:
        return []
    weights = parsed.frame["weight"]
    null_count = int(weights.null_count())
    non_finite_count = int((~weights.drop_nulls().is_finite()).sum())
    if null_count == 0 and non_finite_count == 0:
        return []
    return [
        QaFinding(
            "numeric",
            "error",
            f"weight values: {null_count} null, {non_finite_count} non-finite "
            f"(of {weights.len()} rows)",
        )
    ]


def _check_weight_sum(
    parsed: ParsedFile,
    *,
    source: Source,
    fund_asset_class: AssetClass,
    config: ValidationConfig,
) -> list[QaFinding]:
    """Daily files must sum to 1 within the fund's asset-class tolerance (D38)."""
    if _is_quarterly(source) or _weight_dtype(parsed.frame) is None:
        return []
    tolerance = config.weight_sum_tolerance[fund_asset_class]
    total = _finite_weight_sum(parsed.frame)
    delta = abs(total - 1.0)
    if delta <= tolerance:
        return []
    return [
        QaFinding(
            "weight_sum",
            "error",
            f"sum={total:.4f} expected=1±{tolerance:g} "
            f"(asset_class={fund_asset_class.value}, delta={delta:.4f})",
        )
    ]


def _check_qpd_stated_total(
    parsed: ParsedFile,
    *,
    source: Source,
    config: ValidationConfig,
) -> list[QaFinding]:
    """Quarterly disclosures are checked against the document's stated total (D17, D38)."""
    if not _is_quarterly(source) or _weight_dtype(parsed.frame) is None:
        return []
    raw_stated = _note_value(parsed.notes, "stated_total_fraction")
    if raw_stated is None:
        return [
            QaFinding(
                "qpd_stated_total",
                "warning",
                "stated_total_fraction note missing; table sum was not cross-checked "
                "against the document total",
            )
        ]
    try:
        stated = float(raw_stated)
    except ValueError:
        return [
            QaFinding(
                "qpd_stated_total",
                "error",
                f"stated_total_fraction note is not a number: {raw_stated!r}",
            )
        ]
    total = _finite_weight_sum(parsed.frame)
    delta = abs(total - stated)
    if delta <= config.qpd_total_tolerance:
        return []
    return [
        QaFinding(
            "qpd_stated_total",
            "error",
            f"sum={total:.4f} stated_total={stated:.4f} "
            f"tolerance={config.qpd_total_tolerance:g} (delta={delta:.4f})",
        )
    ]


def _check_duplicate_security(parsed: ParsedFile) -> list[QaFinding]:
    """No security id may appear twice; CASH rows of different currencies have different ids."""
    frame = parsed.frame
    if frame.schema.get("security_id") != pl.String:
        return []
    positions: dict[str, list[int]] = {}
    for index, security_id in enumerate(frame["security_id"].to_list()):
        if security_id is None:
            continue
        positions.setdefault(security_id, []).append(index)
    findings: list[QaFinding] = []
    for security_id in sorted(positions):
        rows = positions[security_id]
        if len(rows) > 1:
            rendered_rows = ", ".join(str(row) for row in rows)
            findings.append(
                QaFinding(
                    "duplicate_security",
                    "error",
                    f"security_id={security_id} appears {len(rows)} times (rows {rendered_rows})",
                )
            )
    return findings


def _check_future_as_of(parsed: ParsedFile, *, config: ValidationConfig) -> list[QaFinding]:
    """The as-of date may not be after the fetch date in America/Toronto (D30)."""
    fetch_date = parsed.raw.fetched_at.astimezone(_TORONTO).date()
    limit = fetch_date + timedelta(days=config.future_as_of_days)
    if parsed.as_of_date <= limit:
        return []
    return [
        QaFinding(
            "future_as_of",
            "error",
            f"as_of_date={parsed.as_of_date.isoformat()} is after fetch date "
            f"{fetch_date.isoformat()} in America/Toronto "
            f"(future_as_of_days={config.future_as_of_days})",
        )
    ]


def _check_unit_contradiction(parsed: ParsedFile) -> list[QaFinding]:
    """A unit contradiction is an error only when the other unit actually fits.

    Parsers record ``unit_check=<UnitCheck.detail()>``; that text appends
    ``"; total matches unit <other>"`` exactly when the other unit fits the
    observed total. A total that fits neither unit is a plain weight-sum
    problem, already reported by ``weight_sum``, so it is not repeated here
    (wave-02 review #9).
    """
    findings: list[QaFinding] = []
    for note in parsed.notes:
        if not note.startswith(_UNIT_CHECK_PREFIX):
            continue
        detail = note[len(_UNIT_CHECK_PREFIX) :]
        if _UNIT_SUGGESTION_MARKER in detail:
            findings.append(QaFinding("unit_contradiction", "error", detail))
    return findings


def _check_negative_weight(parsed: ParsedFile) -> list[QaFinding]:
    """Negative weights on security rows are a warning; cash/fx/derivative rows may be negative."""
    frame = parsed.frame
    if _weight_dtype(frame) is None or not {"asset_class", "security_name"}.issubset(frame.columns):
        return []
    negative = (frame["weight"] < 0).fill_null(False)
    non_security = frame["asset_class"].str.to_lowercase().is_in(_NON_SECURITY_ASSET_CLASSES)
    if frame.schema.get("security_id") == pl.String:
        ids = frame["security_id"].fill_null("")
        for prefix in _NON_SECURITY_ID_PREFIXES:
            non_security = non_security | ids.str.starts_with(prefix)
    flagged = frame.filter(negative & ~non_security)
    if flagged.is_empty():
        return []
    minimum = float(cast(float, flagged["weight"].min()))
    names = flagged["security_name"].unique().sort().to_list()[:3]
    return [
        QaFinding(
            "negative_weight",
            "warning",
            f"{flagged.height} non-cash/fx/derivative row(s) have negative weights "
            f"(min={minimum:.4f}): {', '.join(names)}",
        )
    ]


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
    findings.extend(_check_numeric(parsed))
    findings.extend(
        _check_weight_sum(parsed, source=source, fund_asset_class=fund_asset_class, config=config)
    )
    findings.extend(_check_qpd_stated_total(parsed, source=source, config=config))
    findings.extend(_check_duplicate_security(parsed))
    findings.extend(_check_future_as_of(parsed, config=config))
    findings.extend(_check_unit_contradiction(parsed))
    findings.extend(_check_negative_weight(parsed))
    return findings
