"""Human-reviewed identifier overrides (D23, D41, D54).

``config/security_overrides.csv`` is the only place where a reviewed fuzzy
match or a hand-fixed identifier becomes a real ``security_id``. The file is
loaded into validated :class:`Override` records; a malformed row fails loudly
at load (D25). Overrides are time-invariant evidence: they are applied to any
frame the security master is asked to resolve, never learned from other files.

Name overrides come in two forms. ``<fund_ticker>|<exact security_name>``
applies to one fund, and ``*|<name>`` is global: it matches the normalized
name (see :func:`etfh.secmaster.names.normalize_name`) of a row in any fund.
The file keeps the reviewed text as written; normalization happens when the
security master builds its lookup table. ``etfh review approve`` writes the
already-normalized form after a human approves a proposal.
"""

from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from etfh.secmaster.identifiers import cusip_is_valid, isin_is_valid, normalize_ticker
from etfh.secmaster.names import infer_kind, normalize_name

__all__ = ["Override", "load_overrides"]

MatchType = Literal["isin", "cusip", "ticker", "name"]

_GLOBAL_FUND = "*"

_COLUMNS = ("match_type", "match_value", "security_id", "reviewer", "reviewed_on", "note")
_MIC_PATTERN = re.compile(r"^[A-Z0-9]{4}$")


class Override(BaseModel):
    """One reviewed mapping from a source identifier to a ``security_id``.

    ``match_type`` is one of ``isin``, ``cusip``, ``ticker`` or ``name``.
    Ticker match values are ``MIC:TICKER``; name match values are
    ``<fund_ticker>|<exact security_name>`` for one fund, or ``*|<name>`` for
    a global, human-approved name match. Global values are matched against the
    row's normalized name; the reviewed text is kept as written.
    ``reviewer`` and ``reviewed_on`` are required: an override is only as good
    as the human who signed it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    match_type: MatchType
    match_value: str = Field(min_length=1)
    security_id: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    reviewed_on: date
    note: str = ""

    @field_validator("match_value")
    @classmethod
    def _validate_match_value(cls, value: str, info: ValidationInfo) -> str:
        match_type = info.data.get("match_type")
        if match_type == "isin":
            cleaned = value.strip().upper()
            if not isin_is_valid(cleaned):
                raise ValueError(f"match_value is not a valid ISIN: {value!r}")
            return cleaned
        if match_type == "cusip":
            cleaned = value.strip().upper()
            if not cusip_is_valid(cleaned):
                raise ValueError(f"match_value is not a valid CUSIP: {value!r}")
            return cleaned
        if match_type == "ticker":
            mic, separator, ticker = value.strip().partition(":")
            mic = mic.strip().upper()
            if not separator or not _MIC_PATTERN.match(mic):
                raise ValueError(f"ticker match_value must be MIC:TICKER: {value!r}")
            normalized = normalize_ticker(ticker)
            if normalized is None:
                raise ValueError(f"ticker match_value has an empty ticker: {value!r}")
            return f"{mic}:{normalized}"
        if match_type == "name":
            fund_ticker, separator, security_name = value.partition("|")
            fund_ticker = fund_ticker.strip()
            security_name = security_name.strip()
            if not separator or not fund_ticker or not security_name:
                raise ValueError(
                    f"name match_value must be <fund_ticker>|<security_name> or "
                    f"*|<normalized name>: {value!r}"
                )
            if fund_ticker == _GLOBAL_FUND:
                if not normalize_name(security_name, kind=infer_kind(security_name)):
                    raise ValueError(f"global name match_value has no name tokens: {value!r}")
                return f"{_GLOBAL_FUND}|{security_name}"
            return f"{fund_ticker}|{security_name}"
        return value

    @field_validator("security_id", "reviewer")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned


def load_overrides(path: Path) -> list[Override]:
    """Load and validate the reviewed overrides CSV at ``path``.

    An empty file (header row only) is valid and returns ``[]``. Missing or
    unknown columns, malformed values and duplicate match keys raise
    ``ValueError`` naming the file and line.
    """
    if not path.exists():
        raise FileNotFoundError(f"overrides file not found: {path}")
    overrides: list[Override] = []
    seen: dict[tuple[str, str], int] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [column for column in _COLUMNS if column not in fieldnames]
        unknown = [column for column in fieldnames if column not in _COLUMNS]
        if missing or unknown:
            raise ValueError(
                f"{path}: columns must be exactly {','.join(_COLUMNS)}; "
                f"missing={missing} unknown={unknown}"
            )
        for line_number, row in enumerate(reader, start=2):
            values = {column: (row.get(column) or "").strip() for column in _COLUMNS}
            if not any(values.values()):
                continue
            try:
                override = Override.model_validate(values)
            except ValueError as error:
                raise ValueError(f"{path}:{line_number}: invalid override: {error}") from error
            key = (override.match_type, override.match_value)
            if key in seen:
                raise ValueError(
                    f"{path}:{line_number}: duplicate override for "
                    f"{override.match_type} {override.match_value!r} "
                    f"(first on line {seen[key]})"
                )
            seen[key] = line_number
            overrides.append(override)
    return overrides
