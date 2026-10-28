"""Schema of the weekly digest document (``schema_version`` 1, interfaces-08 §3).

``validate_digest`` is the single place that decides whether a document may be
handed to the static-site renderer, the dashboard or the CLI. It checks the
exact key set at every level, value types, the status and change-type enums,
list ordering, date and timestamp formats, six-decimal rounding, and the D79
publication rule. It raises :class:`ValueError` with the path of the first
problem, so a caller can point at the exact field::

    funds[3].counts.adds: expected >= 0, got -1

The document is JSON-ready: only ``str``, ``int``, ``float``, ``bool``,
``None``, ``list`` and ``dict`` values, and every float already rounded to
:data:`DECIMALS` places.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any, NoReturn

from etfh.analytics import FLOW_METHOD
from etfh.digest.window import known_by, week_start

__all__ = [
    "CHANGE_TYPES",
    "DECIMALS",
    "FUND_STATUSES",
    "SCHEMA_VERSION",
    "STATUS_NEW_IN_WINDOW",
    "STATUS_NO_CHANGE_DATA",
    "STATUS_OK",
    "round_value",
    "validate_digest",
]

SCHEMA_VERSION = 1

#: Decimal places every float in the document is rounded to.
DECIMALS = 6

#: The change types a comparison can report.
CHANGE_TYPES = frozenset({"add", "drop", "weight_change"})

STATUS_OK = "ok"
STATUS_NEW_IN_WINDOW = "new_in_window"
STATUS_NO_CHANGE_DATA = "no_change_data"

#: The statuses a fund entry can carry.
FUND_STATUSES = frozenset({STATUS_OK, STATUS_NEW_IN_WINDOW, STATUS_NO_CHANGE_DATA})

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "week_start",
        "week_end",
        "known_by",
        "public",
        "demo",
        "funds",
        "overlap_shifts",
        "quality",
    }
)
_FUND_KEYS = frozenset(
    {
        "ticker",
        "issuer",
        "name",
        "securities_disclosed",
        "status",
        "portfolio_date_start",
        "portfolio_date_end",
        "counts",
        "turnover",
        "top10_weight_start",
        "top10_weight_end",
        "flow_estimate_sum",
        "flow_days",
        "flow_method",
        "changes",
    }
)
_COUNT_KEYS = frozenset({"adds", "drops", "weight_changes"})
_CHANGE_KEYS = frozenset(
    {"change_type", "security_id", "security_name", "weight_before", "weight_after", "delta"}
)
_OVERLAP_KEYS = frozenset({"fund_a", "fund_b", "overlap_start", "overlap_end", "delta"})
_QUALITY_KEYS = frozenset({"files_included", "files_excluded", "funds_without_data"})

_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def round_value(value: float) -> float:
    """Round ``value`` to :data:`DECIMALS` places and normalise negative zero."""
    return round(float(value), DECIMALS) + 0.0


def validate_digest(doc: Mapping[str, Any]) -> None:
    """Validate a digest document, raising :class:`ValueError` on the first problem.

    The check is strict: keys that are not part of ``schema_version`` 1 fail as
    loudly as missing ones, so a producer and its consumers cannot drift apart
    silently.
    """
    if not isinstance(doc, Mapping):
        _fail("document", f"expected an object, got {_kind(doc)}")
    _exact_keys(doc, _TOP_LEVEL_KEYS, "document")

    version = _integer(doc["schema_version"], "schema_version", minimum=1)
    if version != SCHEMA_VERSION:
        _fail("schema_version", f"expected {SCHEMA_VERSION}, got {version}")

    end = _date(doc["week_end"], "week_end")
    start = _date(doc["week_start"], "week_start")
    expected_start = week_start(end)
    if start != expected_start:
        _fail(
            "week_start",
            f"expected {expected_start.isoformat()} (week_end minus 7 days), "
            f"got {start.isoformat()}",
        )
    _timestamp(doc["known_by"], "known_by", end)
    public = _boolean(doc["public"], "public")
    _boolean(doc["demo"], "demo")

    entries = _list(doc["funds"], "funds")
    tickers: list[str] = []
    for index, entry in enumerate(entries):
        tickers.append(_fund_entry(entry, f"funds[{index}]", public=public))
    _check_order(
        tickers,
        sorted(tickers),
        "funds",
        "funds must be sorted by ticker",
    )
    _check_unique(tickers, "funds", "ticker")

    _overlap_shifts(doc["overlap_shifts"], set(tickers))
    _quality(doc["quality"], set(tickers))


def _fund_entry(entry: object, path: str, *, public: bool) -> str:
    fields = _mapping(entry, path)
    _exact_keys(fields, _FUND_KEYS, path)
    ticker = _text(fields["ticker"], f"{path}.ticker")
    _text(fields["issuer"], f"{path}.issuer")
    _text(fields["name"], f"{path}.name")
    disclosed = _boolean(fields["securities_disclosed"], f"{path}.securities_disclosed")
    status = _enum(fields["status"], f"{path}.status", FUND_STATUSES)
    _optional_date(fields["portfolio_date_start"], f"{path}.portfolio_date_start")
    _optional_date(fields["portfolio_date_end"], f"{path}.portfolio_date_end")

    counts = _mapping(fields["counts"], f"{path}.counts")
    _exact_keys(counts, _COUNT_KEYS, f"{path}.counts")
    numbers = {key: _integer(counts[key], f"{path}.counts.{key}", minimum=0) for key in _COUNT_KEYS}

    turnover = _number(fields["turnover"], f"{path}.turnover", minimum=0.0)
    top_start = _number(fields["top10_weight_start"], f"{path}.top10_weight_start")
    top_end = _number(fields["top10_weight_end"], f"{path}.top10_weight_end")
    flow_sum = fields["flow_estimate_sum"]
    if flow_sum is not None:
        _number(flow_sum, f"{path}.flow_estimate_sum")
    flow_days = _integer(fields["flow_days"], f"{path}.flow_days", minimum=0)
    flow_method = _text(fields["flow_method"], f"{path}.flow_method")
    if flow_method != FLOW_METHOD:
        _fail(
            f"{path}.flow_method",
            f"expected the etfh.analytics.FLOW_METHOD label {FLOW_METHOD!r}, got {flow_method!r}",
        )
    if flow_sum is None and flow_days != 0:
        _fail(f"{path}.flow_days", "must be 0 when flow_estimate_sum is null")

    if not public and not disclosed:
        _fail(
            f"{path}.securities_disclosed",
            "must be true in an internal document (public=false; D79)",
        )

    changes = _list(fields["changes"], f"{path}.changes")
    if not disclosed and changes:
        _fail(
            f"{path}.changes",
            "must be empty when securities_disclosed is false (D79)",
        )
    listed = dict.fromkeys(CHANGE_TYPES, 0)
    identifiers: list[str] = []
    deltas: list[float] = []
    for index, change in enumerate(changes):
        change_path = f"{path}.changes[{index}]"
        kind, identifier, delta = _change(change, change_path)
        listed[kind] += 1
        identifiers.append(identifier)
        deltas.append(delta)
    _check_unique(identifiers, f"{path}.changes", "security_id")
    expected = [
        (-abs(delta), identifier) for delta, identifier in zip(deltas, identifiers, strict=True)
    ]
    _check_order(
        expected,
        sorted(expected),
        f"{path}.changes",
        "changes must be ordered by |delta| descending, then security_id",
    )
    for kind, count_key in (
        ("add", "adds"),
        ("drop", "drops"),
        ("weight_change", "weight_changes"),
    ):
        if listed[kind] > numbers[count_key]:
            _fail(
                f"{path}.changes",
                f"lists {listed[kind]} {kind} line(s) but counts.{count_key} is "
                f"{numbers[count_key]}",
            )

    if status == STATUS_NO_CHANGE_DATA:
        if any(numbers.values()) or turnover != 0.0 or changes:
            _fail(
                f"{path}.status",
                "no_change_data requires zero counts, zero turnover and no changes",
            )
        if top_start != top_end:
            _fail(
                f"{path}.top10_weight_end",
                "no_change_data requires the same concentration at both ends",
            )
    return ticker


def _change(change: object, path: str) -> tuple[str, str, float]:
    entry = _mapping(change, path)
    _exact_keys(entry, _CHANGE_KEYS, path)
    kind = _enum(entry["change_type"], f"{path}.change_type", CHANGE_TYPES)
    identifier = _text(entry["security_id"], f"{path}.security_id")
    _text(entry["security_name"], f"{path}.security_name")
    before = _number(entry["weight_before"], f"{path}.weight_before", minimum=-1.0, maximum=1.0)
    after = _number(entry["weight_after"], f"{path}.weight_after", minimum=-1.0, maximum=1.0)
    delta = _number(entry["delta"], f"{path}.delta")
    if delta != round_value(after - before):
        _fail(
            f"{path}.delta",
            f"expected weight_after - weight_before = {round_value(after - before)!r}, "
            f"got {delta!r}",
        )
    if kind == "add" and before != 0.0:
        _fail(f"{path}.weight_before", "must be 0.0 for an add")
    if kind == "drop" and after != 0.0:
        _fail(f"{path}.weight_after", "must be 0.0 for a drop")
    # A position can be smaller than one unit in the sixth decimal, so a rounded
    # weight of 0.0 on one side of a weight change is legitimate; only a zero
    # delta is impossible for a weight change.
    if kind == "weight_change" and delta == 0.0:
        _fail(f"{path}.delta", "a weight_change needs a non-zero delta")
    return kind, identifier, delta


def _overlap_shifts(value: object, tickers: set[str]) -> None:
    entries = _list(value, "overlap_shifts")
    pairs: list[tuple[str, str]] = []
    shifts: list[float] = []
    for index, entry in enumerate(entries):
        path = f"overlap_shifts[{index}]"
        shift = _mapping(entry, path)
        _exact_keys(shift, _OVERLAP_KEYS, path)
        fund_a = _text(shift["fund_a"], f"{path}.fund_a")
        fund_b = _text(shift["fund_b"], f"{path}.fund_b")
        if fund_a >= fund_b:
            _fail(path, "fund_a must sort before fund_b")
        for role, ticker in (("fund_a", fund_a), ("fund_b", fund_b)):
            if ticker not in tickers:
                _fail(f"{path}.{role}", f"fund {ticker!r} is not in funds")
        overlap_start = _number(shift["overlap_start"], f"{path}.overlap_start")
        overlap_end = _number(shift["overlap_end"], f"{path}.overlap_end")
        delta = _number(shift["delta"], f"{path}.delta")
        expected = round_value(overlap_end - overlap_start)
        if delta != expected:
            _fail(
                f"{path}.delta",
                f"expected overlap_end - overlap_start = {expected!r}, got {delta!r}",
            )
        pairs.append((fund_a, fund_b))
        shifts.append(delta)
    _check_unique(pairs, "overlap_shifts", "pair")
    expected_order = [
        (-abs(delta), fund_a, fund_b) for (fund_a, fund_b), delta in zip(pairs, shifts, strict=True)
    ]
    _check_order(
        expected_order,
        sorted(expected_order),
        "overlap_shifts",
        "overlap_shifts must be ordered by |delta| descending, then fund_a and fund_b",
    )


def _quality(value: object, tickers: set[str]) -> None:
    quality = _mapping(value, "quality")
    _exact_keys(quality, _QUALITY_KEYS, "quality")
    _integer(quality["files_included"], "quality.files_included", minimum=0)
    _integer(quality["files_excluded"], "quality.files_excluded", minimum=0)
    without = _list(quality["funds_without_data"], "quality.funds_without_data")
    names = [
        _text(item, f"quality.funds_without_data[{index}]") for index, item in enumerate(without)
    ]
    _check_order(
        names,
        sorted(names),
        "quality.funds_without_data",
        "funds_without_data must be sorted",
    )
    _check_unique(names, "quality.funds_without_data", "ticker")
    for index, name in enumerate(names):
        if name in tickers:
            _fail(
                f"quality.funds_without_data[{index}]",
                f"fund {name!r} also appears in funds",
            )


def _check_order(actual: list[Any], expected: list[Any], path: str, message: str) -> None:
    if actual == expected:
        return
    for index, (got, want) in enumerate(zip(actual, expected, strict=True)):
        if got != want:
            _fail(f"{path}[{index}]", f"{message} (expected {want!r}, got {got!r})")
    _fail(path, message)


def _check_unique(values: list[Any], path: str, what: str) -> None:
    seen: set[Any] = set()
    for index, value in enumerate(values):
        if value in seen:
            _fail(f"{path}[{index}]", f"duplicate {what} {value!r}")
        seen.add(value)


def _fail(path: str, message: str) -> NoReturn:
    raise ValueError(f"{path}: {message}")


def _kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list | tuple):
        return "array"
    return type(value).__name__


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(path, f"expected an object, got {_kind(value)}")
    return value


def _list(value: object, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(path, f"expected an array, got {_kind(value)}")
    return value


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], path: str) -> None:
    keys = set(value.keys())
    missing = expected - keys
    if missing:
        _fail(path, f"missing key {sorted(missing)[0]!r}")
    extra = keys - expected
    if extra:
        _fail(path, f"unexpected key {sorted(repr(key) for key in extra)[0]}")


def _text(value: object, path: str) -> str:
    if not isinstance(value, str):
        _fail(path, f"expected a string, got {_kind(value)}")
    if not value:
        _fail(path, "expected a non-empty string")
    return value


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, f"expected a boolean, got {_kind(value)}")
    return value


def _enum(value: object, path: str, allowed: frozenset[str]) -> str:
    text = _text(value, path)
    if text not in allowed:
        _fail(path, f"expected one of {sorted(allowed)}, got {text!r}")
    return text


def _integer(value: object, path: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, f"expected an integer, got {_kind(value)}")
    if minimum is not None and value < minimum:
        _fail(path, f"expected >= {minimum}, got {value}")
    return value


def _number(
    value: object,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        _fail(path, f"expected a number, got {_kind(value)}")
    number = float(value)
    if not math.isfinite(number):
        _fail(path, f"expected a finite number, got {number!r}")
    if round(number, DECIMALS) != number:
        _fail(path, f"{number!r} is not rounded to {DECIMALS} decimal places")
    if minimum is not None and number < minimum:
        _fail(path, f"expected >= {minimum}, got {number!r}")
    if maximum is not None and number > maximum:
        _fail(path, f"expected <= {maximum}, got {number!r}")
    return number


def _date(value: object, path: str) -> date:
    text = _text(value, path)
    if _DATE_PATTERN.fullmatch(text) is None:
        _fail(path, f"expected a YYYY-MM-DD date, got {text!r}")
    try:
        return date.fromisoformat(text)
    except ValueError:
        _fail(path, f"invalid date {text!r}")


def _optional_date(value: object, path: str) -> date | None:
    if value is None:
        return None
    return _date(value, path)


def _timestamp(value: object, path: str, end: date) -> None:
    text = _text(value, path)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        _fail(path, f"expected an ISO-8601 timestamp, got {text!r}")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _fail(path, f"expected a UTC timestamp, got {text!r}")
    expected = known_by(end)
    if text != expected:
        _fail(path, f"expected {expected!r} (cutoff_utc for week_end), got {text!r}")
