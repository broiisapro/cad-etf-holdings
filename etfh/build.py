"""Deterministic rebuild of the derived tables from the raw store.

``build`` reads every :class:`~etfh.types.RawFile` in the raw store, finds a
parser, applies the security master and validation, and writes
``<out>/etfh.duckdb`` plus Parquet exports and review reports. The build is
always a full rebuild: the same raw store produces byte-identical tables (D19,
D15).

Point-in-time rules (D20):

- a version is one distinct SHA-256 for one ``(source, fund, as_of_date)``;
  repeated fetches of identical bytes collapse into one version whose
  ``known_at`` is the earliest fetch that produced the hash;
- a different SHA-256 for the same fund and as-of date is a new version
  (``file_version`` 1, 2, ...) ordered by ``known_at``;
- ``known_at`` is the stated publication date when the file carries one,
  otherwise the fetch time.

Every raw file is accounted for: each one gets exactly one ``file_versions``
row, sharing a version when its bytes duplicate an earlier fetch for the same
source, fund and as-of date. A file that cannot be parsed (``parse_error``,
``no_parser``), names an unknown source or fund, or fails validation still gets
an excluded row plus its ``qa_events`` finding, so the QA report can list every
problem file. As-of dates for files that never parsed fall back to the raw
hint, then the fetch date in America/Toronto; ``file_version`` follows the
usual rule.

The build also writes an additive ``sources`` table (``source_id``, ``issuer``,
``kind``, ``format``) from the config it loaded, which analytics uses for the
daily-over-quarterly priority and the ``partial`` flag (D53), and the review
reports ``unmatched.csv``, ``fuzzy_proposals.csv`` and ``name_proposals.csv``.
Name proposals come from identifier-backed rows in this build only; approving
one is a separate human action (D54).

A few parser notes explain how a file was read rather than what it contains
(``partial_list``, ``weight_capped_percent``, ``commodity_quantity``,
``exchange_codes``). Those are kept as ``qa_events`` ``info`` rows, one per
note, with the note name as the check and its value as the detail; other notes
stay on the parsed file only. They never change a file's status.

Every table is sorted on a documented key before writing (D15):

- ``file_versions``: source_id, fund_ticker, as_of_date, file_version
- ``sources``: source_id
- ``holdings``: fund_ticker, as_of_date, file_version, security_id,
  security_name, security_ticker, isin, cusip, currency
- ``qa_events``: source_id, fund_ticker, as_of_date, check, detail,
  raw_file_sha256
- ``holdings_changes``: fund_ticker, from_as_of, to_as_of, change_type,
  security_id
- ``fund_metrics``: fund_ticker, as_of_date, file_version, source_id

Fund metrics (D66, interfaces-06 §3) run beside the holdings path, never inside
it. Every raw file is offered to every metrics parser that claims it, whatever
source the file was collected for (``purpose_metrics`` reads files stored as
``purpose_daily``). A file that no holdings parser claims but at least one
metrics parser does is a metrics-only input: it gets no holdings version and no
``no_parser`` error. The opposite also holds: a metrics failure never excludes a
holdings file and a holdings failure never excludes metrics rows.

Metrics versions use the same D20 rules, keyed on the *metrics* source recorded
in the parsed rows and on each row's ``as_of_date`` — a NAV history file holds
many dates and therefore many versions. ``known_at`` is the parsed file's stated
publication date when it has one, else the fetch time. Findings about parsed
rows go to ``qa_events`` under the metrics source id, so the QA report and as-of
analytics can tell them apart from the holdings findings of the same raw file by
joining the ``sources`` table. A file whose metrics parser raises, or whose rows
name a source or fund missing from the config, is reported as an error against
the raw file's own source and produces no metrics rows; there is no version to
exclude because there is no trustworthy date.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import duckdb
import polars as pl
from polars.datatypes import DataType, DataTypeClass

from etfh.asof import TORONTO
from etfh.parse.registry import find_metrics_parsers, find_parser
from etfh.registry import Fund, Source, load_funds, load_sources
from etfh.schema import (
    DDL_FILE_VERSIONS,
    DDL_FUND_METRICS,
    DDL_HOLDINGS,
    DDL_HOLDINGS_CHANGES,
    DDL_QA_EVENTS,
)
from etfh.secmaster.names import name_proposals, reference_names
from etfh.secmaster.reports import fuzzy_proposals, unmatched_report
from etfh.storage import RawStore
from etfh.types import FileFormat, ParsedFile, Parser, RawFile, SourceKind
from etfh.validate import ValidationConfig, is_excluded, validate_file

if TYPE_CHECKING:
    from etfh.secmaster.openfigi import FigiLookup
    from etfh.secmaster.resolve import SecurityMaster
    from etfh.validate import QaFinding

__all__ = ["CHANGE_THRESHOLD", "NAME_PROPOSAL_COLUMNS", "BuildResult", "build", "connect"]

CHANGE_THRESHOLD = 0.001

#: Parser notes the build keeps as ``qa_events`` ``info`` rows. They record how
#: a file was interpreted (a source-declared partial list, a weight the document
#: printed above 100%, a physical-quantity commodity line, the exchange codes a
#: parser had to resolve) without changing the file's status. The note name
#: becomes the check and the text after the first ``=`` becomes the detail.
_PERSISTED_NOTES: frozenset[str] = frozenset(
    {
        "partial_list",
        "weight_capped_percent",
        "commodity_quantity",
        "exchange_codes",
    }
)

DDL_SOURCES = """
CREATE TABLE IF NOT EXISTS sources (
    source_id VARCHAR NOT NULL,
    issuer VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    format VARCHAR NOT NULL
);
"""

_DDLS = (
    DDL_FILE_VERSIONS,
    DDL_SOURCES,
    DDL_HOLDINGS,
    DDL_QA_EVENTS,
    DDL_HOLDINGS_CHANGES,
    DDL_FUND_METRICS,
)

#: Columns a metrics parser's frame must supply, in model order. The build
#: overwrites ``raw_file_sha256`` with the hash of the file it actually parsed;
#: a parser cannot claim another file's bytes.
_METRICS_PARSED_COLUMNS: tuple[str, ...] = (
    "fund_ticker",
    "as_of_date",
    "nav_per_unit",
    "shares_outstanding",
    "aum",
    "currency",
    "source_id",
    "raw_file_sha256",
)

_SOURCES_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "source_id": pl.String,
    "issuer": pl.String,
    "kind": pl.String,
    "format": pl.String,
}

#: Columns of ``reports/name_proposals.csv`` (interfaces-04 §3, D54). Name
#: matches are only proposals: the build writes them, a human approves them
#: (``etfh review approve``) and only the reviewed overrides file changes an id.
NAME_PROPOSAL_COLUMNS: tuple[str, ...] = (
    "normalized_name",
    "example_name",
    "country",
    "n_funds",
    "total_weight",
    "candidate_security_id",
    "candidate_name",
    "score",
    "confidence",
)

_FILE_VERSIONS_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "source_id": pl.String,
    "fund_ticker": pl.String,
    "as_of_date": pl.Date,
    "file_version": pl.Int32,
    "known_at": pl.Datetime("us", "UTC"),
    "raw_file_sha256": pl.String,
    "path": pl.String,
    "status": pl.String,
}

_HOLDINGS_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "fund_ticker": pl.String,
    "as_of_date": pl.Date,
    "known_at": pl.Datetime("us", "UTC"),
    "file_version": pl.Int32,
    "security_id": pl.String,
    "security_name": pl.String,
    "security_ticker": pl.String,
    "exchange": pl.String,
    "isin": pl.String,
    "cusip": pl.String,
    "asset_class": pl.String,
    "sector": pl.String,
    "country": pl.String,
    "currency": pl.String,
    "weight": pl.Float64,
    "shares": pl.Float64,
    "market_value": pl.Float64,
    "source_id": pl.String,
    "raw_file_sha256": pl.String,
}

_QA_EVENTS_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "raw_file_sha256": pl.String,
    "source_id": pl.String,
    "fund_ticker": pl.String,
    "as_of_date": pl.Date,
    "check": pl.String,
    "severity": pl.String,
    "detail": pl.String,
    "excluded": pl.Boolean,
}

_HOLDINGS_CHANGES_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "fund_ticker": pl.String,
    "from_as_of": pl.Date,
    "to_as_of": pl.Date,
    "security_id": pl.String,
    "change_type": pl.String,
    "weight_before": pl.Float64,
    "weight_after": pl.Float64,
    "delta": pl.Float64,
}

_FUND_METRICS_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "fund_ticker": pl.String,
    "as_of_date": pl.Date,
    "known_at": pl.Datetime("us", "UTC"),
    "file_version": pl.Int32,
    "nav_per_unit": pl.Float64,
    "shares_outstanding": pl.Float64,
    "aum": pl.Float64,
    "currency": pl.String,
    "source_id": pl.String,
    "raw_file_sha256": pl.String,
}

_HOLDINGS_SORT_KEY = (
    "fund_ticker",
    "as_of_date",
    "file_version",
    "security_id",
    "security_name",
    "security_ticker",
    "isin",
    "cusip",
    "currency",
    "weight",
)


@dataclass(frozen=True)
class BuildResult:
    """Summary of one rebuild.

    Counts are rows written to each table; ``n_file_versions`` counts every
    stored version (holdings and metrics, included and excluded), while
    ``n_holdings`` and ``n_fund_metrics`` cover the included files only.
    """

    out_dir: Path
    db_path: Path
    n_file_versions: int
    n_included_files: int
    n_excluded_files: int
    n_holdings: int
    n_changes: int
    n_qa_events: int
    n_unmatched: int
    n_name_proposals: int = 0
    n_figi: int = 0
    n_fund_metrics: int = 0


@dataclass
class _Candidate:
    parsed: ParsedFile
    source: Source
    fund: Fund


@dataclass
class _Record:
    """One raw file (after collapsing identical SHAs) awaiting a version number."""

    raw: RawFile
    as_of_date: date
    known_at: datetime
    fetched_at: datetime
    path: str
    candidate: _Candidate | None = None
    finding: tuple[str, str] | None = None


@dataclass
class _Version:
    source_id: str
    fund_ticker: str
    as_of_date: date
    file_version: int
    raw: RawFile = field(repr=False)
    path: str
    known_at: datetime
    candidate: _Candidate | None = field(default=None, repr=False)
    finding: tuple[str, str] | None = None


@dataclass
class _MetricsRecord:
    """One raw file's metrics for one (metrics source, fund, as-of date)."""

    raw: RawFile = field(repr=False)
    source_id: str
    fund_ticker: str
    as_of_date: date
    known_at: datetime
    path: str
    frame: pl.DataFrame = field(repr=False)
    excluded: bool


@dataclass
class _MetricsCache:
    """Parsed metrics files and their findings, shared across duplicate fetches.

    Files with the same SHA-256 are parsed and validated once per metrics
    parser, so a raw file stored twice cannot produce duplicate findings.
    """

    parsed: dict[tuple[str, int], ParsedFile] = field(default_factory=dict)
    parse_errors: dict[tuple[str, int], str] = field(default_factory=dict)
    findings: dict[tuple[str, str], list[QaFinding]] = field(default_factory=dict)


def build(
    raw_root: Path,
    out_dir: Path,
    *,
    config_dir: Path = Path("config"),
    security_master: SecurityMaster | None = None,
    validator: Callable[..., list[QaFinding]] | None = None,
    metrics_validator: Callable[..., list[QaFinding]] | None = None,
    figi: FigiLookup | None = None,
) -> BuildResult:
    """Rebuild DuckDB, Parquet exports and reports from ``raw_root``.

    ``security_master``, ``validator`` and ``metrics_validator`` are injection
    points: when omitted, the real
    :class:`~etfh.secmaster.resolve.SecurityMaster`,
    :func:`etfh.validate.validate_file` and
    :func:`etfh.validate.validate_metrics` are imported lazily. Tests pass fakes
    that follow the same contracts. ``figi`` is the optional
    :class:`~etfh.secmaster.openfigi.FigiLookup` used when the build creates
    the security master itself (D47); an injected ``security_master`` wins.
    """
    raw_root = Path(raw_root)
    out_dir = Path(out_dir)
    sources = _load_sources(config_dir / "sources.yaml")
    funds = load_funds(config_dir / "funds.csv", sources)
    funds_by_ticker = {fund.ticker: fund for fund in funds}
    if security_master is None:
        security_master = _default_security_master(config_dir, figi=figi)
    validation_config = ValidationConfig.load(config_dir / "validation.yaml")
    if validator is None:
        validator = validate_file
    metrics_validator_fn = metrics_validator

    raws = RawStore(raw_root).iter_raw()
    parsed_cache: dict[str, ParsedFile] = {}
    parse_failures: dict[str, tuple[str, str]] = {}
    qa_rows: list[dict[str, Any]] = []
    records: dict[tuple[str, str, date, str], _Record] = {}
    metrics_cache = _MetricsCache()
    metrics_records: dict[tuple[str, str, date, str], _MetricsRecord] = {}

    for raw in raws:
        source = sources.get(raw.source_id)
        fund = funds_by_ticker.get(raw.fund_ticker)
        metrics_parsers = (
            find_metrics_parsers(raw, source) if source is not None and fund is not None else []
        )
        candidate: _Candidate | None = None
        finding: tuple[str, str] | None = None
        as_of_date = _fallback_as_of(raw)
        known_at = raw.fetched_at
        if source is None:
            finding = ("unknown_source", f"source {raw.source_id!r} is not in sources.yaml")
        elif fund is None:
            finding = ("unknown_fund", f"fund {raw.fund_ticker!r} is not in funds.csv")
        else:
            if raw.sha256 not in parsed_cache and raw.sha256 not in parse_failures:
                parser = find_parser(raw, source)
                if parser is None:
                    if not metrics_parsers:
                        parse_failures[raw.sha256] = (
                            "no_parser",
                            f"no parser for .{raw.ext} files from source {source.id}",
                        )
                else:
                    try:
                        parsed_cache[raw.sha256] = parser.parse(raw)
                    except Exception as error:
                        parse_failures[raw.sha256] = (
                            "parse_error",
                            f"{type(error).__name__}: {error}",
                        )
            if raw.sha256 in parse_failures:
                finding = parse_failures[raw.sha256]
            else:
                parsed = parsed_cache.get(raw.sha256)
                if parsed is not None:
                    as_of_date = parsed.as_of_date
                    if parsed.published_at is not None:
                        known_at = parsed.published_at
                    candidate = _Candidate(parsed=parsed, source=source, fund=fund)
        metrics_only = candidate is None and finding is None and bool(metrics_parsers)
        if not metrics_only:
            key = (raw.source_id, raw.fund_ticker, as_of_date, raw.sha256)
            previous = records.get(key)
            if previous is None:
                records[key] = _Record(
                    raw=raw,
                    as_of_date=as_of_date,
                    known_at=known_at,
                    fetched_at=raw.fetched_at,
                    path=str(raw.path),
                    candidate=candidate,
                    finding=finding,
                )
            else:
                previous.known_at = min(previous.known_at, known_at)
                previous.fetched_at = min(previous.fetched_at, raw.fetched_at)
                previous.path = min(previous.path, str(raw.path))

        if metrics_parsers:
            if metrics_validator_fn is None:
                metrics_validator_fn = _default_metrics_validator()
            new_records, events = _collect_metrics(
                raw,
                metrics_parsers=metrics_parsers,
                sources=sources,
                funds_by_ticker=funds_by_ticker,
                validator=metrics_validator_fn,
                config=validation_config,
                cache=metrics_cache,
            )
            qa_rows.extend(events)
            for metrics_record in new_records:
                metrics_key = (
                    metrics_record.source_id,
                    metrics_record.fund_ticker,
                    metrics_record.as_of_date,
                    metrics_record.raw.sha256,
                )
                previous_metrics = metrics_records.get(metrics_key)
                if previous_metrics is None:
                    metrics_records[metrics_key] = metrics_record
                else:
                    previous_metrics.known_at = min(
                        previous_metrics.known_at, metrics_record.known_at
                    )
                    previous_metrics.path = min(previous_metrics.path, metrics_record.path)

    if figi is not None:
        _prefetch_figi(security_master, records.values())

    versions: list[_Version] = []
    grouped: dict[tuple[str, str, date], list[_Record]] = {}
    for (source_id, fund_ticker, as_of_date, _sha), record in sorted(records.items()):
        grouped.setdefault((source_id, fund_ticker, as_of_date), []).append(record)
    for (source_id, fund_ticker, as_of_date), group in grouped.items():
        ordered = sorted(group, key=lambda record: (record.known_at, record.raw.sha256))
        for index, record in enumerate(ordered, start=1):
            versions.append(
                _Version(
                    source_id=source_id,
                    fund_ticker=fund_ticker,
                    as_of_date=as_of_date,
                    file_version=index,
                    raw=record.raw,
                    path=record.path,
                    known_at=record.known_at,
                    candidate=record.candidate,
                    finding=record.finding,
                )
            )

    metrics_versions: list[tuple[_MetricsRecord, int]] = []
    metrics_grouped: dict[tuple[str, str, date], list[_MetricsRecord]] = {}
    for (source_id, fund_ticker, as_of_date, _sha), metrics_record in sorted(
        metrics_records.items()
    ):
        metrics_grouped.setdefault((source_id, fund_ticker, as_of_date), []).append(metrics_record)
    for metrics_group in metrics_grouped.values():
        ordered_metrics = sorted(
            metrics_group, key=lambda record: (record.known_at, record.raw.sha256)
        )
        for index, metrics_record in enumerate(ordered_metrics, start=1):
            metrics_versions.append((metrics_record, index))

    file_version_rows: list[dict[str, Any]] = []
    holdings_frames: list[pl.DataFrame] = []
    for version in versions:
        candidate = version.candidate
        if candidate is None:
            check, detail = version.finding if version.finding is not None else ("parse_error", "")
            file_version_rows.append(
                {
                    "source_id": version.source_id,
                    "fund_ticker": version.fund_ticker,
                    "as_of_date": version.as_of_date,
                    "file_version": version.file_version,
                    "known_at": version.known_at,
                    "raw_file_sha256": version.raw.sha256,
                    "path": version.path,
                    "status": "excluded",
                }
            )
            qa_rows.append(
                _qa_row(
                    version.raw,
                    version.as_of_date,
                    check,
                    "error",
                    detail,
                    excluded=True,
                )
            )
            continue
        assigned = security_master.assign(candidate.parsed.frame)
        parsed_with_id = ParsedFile(
            raw=candidate.parsed.raw,
            as_of_date=candidate.parsed.as_of_date,
            published_at=candidate.parsed.published_at,
            frame=assigned,
            notes=list(candidate.parsed.notes),
        )
        findings = validator(
            parsed_with_id,
            source=candidate.source,
            fund_asset_class=candidate.fund.asset_class,
            config=validation_config,
        )
        excluded = is_excluded(findings)
        file_version_rows.append(
            {
                "source_id": version.source_id,
                "fund_ticker": version.fund_ticker,
                "as_of_date": version.as_of_date,
                "file_version": version.file_version,
                "known_at": version.known_at,
                "raw_file_sha256": version.raw.sha256,
                "path": version.path,
                "status": "excluded" if excluded else "included",
            }
        )
        for name, value in _persisted_notes(candidate.parsed.notes):
            qa_rows.append(
                {
                    "raw_file_sha256": version.raw.sha256,
                    "source_id": version.source_id,
                    "fund_ticker": version.fund_ticker,
                    "as_of_date": version.as_of_date,
                    "check": name,
                    "severity": "info",
                    "detail": value,
                    "excluded": excluded,
                }
            )
        for qa_finding in findings:
            qa_rows.append(
                {
                    "raw_file_sha256": version.raw.sha256,
                    "source_id": version.source_id,
                    "fund_ticker": version.fund_ticker,
                    "as_of_date": version.as_of_date,
                    "check": qa_finding.check,
                    "severity": qa_finding.severity,
                    "detail": qa_finding.detail,
                    "excluded": excluded,
                }
            )
        if not excluded:
            holdings_frames.append(
                _holdings_frame(
                    assigned,
                    known_at=version.known_at,
                    file_version=version.file_version,
                )
            )

    fund_metrics_frames: list[pl.DataFrame] = []
    for metrics_record, metrics_file_version in metrics_versions:
        file_version_rows.append(
            {
                "source_id": metrics_record.source_id,
                "fund_ticker": metrics_record.fund_ticker,
                "as_of_date": metrics_record.as_of_date,
                "file_version": metrics_file_version,
                "known_at": metrics_record.known_at,
                "raw_file_sha256": metrics_record.raw.sha256,
                "path": metrics_record.path,
                "status": "excluded" if metrics_record.excluded else "included",
            }
        )
        if not metrics_record.excluded:
            fund_metrics_frames.append(
                _fund_metrics_frame(
                    metrics_record.frame,
                    known_at=metrics_record.known_at,
                    file_version=metrics_file_version,
                )
            )

    file_versions = _sorted(
        pl.DataFrame(file_version_rows, schema=_FILE_VERSIONS_SCHEMA),
        ("source_id", "fund_ticker", "as_of_date", "file_version"),
    )
    holdings = _sorted(
        pl.concat(holdings_frames, how="vertical")
        if holdings_frames
        else pl.DataFrame(schema=_HOLDINGS_SCHEMA),
        _HOLDINGS_SORT_KEY,
    )
    fund_metrics = _sorted(
        pl.concat(fund_metrics_frames, how="vertical")
        if fund_metrics_frames
        else pl.DataFrame(schema=_FUND_METRICS_SCHEMA),
        ("fund_ticker", "as_of_date", "file_version", "source_id"),
    )
    qa_events = _sorted(
        pl.DataFrame(qa_rows, schema=_QA_EVENTS_SCHEMA),
        ("source_id", "fund_ticker", "as_of_date", "check", "detail", "raw_file_sha256"),
    )
    priority = {source_id: _source_priority(source) for source_id, source in sources.items()}
    holdings_changes = _compute_changes(holdings, priority)
    n_figi = holdings.filter(pl.col("security_id").str.starts_with("FIGI:")).height

    frames = {
        "file_versions": file_versions,
        "sources": _sources_frame(sources),
        "holdings": holdings,
        "qa_events": qa_events,
        "holdings_changes": holdings_changes,
        "fund_metrics": fund_metrics,
    }
    db_path = _write_database(out_dir, frames)
    unmatched, n_name_proposals = _write_reports(out_dir, holdings)
    return BuildResult(
        out_dir=out_dir,
        db_path=db_path,
        n_file_versions=file_versions.height,
        n_included_files=file_versions.filter(pl.col("status") == "included").height,
        n_excluded_files=file_versions.filter(pl.col("status") == "excluded").height,
        n_holdings=holdings.height,
        n_changes=holdings_changes.height,
        n_qa_events=qa_events.height,
        n_unmatched=unmatched.height,
        n_name_proposals=n_name_proposals,
        n_figi=n_figi,
        n_fund_metrics=fund_metrics.height,
    )


def _prefetch_figi(security_master: SecurityMaster, records: Iterable[_Record]) -> None:
    """Batch the FIGI lookups for every parsed file in one pass (D47)."""
    prefetch = getattr(security_master, "prefetch", None)
    if not callable(prefetch):
        return
    frames = [record.candidate.parsed.frame for record in records if record.candidate is not None]
    if frames:
        prefetch(frames)


def _fallback_as_of(raw: RawFile) -> date:
    """The as-of date for a file that never parsed: hint, else fetch date (D30)."""
    if raw.as_of_hint is not None:
        return raw.as_of_hint
    return raw.fetched_at.astimezone(TORONTO).date()


def _sources_frame(sources: Mapping[str, Source]) -> pl.DataFrame:
    rows = [
        {
            "source_id": source.id,
            "issuer": source.issuer,
            "kind": source.kind.value,
            "format": source.format.value,
        }
        for source in sorted(sources.values(), key=lambda source: source.id)
    ]
    return pl.DataFrame(rows, schema=_SOURCES_SCHEMA)


def _qa_row(
    raw: RawFile,
    as_of_date: date | None,
    check: str,
    severity: str,
    detail: str,
    *,
    excluded: bool,
    source_id: str | None = None,
    fund_ticker: str | None = None,
) -> dict[str, Any]:
    """One ``qa_events`` row for ``raw``.

    ``source_id`` and ``fund_ticker`` default to the raw file's own values;
    metrics findings pass the metrics source and the fund the parser named, so
    the report can tell a fund-metrics problem from the holdings file's own.
    """
    return {
        "raw_file_sha256": raw.sha256,
        "source_id": raw.source_id if source_id is None else source_id,
        "fund_ticker": raw.fund_ticker if fund_ticker is None else fund_ticker,
        "as_of_date": as_of_date,
        "check": check,
        "severity": severity,
        "detail": detail,
        "excluded": excluded,
    }


def _collect_metrics(
    raw: RawFile,
    *,
    metrics_parsers: Sequence[Parser],
    sources: Mapping[str, Source],
    funds_by_ticker: Mapping[str, Fund],
    validator: Callable[..., list[QaFinding]],
    config: ValidationConfig,
    cache: _MetricsCache,
) -> tuple[list[_MetricsRecord], list[dict[str, Any]]]:
    """Run every metrics parser that claims ``raw`` (interfaces-06 §3).

    Returns the metrics records for this file plus the ``qa_events`` rows the
    parsing and validation produced. A parser error, an unknown metrics source
    or an unknown fund is an error event and produces no rows: there is no
    trustworthy as-of date to attach a version to (see the module docstring).
    """
    records: list[_MetricsRecord] = []
    events: list[dict[str, Any]] = []
    for index, parser in enumerate(metrics_parsers):
        cache_key = (raw.sha256, index)
        if cache_key not in cache.parsed and cache_key not in cache.parse_errors:
            try:
                parsed_metrics = parser.parse(raw)
                frame = _metrics_frame(parsed_metrics.frame, sha=raw.sha256)
            except Exception as error:
                cache.parse_errors[cache_key] = f"{type(error).__name__}: {error}"
            else:
                cache.parsed[cache_key] = ParsedFile(
                    raw=parsed_metrics.raw,
                    as_of_date=parsed_metrics.as_of_date,
                    published_at=parsed_metrics.published_at,
                    frame=frame,
                    notes=list(parsed_metrics.notes),
                )
        if cache_key in cache.parse_errors:
            events.append(
                _qa_row(
                    raw,
                    _fallback_as_of(raw),
                    "metrics_parse_error",
                    "error",
                    cache.parse_errors[cache_key],
                    excluded=True,
                )
            )
            continue
        parsed = cache.parsed[cache_key]
        for raw_source_id in sorted(parsed.frame["source_id"].unique().to_list()):
            source_id = str(raw_source_id)
            metrics_source = sources.get(source_id)
            if metrics_source is None:
                events.append(
                    _qa_row(
                        raw,
                        parsed.as_of_date,
                        "unknown_source",
                        "error",
                        f"metrics source {source_id!r} is not in sources.yaml",
                        excluded=True,
                        source_id=source_id,
                    )
                )
                continue
            subset = parsed.frame.filter(pl.col("source_id") == source_id)
            findings = cache.findings.get((raw.sha256, source_id))
            if findings is None:
                findings = validator(
                    ParsedFile(
                        raw=parsed.raw,
                        as_of_date=parsed.as_of_date,
                        published_at=parsed.published_at,
                        frame=subset,
                        notes=list(parsed.notes),
                    ),
                    source=metrics_source,
                    config=config,
                )
                cache.findings[(raw.sha256, source_id)] = findings
                excluded_file = is_excluded(findings)
                for finding in findings:
                    events.append(
                        _qa_row(
                            raw,
                            parsed.as_of_date,
                            finding.check,
                            finding.severity,
                            finding.detail,
                            excluded=excluded_file,
                            source_id=source_id,
                        )
                    )
            excluded = is_excluded(findings)
            known_at = parsed.published_at if parsed.published_at is not None else raw.fetched_at
            for keys, group in subset.group_by(["fund_ticker", "as_of_date"], maintain_order=True):
                fund_ticker = str(keys[0])
                as_of_date = keys[1]
                if fund_ticker not in funds_by_ticker:
                    events.append(
                        _qa_row(
                            raw,
                            as_of_date,
                            "unknown_fund",
                            "error",
                            f"fund {fund_ticker!r} is not in funds.csv",
                            excluded=True,
                            source_id=source_id,
                            fund_ticker=fund_ticker,
                        )
                    )
                    continue
                records.append(
                    _MetricsRecord(
                        raw=raw,
                        source_id=source_id,
                        fund_ticker=fund_ticker,
                        as_of_date=as_of_date,
                        known_at=known_at,
                        path=str(raw.path),
                        frame=group,
                        excluded=excluded,
                    )
                )
    return records, events


def _metrics_frame(frame: pl.DataFrame, *, sha: str) -> pl.DataFrame:
    """Select and type the metric columns a parser must supply (interfaces-06 §2).

    ``raw_file_sha256`` is always the hash of the bytes that were parsed, so a
    parser cannot claim another file's provenance. A frame missing a column is a
    parse error, not a silent gap.
    """
    missing = [name for name in _METRICS_PARSED_COLUMNS if name not in frame.columns]
    if missing:
        raise ValueError(f"metrics frame is missing column(s): {', '.join(missing)}")
    return frame.select(
        pl.col("fund_ticker").cast(pl.String),
        pl.col("as_of_date").cast(pl.Date),
        pl.col("nav_per_unit").cast(pl.Float64),
        pl.col("shares_outstanding").cast(pl.Float64),
        pl.col("aum").cast(pl.Float64),
        pl.col("currency").cast(pl.String),
        pl.col("source_id").cast(pl.String),
        pl.lit(sha).alias("raw_file_sha256"),
    )


def _fund_metrics_frame(
    frame: pl.DataFrame, *, known_at: datetime, file_version: int
) -> pl.DataFrame:
    added = frame.with_columns(
        pl.lit(known_at).alias("known_at"),
        pl.lit(file_version, dtype=pl.Int32).alias("file_version"),
    )
    return added.select(list(_FUND_METRICS_SCHEMA))


def _default_metrics_validator() -> Callable[..., list[QaFinding]]:
    """The real metrics validator, imported only when a metrics parser matched."""
    from etfh import validate as validate_module

    validator = getattr(validate_module, "validate_metrics", None)
    if validator is None:
        raise RuntimeError(
            "etfh.validate.validate_metrics is not available; it is required to build fund metrics"
        )
    return cast(Callable[..., list[QaFinding]], validator)


def _persisted_notes(notes: Iterable[str]) -> list[tuple[str, str]]:
    """``(check, detail)`` for the parser notes the build keeps as info events.

    Only the names in :data:`_PERSISTED_NOTES` are selected, matched exactly so
    a note such as ``partial_list_hint=...`` is not mistaken for ``partial_list``.
    The detail is the text after the first ``=``.
    """
    selected: list[tuple[str, str]] = []
    for note in notes:
        name, separator, value = note.partition("=")
        if separator and name in _PERSISTED_NOTES:
            selected.append((name, value))
    return selected


def _holdings_frame(frame: pl.DataFrame, *, known_at: datetime, file_version: int) -> pl.DataFrame:
    added = frame.with_columns(
        pl.lit(known_at).alias("known_at"),
        pl.lit(file_version, dtype=pl.Int32).alias("file_version"),
    )
    return added.select(list(_HOLDINGS_SCHEMA))


def _sorted(frame: pl.DataFrame, keys: tuple[str, ...]) -> pl.DataFrame:
    return frame.sort(list(keys))


def _source_priority(source: Source) -> int:
    if source.kind is SourceKind.quarterly_disclosure:
        return 1
    if source.kind is SourceKind.synthetic and source.format is FileFormat.pdf:
        return 1
    return 0


def _compute_changes(holdings: pl.DataFrame, priority: Mapping[str, int]) -> pl.DataFrame:
    """Compare consecutive as-of dates per fund, preferring daily sources (D42)."""
    if holdings.is_empty():
        return pl.DataFrame(schema=_HOLDINGS_CHANGES_SCHEMA)
    priority_frame = pl.DataFrame(
        {
            "source_id": pl.Series(list(priority), dtype=pl.String),
            "_priority": pl.Series(list(priority.values()), dtype=pl.Int32),
        }
    )
    versions = (
        holdings.select("fund_ticker", "as_of_date", "file_version", "source_id")
        .unique()
        .join(priority_frame, on="source_id", how="left")
        .with_columns(pl.col("_priority").fill_null(1))
    )
    preferred = (
        versions.sort(
            ["fund_ticker", "as_of_date", "_priority", "file_version", "source_id"],
            descending=[False, False, False, True, False],
        )
        .unique(subset=["fund_ticker", "as_of_date"], keep="first", maintain_order=True)
        .select("fund_ticker", "as_of_date", "file_version", "source_id")
    )
    timeline = preferred.select("fund_ticker", "as_of_date").sort(["fund_ticker", "as_of_date"])
    pairs = (
        timeline.with_columns(
            pl.col("as_of_date").shift(1).over("fund_ticker").alias("from_as_of"),
            pl.col("as_of_date").alias("to_as_of"),
        )
        .filter(pl.col("from_as_of").is_not_null())
        .select("fund_ticker", "from_as_of", "to_as_of")
    )
    if pairs.is_empty():
        return pl.DataFrame(schema=_HOLDINGS_CHANGES_SCHEMA)
    selected = holdings.join(
        preferred, on=["fund_ticker", "as_of_date", "file_version", "source_id"], how="inner"
    ).with_columns(
        pl.coalesce(
            pl.col("security_id"),
            pl.concat_str(
                ["security_name", "security_ticker", "currency"], separator="|", ignore_nulls=True
            ),
        ).alias("_key")
    )
    per_version = selected.group_by(["fund_ticker", "as_of_date", "_key"]).agg(
        pl.col("security_id").drop_nulls().first().alias("security_id"),
        pl.col("weight").sum().alias("weight"),
    )
    before = per_version.rename(
        {
            "as_of_date": "from_as_of",
            "weight": "weight_before",
            "security_id": "security_id_before",
        }
    ).join(pairs, on=["fund_ticker", "from_as_of"], how="inner")
    after = per_version.rename(
        {"as_of_date": "to_as_of", "weight": "weight_after", "security_id": "security_id_after"}
    ).join(pairs, on=["fund_ticker", "to_as_of"], how="inner")
    keys = ["fund_ticker", "from_as_of", "to_as_of", "_key"]
    added = after.join(before.select(keys), on=keys, how="anti").select(
        "fund_ticker",
        "from_as_of",
        "to_as_of",
        pl.col("security_id_after").alias("security_id"),
        pl.lit("add", dtype=pl.String).alias("change_type"),
        pl.lit(None, dtype=pl.Float64).alias("weight_before"),
        pl.col("weight_after"),
        pl.lit(None, dtype=pl.Float64).alias("delta"),
    )
    dropped = before.join(after.select(keys), on=keys, how="anti").select(
        "fund_ticker",
        "from_as_of",
        "to_as_of",
        pl.col("security_id_before").alias("security_id"),
        pl.lit("drop", dtype=pl.String).alias("change_type"),
        pl.col("weight_before"),
        pl.lit(None, dtype=pl.Float64).alias("weight_after"),
        pl.lit(None, dtype=pl.Float64).alias("delta"),
    )
    changed = (
        before.join(after, on=keys, how="inner")
        .with_columns((pl.col("weight_after") - pl.col("weight_before")).alias("delta"))
        .filter(pl.col("delta").abs() >= CHANGE_THRESHOLD)
        .select(
            "fund_ticker",
            "from_as_of",
            "to_as_of",
            pl.coalesce("security_id_after", "security_id_before").alias("security_id"),
            pl.lit("weight_change", dtype=pl.String).alias("change_type"),
            "weight_before",
            "weight_after",
            "delta",
        )
    )
    changes = pl.concat([added, dropped, changed], how="vertical")
    return _sorted(
        changes,
        ("fund_ticker", "from_as_of", "to_as_of", "change_type", "security_id"),
    )


def connect(path: str | Path, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection, honouring ``ETFH_DUCKDB_THREADS`` (D49).

    Every connection the package opens goes through this helper so the
    single-runner CI setting (``ETFH_DUCKDB_THREADS=1``) applies without any
    other configuration. An invalid value fails loudly rather than silently
    using every core.
    """
    connection = duckdb.connect(str(path), read_only=read_only)
    configured = os.environ.get("ETFH_DUCKDB_THREADS")
    if configured:
        try:
            threads = int(configured)
        except ValueError as error:
            connection.close()
            raise ValueError(
                f"ETFH_DUCKDB_THREADS must be a positive integer, got {configured!r}"
            ) from error
        if threads < 1:
            connection.close()
            raise ValueError(f"ETFH_DUCKDB_THREADS must be >= 1, got {threads}")
        connection.execute(f"SET threads TO {threads}")
    return connection


def _write_database(out_dir: Path, frames: Mapping[str, pl.DataFrame]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "etfh.duckdb"
    db_path.unlink(missing_ok=True)
    connection = connect(db_path)
    try:
        for ddl in _DDLS:
            connection.execute(ddl)
        for table, frame in frames.items():
            connection.register("_etfh_frame", frame)
            connection.execute(f"INSERT INTO {table} SELECT * FROM _etfh_frame")
            connection.unregister("_etfh_frame")
    finally:
        connection.close()
    parquet_dir = out_dir / "parquet"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    for table, frame in frames.items():
        frame.write_parquet(parquet_dir / f"{table}.parquet")
    return db_path


def _write_reports(out_dir: Path, holdings: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    reports_dir = out_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    unmatched = unmatched_report(holdings)
    unmatched.write_csv(reports_dir / "unmatched.csv")
    known = holdings.select("security_id", "security_name")
    proposals = fuzzy_proposals(unmatched, known)
    proposals.write_csv(reports_dir / "fuzzy_proposals.csv")
    n_name_proposals = _write_name_proposals(reports_dir, holdings)
    return unmatched, n_name_proposals


def _write_name_proposals(reports_dir: Path, holdings: pl.DataFrame) -> int:
    """Write ``reports/name_proposals.csv`` for the unmatched names (D54).

    Candidates come from the identifier-backed rows in this build (never from
    ids that a name override produced, D41), so the report can only propose
    what the raw store actually evidences. The file is always written, with
    its contract header even when there is nothing to propose; nothing here
    changes an id or the overrides file.
    """
    unmatched = holdings.filter(
        pl.col("security_id").is_null()
        & ~pl.col("asset_class").str.to_lowercase().is_in(["cash", "fx", "derivative"])
    )
    reference = reference_names([holdings])
    proposals = name_proposals(
        unmatched.select("fund_ticker", "security_name", "country", "weight"),
        reference,
    )
    proposals.write_csv(reports_dir / "name_proposals.csv")
    return proposals.height


@lru_cache(maxsize=4)
def _cached_sources(path: str, mtime_ns: int, size: int) -> dict[str, Source]:
    return load_sources(path)


def _load_sources(path: Path) -> dict[str, Source]:
    stat = path.stat()
    return _cached_sources(str(path), stat.st_mtime_ns, stat.st_size)


def _default_security_master(config_dir: Path, *, figi: FigiLookup | None = None) -> SecurityMaster:
    from etfh.secmaster.overrides import load_overrides
    from etfh.secmaster.resolve import SecurityMaster

    overrides_path = config_dir / "security_overrides.csv"
    overrides = load_overrides(overrides_path) if overrides_path.exists() else []
    return SecurityMaster(overrides=overrides, figi=figi)
