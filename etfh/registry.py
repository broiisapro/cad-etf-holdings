"""Source and fund registry with the terms-of-use gate."""

from __future__ import annotations

import csv
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from etfh.types import AssetClass, Decision, FileFormat, SourceKind

__all__ = [
    "Fund",
    "Source",
    "TermsNotAllowedError",
    "TermsRecord",
    "is_collectable",
    "load_funds",
    "load_sources",
    "require_collectable",
]


class TermsRecord(BaseModel):
    """The recorded terms-of-use review for one source."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    reviewed_on: date | None = None
    reviewer: str | None = None
    decision: Decision
    conditions: list[str] = Field(default_factory=list)
    min_interval_seconds: float | None = None
    redistribution_allowed: bool = False

    @model_validator(mode="after")
    def _validate_review_fields(self) -> TermsRecord:
        if self.decision is Decision.pending_review:
            return self
        missing = [
            name
            for name, value in (
                ("url", self.url),
                ("reviewer", self.reviewer),
                ("reviewed_on", self.reviewed_on),
            )
            if value in (None, "")
        ]
        if missing:
            raise ValueError(
                f"decision {self.decision.value!r} requires {', '.join(missing)} to be set"
            )
        if self.decision is Decision.allowed_with_conditions and not self.conditions:
            raise ValueError("allowed_with_conditions requires at least one condition")
        return self

    @model_validator(mode="after")
    def _validate_min_interval(self) -> TermsRecord:
        if self.min_interval_seconds is not None and self.min_interval_seconds <= 0:
            raise ValueError("min_interval_seconds must be positive when set")
        return self


class Source(BaseModel):
    """One issuer/source entry from ``config/sources.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    issuer: str
    kind: SourceKind
    format: FileFormat
    url_patterns: list[str] = Field(min_length=1)
    update_frequency: str
    supported: bool = True
    notes: str = ""
    terms: TermsRecord


class Fund(BaseModel):
    """One fund from ``config/funds.csv``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    issuer: str
    name: str
    asset_class: AssetClass
    source_id: str
    exchange: str = "XTSE"


class TermsNotAllowedError(PermissionError):
    """Raised when a collector tries to use a source the terms gate refuses."""

    def __init__(self, source: Source) -> None:
        self.source_id = source.id
        self.decision = source.terms.decision
        super().__init__(
            f"skipped: terms source={source.id} "
            f"decision={source.terms.decision.value} supported={str(source.supported).lower()}"
        )


def load_sources(path: str | Path) -> dict[str, Source]:
    """Load and validate ``sources.yaml``; raise loudly on invalid records."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise ValueError(f"{path}: expected a top-level 'sources' list")
    sources: dict[str, Source] = {}
    for entry in data["sources"]:
        source = Source.model_validate(entry)
        if source.id in sources:
            raise ValueError(f"{path}: duplicate source id {source.id!r}")
        sources[source.id] = source
    if not sources:
        raise ValueError(f"{path}: no sources defined")
    return sources


def load_funds(path: str | Path, sources: Mapping[str, Source]) -> list[Fund]:
    """Load and validate ``funds.csv`` against the loaded sources."""
    funds: list[Fund] = []
    seen: set[str] = set()
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"ticker", "issuer", "name", "asset_class", "source_id", "exchange"}
        if reader.fieldnames is None or set(reader.fieldnames) != required:
            raise ValueError(f"{path}: expected columns {sorted(required)}")
        for row in reader:
            fund = Fund.model_validate(row)
            if fund.ticker in seen:
                raise ValueError(f"{path}: duplicate fund ticker {fund.ticker!r}")
            if fund.source_id not in sources:
                raise ValueError(
                    f"{path}: fund {fund.ticker!r} references unknown source id {fund.source_id!r}"
                )
            seen.add(fund.ticker)
            funds.append(fund)
    return funds


def is_collectable(source: Source) -> bool:
    """True only for sources marked allowed (with or without conditions) and supported."""
    return source.supported and source.terms.decision in (
        Decision.allowed,
        Decision.allowed_with_conditions,
    )


def require_collectable(source: Source) -> None:
    """Raise :class:`TermsNotAllowedError` unless the source is collectable."""
    if not is_collectable(source):
        raise TermsNotAllowedError(source)
