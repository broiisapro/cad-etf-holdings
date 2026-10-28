"""Typed view of a weekly digest document (interfaces wave 08 §3).

The renderer consumes digests as plain JSON-ready mappings, exactly as the
CLI and the Pages workflow hand them over. This module checks the fields the
site actually uses, converts them into frozen dataclasses and *drops*
security-level changes for funds that do not disclose securities (D79), so no
later code path can render a security name, id or weight for such a fund even
when a caller supplies a document that wrongly contains changes.

The digest validator (``etfh.digest.validate_digest``) owns the full schema;
the site package cannot import it while both land in the same wave, so this
checker covers only what is rendered and raises ``ValueError`` naming the
first problem, with the digest's position in the input sequence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, NoReturn

__all__ = [
    "Fund",
    "OverlapShift",
    "Quality",
    "SecurityChange",
    "WeeklyDigest",
    "parse_digest",
]

_SCHEMA_VERSION = 1
_STATUSES = frozenset({"ok", "new_in_window", "no_change_data"})
_CHANGE_TYPES = frozenset({"add", "drop", "weight_change"})


@dataclass(frozen=True)
class SecurityChange:
    """One security-level line of a fund that discloses securities."""

    change_type: str
    security_name: str
    security_id: str | None
    weight_before: float | None
    weight_after: float | None
    delta: float | None


@dataclass(frozen=True)
class Fund:
    """One fund's row of the weekly changes table plus its security lines."""

    ticker: str
    name: str
    status: str
    securities_disclosed: bool
    adds: int
    drops: int
    weight_changes: int
    turnover: float | None
    top10_weight_end: float | None
    flow_estimate_sum: float | None
    changes: tuple[SecurityChange, ...]


@dataclass(frozen=True)
class OverlapShift:
    """A fund pair whose overlap moved beyond the reporting threshold."""

    fund_a: str
    fund_b: str
    overlap_start: float
    overlap_end: float
    delta: float


@dataclass(frozen=True)
class Quality:
    """Publication-safe data-quality counts for one week."""

    files_included: int
    files_excluded: int
    funds_without_data: tuple[str, ...]


@dataclass(frozen=True)
class WeeklyDigest:
    """A checked digest document, ready to render."""

    week_start: date
    week_end: date
    known_by: datetime
    demo: bool
    funds: tuple[Fund, ...]
    overlap_shifts: tuple[OverlapShift, ...]
    quality: Quality


def parse_digest(document: object, *, label: str = "digest") -> WeeklyDigest:
    """Check the fields the site uses and return a :class:`WeeklyDigest`.

    ``label`` prefixes every error message (``render_site`` passes the
    digest's index). Raises ``ValueError`` for a wrong schema version or a
    malformed or missing field.
    """
    root = _object(document, label)
    where = label
    version = root.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        _fail(f"{where}.schema_version", f"must be {_SCHEMA_VERSION}, got {version!r}")
    if version != _SCHEMA_VERSION:
        _fail(f"{where}.schema_version", f"must be {_SCHEMA_VERSION}, got {version!r}")

    week_start = _date(root, "week_start", where)
    week_end = _date(root, "week_end", where)
    if week_start >= week_end:
        _fail(f"{where}.week_start", f"must be before week_end {week_end.isoformat()}")
    known_by = _datetime(root, "known_by", where)
    demo = _bool(root, "demo", where)

    funds = tuple(
        _fund(entry, f"{where}.funds[{index}]")
        for index, entry in enumerate(_array(_field(root, "funds", where), f"{where}.funds"))
    )
    tickers = [fund.ticker for fund in funds]
    duplicated = sorted({ticker for ticker in tickers if tickers.count(ticker) > 1})
    if duplicated:
        _fail(f"{where}.funds", f"has duplicate tickers {', '.join(duplicated)}")

    overlap_shifts = tuple(
        _overlap_shift(entry, f"{where}.overlap_shifts[{index}]")
        for index, entry in enumerate(
            _array(_field(root, "overlap_shifts", where), f"{where}.overlap_shifts")
        )
    )
    quality = _quality(_field(root, "quality", where), f"{where}.quality")
    return WeeklyDigest(
        week_start=week_start,
        week_end=week_end,
        known_by=known_by,
        demo=demo,
        funds=tuple(sorted(funds, key=lambda fund: fund.ticker)),
        overlap_shifts=overlap_shifts,
        quality=quality,
    )


def _fund(entry: object, where: str) -> Fund:
    obj = _object(entry, where)
    ticker = _string(obj, "ticker", where)
    securities_disclosed = _bool(obj, "securities_disclosed", where)
    counts = _object(_field(obj, "counts", where), f"{where}.counts")
    changes = (
        tuple(
            _security_change(change, f"{where}.changes[{index}]")
            for index, change in enumerate(
                _array(_field(obj, "changes", where), f"{where}.changes")
            )
        )
        if securities_disclosed
        else ()
    )
    return Fund(
        ticker=ticker,
        name=_string(obj, "name", where),
        status=_status(obj, where),
        securities_disclosed=securities_disclosed,
        adds=_integer(counts, "adds", f"{where}.counts"),
        drops=_integer(counts, "drops", f"{where}.counts"),
        weight_changes=_integer(counts, "weight_changes", f"{where}.counts"),
        turnover=_optional_number(obj, "turnover", where),
        top10_weight_end=_optional_number(obj, "top10_weight_end", where),
        flow_estimate_sum=_optional_number(obj, "flow_estimate_sum", where),
        changes=changes,
    )


def _security_change(entry: object, where: str) -> SecurityChange:
    obj = _object(entry, where)
    change_type = _string(obj, "change_type", where)
    if change_type not in _CHANGE_TYPES:
        _fail(
            f"{where}.change_type",
            f"must be one of {', '.join(sorted(_CHANGE_TYPES))}, got {change_type!r}",
        )
    return SecurityChange(
        change_type=change_type,
        security_name=_string(obj, "security_name", where),
        security_id=_optional_string(obj, "security_id", where),
        weight_before=_optional_number(obj, "weight_before", where),
        weight_after=_optional_number(obj, "weight_after", where),
        delta=_optional_number(obj, "delta", where),
    )


def _overlap_shift(entry: object, where: str) -> OverlapShift:
    obj = _object(entry, where)
    return OverlapShift(
        fund_a=_string(obj, "fund_a", where),
        fund_b=_string(obj, "fund_b", where),
        overlap_start=_number(obj, "overlap_start", where),
        overlap_end=_number(obj, "overlap_end", where),
        delta=_number(obj, "delta", where),
    )


def _quality(value: object, where: str) -> Quality:
    obj = _object(value, where)
    funds_without_data = tuple(
        _text(item, f"{where}.funds_without_data[{index}]")
        for index, item in enumerate(
            _array(_field(obj, "funds_without_data", where), f"{where}.funds_without_data")
        )
    )
    return Quality(
        files_included=_integer(obj, "files_included", where),
        files_excluded=_integer(obj, "files_excluded", where),
        funds_without_data=funds_without_data,
    )


def _status(obj: Mapping[str, Any], where: str) -> str:
    status = _string(obj, "status", where)
    if status not in _STATUSES:
        _fail(
            f"{where}.status",
            f"must be one of {', '.join(sorted(_STATUSES))}, got {status!r}",
        )
    return status


def _fail(where: str, message: str) -> NoReturn:
    raise ValueError(f"{where}: {message}" if where else message)


def _field(obj: Mapping[str, Any], key: str, where: str) -> object:
    if key not in obj:
        _fail(f"{where}.{key}" if where else key, "is missing")
    return obj[key]


def _object(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(where, f"must be an object, got {type(value).__name__}")
    return value


def _array(value: object, where: str) -> Sequence[Any]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        _fail(where, f"must be a list, got {type(value).__name__}")
    return value


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(where, "must be a non-empty string")
    return value


def _string(obj: Mapping[str, Any], key: str, where: str) -> str:
    return _text(_field(obj, key, where), f"{where}.{key}" if where else key)


def _optional_string(obj: Mapping[str, Any], key: str, where: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    return _text(value, f"{where}.{key}" if where else key)


def _bool(obj: Mapping[str, Any], key: str, where: str) -> bool:
    value = _field(obj, key, where)
    if not isinstance(value, bool):
        _fail(f"{where}.{key}", f"must be a boolean, got {value!r}")
    return value


def _integer(obj: Mapping[str, Any], key: str, where: str) -> int:
    value = _field(obj, key, where)
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{where}.{key}", f"must be an integer, got {value!r}")
    return value


def _number(obj: Mapping[str, Any], key: str, where: str) -> float:
    return _as_number(_field(obj, key, where), f"{where}.{key}" if where else key)


def _optional_number(obj: Mapping[str, Any], key: str, where: str) -> float | None:
    value = obj.get(key)
    if value is None:
        return None
    return _as_number(value, f"{where}.{key}" if where else key)


def _as_number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        _fail(where, f"must be a number or null, got {value!r}")
    return float(value)


def _date(obj: Mapping[str, Any], key: str, where: str) -> date:
    value = _string(obj, key, where)
    try:
        return date.fromisoformat(value)
    except ValueError:
        _fail(f"{where}.{key}", f"must be an ISO date (YYYY-MM-DD), got {value!r}")


def _datetime(obj: Mapping[str, Any], key: str, where: str) -> datetime:
    value = _string(obj, key, where)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _fail(f"{where}.{key}", f"must be an ISO timestamp, got {value!r}")
    if parsed.tzinfo is None:
        _fail(f"{where}.{key}", f"must include a UTC offset, got {value!r}")
    return parsed
