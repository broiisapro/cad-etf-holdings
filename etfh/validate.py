"""Per-file validation checks and their QA findings.

Every check is a pure function over a :class:`~etfh.types.ParsedFile` whose
frame already carries the ``security_id`` column assigned by the security
master. The checks return :class:`QaFinding` records; the build writes them to
``qa_events`` and excludes the file from derived tables when any finding has
severity ``error``.

Weight totals are checked with a rounding-aware tolerance (D56):
:func:`tolerance_for` starts from the fund asset class's tolerance in
``config/validation.yaml`` and adds the largest rounding error a sum of the
file's printed weights can carry (half the smallest printed weight unit per
row). The configured tolerance is a floor for genuine composition drift and the
rounding allowance is measurement error, so the two are added; the result is
capped at :data:`TOLERANCE_CAP` (0.02) so a damaged file can never hide behind
a large row count. Any file whose weights sum above ``1 + tolerance`` is an
error regardless of source kind or ``partial_list`` (``weight_sum_over_one``).

:func:`validate_metrics` applies the same finding contract to the additive
fund-metrics files (D66, FR-2.1): the frame must match
``etfh.schema.ParsedFundMetrics`` and every date must be plausible relative to
the fetch date. AUM is cross-checked against NAV times shares outstanding, but
only as a warning: issuers round the three figures independently and a mismatch
is evidence, not proof, of a parsing error. An error in a metrics file excludes
that metrics file only; holdings files are never affected (interfaces wave 06).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from importlib import import_module
from math import isfinite
from pathlib import Path
from typing import Literal, Self, cast
from zoneinfo import ZoneInfo

import pandera.polars as pa
import polars as pl
import yaml
from pandera.errors import SchemaError, SchemaErrors
from pydantic import BaseModel, ConfigDict, Field, model_validator

from etfh.registry import Source
from etfh.schema import ParsedHoldings
from etfh.types import AssetClass, FileFormat, ParsedFile, SourceKind
from etfh.units import WeightUnit

__all__ = [
    "QaFinding",
    "Severity",
    "TOLERANCE_CAP",
    "ValidationConfig",
    "is_excluded",
    "tolerance_for",
    "validate_file",
    "validate_metrics",
]

Severity = Literal["error", "warning", "info"]

#: Ceiling for the rounding-aware weight-sum tolerance (D56).
TOLERANCE_CAP = 0.02

_TORONTO = ZoneInfo("America/Toronto")
_NON_SECURITY_ASSET_CLASSES = ("cash", "fx", "derivative")
_NON_SECURITY_ID_PREFIXES = ("CASH:", "DERIV:")
_UNIT_CHECK_PREFIX = "unit_check="
_UNIT_SUGGESTION_MARKER = "; total matches unit "
_DECLARED_UNIT_KEY = "declared_unit"
_WEIGHT_DECIMALS_KEY = "weight_decimals"
# Interfaces wave 04 section 4: when a parser does not state the number of
# decimals it printed, assume the usual precision for its declared unit.
_DEFAULT_WEIGHT_DECIMALS = {WeightUnit.percent.value: 2, WeightUnit.fraction.value: 4}


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
    aum_tolerance: float = Field(default=0.02, ge=0)

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


def _weight_decimals(notes: Sequence[str]) -> int:
    """The decimals the file prints weights with (``weight_decimals=<n>``).

    Falls back to the usual precision for the declared unit (2 for percent,
    4 for fraction) when a parser does not record the note, and to the same
    defaults when the note is not a non-negative integer.
    """
    raw = _note_value(notes, _WEIGHT_DECIMALS_KEY)
    if raw is not None:
        try:
            decimals = int(raw)
        except ValueError:
            decimals = -1
        if decimals >= 0:
            return decimals
    unit = _note_value(notes, _DECLARED_UNIT_KEY)
    return _DEFAULT_WEIGHT_DECIMALS.get(unit or WeightUnit.fraction.value, 4)


def _smallest_weight_unit(notes: Sequence[str], decimals: int) -> float:
    """The smallest weight change one printed digit can express, as a fraction."""
    scale = 100.0 if _note_value(notes, _DECLARED_UNIT_KEY) == WeightUnit.percent.value else 1.0
    return 10.0**-decimals / scale


def tolerance_for(
    frame: pl.DataFrame,
    notes: Sequence[str],
    asset_class: AssetClass,
    config: ValidationConfig,
) -> float:
    """The allowed absolute deviation of a file's weight sum from 1.0 (D56).

    The configured per-asset-class tolerance is a floor for genuine
    composition drift. On top of it, a file that prints ``n`` rounded weights
    can sum up to ``n`` half-units away from its true total; that rounding
    bound is added, using the smallest unit inferred from the parser's
    ``declared_unit``/``weight_decimals`` notes. The result is capped at
    :data:`TOLERANCE_CAP` so a damaged file cannot hide behind a large row
    count.

    ``frame`` supplies the row count; ``notes`` the unit and printed
    precision. Both checks that compare a total with 1.0 use this function
    (``weight_sum`` and ``weight_sum_over_one``).
    """
    base = config.weight_sum_tolerance[asset_class]
    decimals = _weight_decimals(notes)
    rounding = frame.height * _smallest_weight_unit(notes, decimals) / 2.0
    return min(TOLERANCE_CAP, base + rounding)


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


def _tolerance_detail(parsed: ParsedFile, *, asset_class: AssetClass, tolerance: float) -> str:
    return (
        f"asset_class={asset_class.value}, rows={parsed.frame.height}, "
        f"weight_decimals={_weight_decimals(parsed.notes)}, tolerance={tolerance:g}"
    )


def _check_weight_sum(
    parsed: ParsedFile,
    *,
    source: Source,
    fund_asset_class: AssetClass,
    config: ValidationConfig,
) -> list[QaFinding]:
    """Daily files must sum to 1 within the rounding-aware tolerance (D38, D56).

    A file whose parser records ``partial_list=true`` publishes only part of
    the portfolio with evidence from the source; its weight sum becomes a
    warning instead of an error (D52) and the file stays included.
    """
    if _is_quarterly(source) or _weight_dtype(parsed.frame) is None:
        return []
    tolerance = tolerance_for(parsed.frame, parsed.notes, fund_asset_class, config)
    total = _finite_weight_sum(parsed.frame)
    delta = abs(total - 1.0)
    if delta <= tolerance:
        return []
    partial = _note_value(parsed.notes, "partial_list") == "true"
    detail = (
        f"sum={total:.4f} expected=1±{tolerance:g} "
        f"({_tolerance_detail(parsed, asset_class=fund_asset_class, tolerance=tolerance)}, "
        f"delta={delta:.4f})"
    )
    if partial:
        detail += "; partial_list=true: the source publishes part of the portfolio"
    return [QaFinding("weight_sum", "warning" if partial else "error", detail)]


def _check_weight_sum_over_one(
    parsed: ParsedFile,
    *,
    fund_asset_class: AssetClass,
    config: ValidationConfig,
) -> list[QaFinding]:
    """No file may sum above ``1 + tolerance``, partial or quarterly (D56).

    Quarterly disclosures are top-holdings lists and normally sum well below
    1.0, so this check is what catches a parser that mixes units or includes
    non-holding rows (for example a disclosure whose weights summed to 3.07).
    """
    if _weight_dtype(parsed.frame) is None:
        return []
    tolerance = tolerance_for(parsed.frame, parsed.notes, fund_asset_class, config)
    total = _finite_weight_sum(parsed.frame)
    limit = 1.0 + tolerance
    if total <= limit:
        return []
    return [
        QaFinding(
            "weight_sum_over_one",
            "error",
            f"sum={total:.4f} exceeds 1+tolerance={limit:.4f} "
            f"({_tolerance_detail(parsed, asset_class=fund_asset_class, tolerance=tolerance)})",
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


def _identifier_form(row: Mapping[str, object]) -> str:
    """The identifier evidence through which a row reached its ``security_id``.

    Used by the D51 duplicate rule: a repeated id whose rows arrived through
    the same identifier form is a repeated line (``info duplicate_lines``); a
    repeated id reached through different forms (ISIN vs CUSIP vs ticker, a
    double listing) is a genuine duplicate. Overridden or FIGI ids fall back
    to the strongest identifier the row itself provides.
    """
    security_id = str(row["security_id"])
    isin = row.get("isin")
    cusip = row.get("cusip")
    ticker = row.get("security_ticker")
    exchange = row.get("exchange")
    if security_id.startswith("CASH:"):
        return "CASH"
    if security_id.startswith("DERIV:"):
        return "DERIV"
    if isin is not None and security_id == f"ISIN:{isin}":
        return "ISIN"
    if cusip is not None and isin is None and security_id.startswith(("ISIN:", "CUSIP:")):
        return "CUSIP"
    if ticker is not None and exchange is not None and security_id == f"TKR:{exchange}:{ticker}":
        return "TKR"
    if isin is not None:
        return "ISIN"
    if cusip is not None:
        return "CUSIP"
    if ticker is not None and exchange is not None:
        return "TKR"
    return "OTHER"


def _check_duplicate_security(parsed: ParsedFile) -> list[QaFinding]:
    """Duplicate lines within one file follow D51.

    For every non-null ``security_id`` that appears on several rows:

    - ``error duplicate_security`` when the rows reached the id through
      different identifier forms or when two rows are identical in every
      parsed field (the file is excluded);
    - ``info duplicate_lines`` otherwise: the same identifier form on several
      lines with different quantities; the rows are kept separate, because
      analytics sums by ``security_id``.

    Cash rows of the same currency follow the same rule; rows of different
    currencies have different ids and never collide.
    """
    frame = parsed.frame
    if frame.schema.get("security_id") != pl.String or frame.is_empty():
        return []
    compare_columns = [name for name in frame.columns if name != "security_id"]
    identical_flags = frame.select(compare_columns).is_duplicated().to_list()
    rows_by_id: dict[str, list[int]] = {}
    forms_by_id: dict[str, set[str]] = {}
    for index, row in enumerate(frame.iter_rows(named=True)):
        security_id = row["security_id"]
        if security_id is None:
            continue
        rows_by_id.setdefault(str(security_id), []).append(index)
        forms_by_id.setdefault(str(security_id), set()).add(_identifier_form(row))
    findings: list[QaFinding] = []
    for security_id in sorted(rows_by_id):
        rows = rows_by_id[security_id]
        if len(rows) < 2:
            continue
        rendered_rows = ", ".join(str(row) for row in rows)
        prefix = f"security_id={security_id} appears {len(rows)} times (rows {rendered_rows})"
        forms = sorted(forms_by_id[security_id])
        identical_rows = [row for row in rows if identical_flags[row]]
        if len(forms) > 1:
            findings.append(
                QaFinding(
                    "duplicate_security",
                    "error",
                    f"{prefix}; different identifier forms: {', '.join(forms)}",
                )
            )
        elif identical_rows:
            rendered_identical = ", ".join(str(row) for row in identical_rows)
            findings.append(
                QaFinding(
                    "duplicate_security",
                    "error",
                    f"{prefix}; identical row(s): {rendered_identical}",
                )
            )
        else:
            findings.append(
                QaFinding(
                    "duplicate_lines",
                    "info",
                    f"{prefix}; same identifier form ({forms[0]}), rows kept separate",
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
    findings.extend(
        _check_weight_sum_over_one(parsed, fund_asset_class=fund_asset_class, config=config)
    )
    findings.extend(_check_qpd_stated_total(parsed, source=source, config=config))
    findings.extend(_check_duplicate_security(parsed))
    findings.extend(_check_future_as_of(parsed, config=config))
    findings.extend(_check_unit_contradiction(parsed))
    findings.extend(_check_negative_weight(parsed))
    return findings


# --- fund metrics (D66, interfaces wave 06 §4) ---------------------------------

_METRICS_NUMERIC_COLUMNS = ("nav_per_unit", "shares_outstanding", "aum")
_METRICS_EXAMPLES = 3


def _metrics_model() -> type[pa.DataFrameModel]:
    """The additive fund-metrics contract from :mod:`etfh.schema` (D66).

    Resolved on use: the holdings checks stay usable without the additive
    metrics models, and the schema module remains the single owner of the
    frozen column contract.
    """
    module = import_module("etfh.schema")
    return cast("type[pa.DataFrameModel]", module.__dict__["ParsedFundMetrics"])


def _numeric_column(frame: pl.DataFrame, name: str) -> pl.Series | None:
    """The column when it exists with a numeric dtype, else ``None``.

    A missing or mistyped column is already a ``schema`` error; the individual
    checks stay silent about it instead of raising.
    """
    dtype = frame.schema.get(name)
    if dtype is None or not dtype.is_numeric():
        return None
    return frame[name]


def _render_values(values: Sequence[float]) -> str:
    return ", ".join(f"{value:g}" for value in values)


def _check_metrics_schema(parsed: ParsedFile, *, source: Source) -> list[QaFinding]:
    """The frame must match ``ParsedFundMetrics`` (D66).

    Nullable fields are optional to Pandera, so the model's column set is
    checked explicitly first: a metrics frame that omits a contract column is a
    schema error even when the remaining rows are valid.
    """
    model = _metrics_model()
    expected = tuple(model.to_schema().columns)
    missing = [name for name in expected if name not in parsed.frame.columns]
    if missing:
        return [
            QaFinding(
                "schema",
                "error",
                f"fund metrics frame for source {source.id} is missing column(s): "
                f"{', '.join(missing)}",
            )
        ]
    try:
        model.validate(parsed.frame, lazy=True)
    except (SchemaError, SchemaErrors) as error:
        return [
            QaFinding(
                "schema",
                "error",
                f"fund metrics frame for source {source.id} does not match "
                f"ParsedFundMetrics: {_schema_failure_detail(error)}",
            )
        ]
    return []


def _check_nav_positive(parsed: ParsedFile) -> list[QaFinding]:
    """Every stated NAV per unit must be positive and finite; null stays allowed."""
    nav = _numeric_column(parsed.frame, "nav_per_unit")
    if nav is None:
        return []
    present = nav.drop_nulls()
    invalid = present.filter(~present.is_finite() | (present <= 0))
    if invalid.is_empty():
        return []
    examples = invalid.unique().sort().to_list()[:_METRICS_EXAMPLES]
    return [
        QaFinding(
            "nav_positive",
            "error",
            f"{invalid.len()} of {present.len()} nav_per_unit value(s) are not positive: "
            f"{_render_values(examples)}",
        )
    ]


def _check_shares_nonnegative(parsed: ParsedFile) -> list[QaFinding]:
    """Every stated share count must be non-negative and finite; null stays allowed."""
    shares = _numeric_column(parsed.frame, "shares_outstanding")
    if shares is None:
        return []
    present = shares.drop_nulls()
    invalid = present.filter(~present.is_finite() | (present < 0))
    if invalid.is_empty():
        return []
    examples = invalid.unique().sort().to_list()[:_METRICS_EXAMPLES]
    return [
        QaFinding(
            "shares_nonnegative",
            "error",
            f"{invalid.len()} of {present.len()} shares_outstanding value(s) are negative or "
            f"non-finite: {_render_values(examples)}",
        )
    ]


def _check_aum_consistency(parsed: ParsedFile, *, config: ValidationConfig) -> list[QaFinding]:
    """AUM should equal NAV x shares outstanding; a gap is a warning, not an error.

    Issuers round NAV, shares and AUM independently, and some publish only two
    of the three; a relative difference above ``aum_tolerance`` therefore
    records evidence for a human to look at without excluding the file. Rows
    with a missing figure or with their own NAV/shares finding are skipped here.
    An AUM of zero with a positive NAV x shares has no relative difference to
    measure and is reported as undefined.
    """
    frame = parsed.frame
    if any(_numeric_column(frame, name) is None for name in _METRICS_NUMERIC_COLUMNS):
        return []
    offenders: list[tuple[str, str, str]] = []
    for row in frame.iter_rows(named=True):
        nav = row["nav_per_unit"]
        shares = row["shares_outstanding"]
        aum = row["aum"]
        if nav is None or shares is None or aum is None:
            continue
        nav_value, shares_value, aum_value = float(nav), float(shares), float(aum)
        if not all(isfinite(value) for value in (nav_value, shares_value, aum_value)):
            continue
        if nav_value <= 0 or shares_value < 0 or aum_value < 0:
            continue
        expected = nav_value * shares_value
        if aum_value == 0.0:
            if expected == 0.0:
                continue
            difference = "relative=undefined (aum=0)"
        else:
            relative = abs(aum_value - expected) / aum_value
            if relative <= config.aum_tolerance:
                continue
            difference = f"relative={relative:.2%}"
        as_of = row.get("as_of_date")
        as_of_text = as_of.isoformat() if isinstance(as_of, date) else "-"
        fund = str(row.get("fund_ticker", ""))
        description = (
            f"as_of={as_of_text} fund={fund} aum={aum_value:.4f} "
            f"nav*shares={expected:.4f} ({difference})"
        )
        offenders.append((as_of_text, fund, description))
    if not offenders:
        return []
    offenders.sort()
    shown = "; ".join(description for _as_of, _fund, description in offenders[:_METRICS_EXAMPLES])
    remaining = len(offenders) - _METRICS_EXAMPLES
    suffix = f"; {remaining} more" if remaining > 0 else ""
    return [
        QaFinding(
            "aum_consistency",
            "warning",
            f"{len(offenders)} row(s) differ from NAV x shares by more than "
            f"aum_tolerance={config.aum_tolerance:g}: {shown}{suffix}",
        )
    ]


def _check_metrics_future_as_of(parsed: ParsedFile, *, config: ValidationConfig) -> list[QaFinding]:
    """No row's as-of date may be after the fetch date in America/Toronto (D30).

    A metrics file can carry a history, so every stated date is checked, not
    just the file's latest one.
    """
    fetch_date = parsed.raw.fetched_at.astimezone(_TORONTO).date()
    limit = fetch_date + timedelta(days=config.future_as_of_days)
    dates: set[date] = {parsed.as_of_date}
    if parsed.frame.schema.get("as_of_date") == pl.Date:
        dates.update(parsed.frame["as_of_date"].drop_nulls().to_list())
    late = sorted(day for day in dates if day > limit)
    if not late:
        return []
    rendered = ", ".join(day.isoformat() for day in late[:_METRICS_EXAMPLES])
    remaining = len(late) - _METRICS_EXAMPLES
    suffix = f" (+{remaining} more)" if remaining > 0 else ""
    return [
        QaFinding(
            "future_as_of",
            "error",
            f"as_of_date={rendered}{suffix} is after fetch date {fetch_date.isoformat()} "
            f"in America/Toronto (future_as_of_days={config.future_as_of_days})",
        )
    ]


def _check_duplicate_date(parsed: ParsedFile) -> list[QaFinding]:
    """One (fund, as-of date) key may appear only once per metrics file (D66)."""
    frame = parsed.frame
    if "fund_ticker" not in frame.columns or frame.schema.get("as_of_date") != pl.Date:
        return []
    keys = frame.select("fund_ticker", "as_of_date").drop_nulls()
    duplicates = (
        keys.group_by(["fund_ticker", "as_of_date"])
        .len()
        .filter(pl.col("len") > 1)
        .sort(["fund_ticker", "as_of_date"])
    )
    if duplicates.is_empty():
        return []
    rendered = ", ".join(
        f"{row['fund_ticker']} {row['as_of_date'].isoformat()} x{row['len']}"
        for row in duplicates.head(_METRICS_EXAMPLES).iter_rows(named=True)
    )
    remaining = duplicates.height - _METRICS_EXAMPLES
    suffix = f"; {remaining} more" if remaining > 0 else ""
    return [
        QaFinding(
            "duplicate_date",
            "error",
            f"{duplicates.height} (fund, as_of_date) key(s) appear more than once: "
            f"{rendered}{suffix}",
        )
    ]


def validate_metrics(
    parsed: ParsedFile,
    *,
    source: Source,
    config: ValidationConfig,
) -> list[QaFinding]:
    """Run all applicable checks over one parsed fund-metrics file, in a fixed order.

    ``source`` is the metrics source (``kind: fund_metrics``) whose frame this
    is, which may differ from ``parsed.raw.source_id`` when a metrics parser
    reads another source's stored files (``purpose_metrics`` from
    ``purpose_daily``). An error excludes that metrics file only: the build
    records holdings and metrics findings separately.
    """
    findings: list[QaFinding] = []
    findings.extend(_check_metrics_schema(parsed, source=source))
    findings.extend(_check_nav_positive(parsed))
    findings.extend(_check_shares_nonnegative(parsed))
    findings.extend(_check_aum_consistency(parsed, config=config))
    findings.extend(_check_metrics_future_as_of(parsed, config=config))
    findings.extend(_check_duplicate_date(parsed))
    return findings
