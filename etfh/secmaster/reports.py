"""Unmatched report and fuzzy match proposals (D23).

An unmatched holding is never merged automatically: it is listed in the
unmatched report, and rapidfuzz name similarity only writes *proposals* to a
review file. A proposal becomes real only after a human copies it into
``config/security_overrides.csv`` (D23, D41).
"""

from __future__ import annotations

import re
import unicodedata

import polars as pl
from rapidfuzz import fuzz

__all__ = ["fuzzy_proposals", "normalize_name", "unmatched_report"]

_REPORT_COLUMNS = (
    "fund_ticker",
    "as_of_date",
    "security_name",
    "security_ticker",
    "exchange",
    "source_id",
    "raw_file_sha256",
)

_REPORT_SORT_COLUMNS = (
    "fund_ticker",
    "as_of_date",
    "security_name",
    "security_ticker",
    "source_id",
    "raw_file_sha256",
)

_PROPOSAL_SCHEMA = {
    "security_name": pl.String,
    "candidate_security_id": pl.String,
    "candidate_name": pl.String,
    "score": pl.Float64,
}

_CORPORATE_SUFFIXES = frozenset(
    {
        "AG",
        "CO",
        "COMPANY",
        "CORP",
        "CORPORATION",
        "GMBH",
        "HOLDING",
        "HOLDINGS",
        "INC",
        "INCORPORATED",
        "LIMITEE",
        "LLC",
        "LP",
        "LLP",
        "LTD",
        "LIMITED",
        "LTEE",
        "NV",
        "PLC",
        "SA",
        "TRUST",
        "ULC",
    }
)

_NON_SECURITY_PREFIXES = ("CASH:", "DERIV:")


def normalize_name(name: str) -> str:
    """Normalize a security name for fuzzy matching.

    Upper-cases, drops accents, replaces punctuation with spaces and removes
    trailing corporate suffixes (Inc, Corp, Ltd, Limited, Holdings, ...).
    This only feeds rapidfuzz proposals; it is never used to merge rows.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    tokens = re.sub(r"[^0-9A-Z]+", " ", ascii_name.upper()).split()
    while tokens and tokens[-1] in _CORPORATE_SUFFIXES:
        tokens.pop()
    if tokens:
        return " ".join(tokens)
    return " ".join(re.sub(r"[^0-9A-Z]+", " ", ascii_name.upper()).split())


def unmatched_report(frame: pl.DataFrame) -> pl.DataFrame:
    """Return the rows whose ``security_id`` is null, as a deterministic table.

    Columns are exactly ``fund_ticker, as_of_date, security_name,
    security_ticker, exchange, source_id, raw_file_sha256``; rows are sorted
    on a stable key so the report is reproducible. ``frame`` must carry a
    ``security_id`` column (run :meth:`SecurityMaster.assign` first).
    """
    if "security_id" not in frame.columns:
        raise ValueError("frame has no security_id column; run SecurityMaster.assign first")
    missing = [column for column in _REPORT_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"frame is missing columns: {', '.join(missing)}")
    return (
        frame.filter(pl.col("security_id").is_null())
        .select(_REPORT_COLUMNS)
        .sort(_REPORT_SORT_COLUMNS)
    )


def fuzzy_proposals(
    unmatched: pl.DataFrame,
    known: pl.DataFrame,
    threshold: int = 92,
) -> pl.DataFrame:
    """Propose known securities for unmatched names, for human review only.

    Names are compared with rapidfuzz ``token_sort_ratio`` after
    :func:`normalize_name`. The result has one row per (unmatched name,
    candidate ``security_id``) whose best-scoring candidate name reaches
    ``threshold``; cash and derivative ids are never proposed. Output is
    sorted by ``security_name``, then score descending, then
    ``candidate_security_id`` ascending, so it is byte-stable. Proposals are
    never applied: applying :meth:`SecurityMaster.assign` again after
    generating them yields identical ids.
    """
    if not 0 <= threshold <= 100:
        raise ValueError("threshold must be between 0 and 100")
    if "security_name" not in unmatched.columns:
        raise ValueError("unmatched frame has no security_name column")
    missing = [column for column in ("security_id", "security_name") if column not in known.columns]
    if missing:
        raise ValueError(f"known frame is missing columns: {', '.join(missing)}")

    unmatched_names = sorted(
        {name for name in unmatched["security_name"].to_list() if name is not None and name.strip()}
    )
    candidate_names: dict[str, list[str]] = {}
    for security_id, security_name in (
        known.select("security_id", "security_name")
        .filter(pl.col("security_id").is_not_null())
        .unique()
        .sort("security_id", "security_name")
        .iter_rows()
    ):
        if security_id.startswith(_NON_SECURITY_PREFIXES):
            continue
        if security_name is None or not security_name.strip():
            continue
        candidate_names.setdefault(security_id, []).append(security_name)

    rows: list[dict[str, object]] = []
    for name in unmatched_names:
        normalized = normalize_name(name)
        best: dict[str, tuple[float, str]] = {}
        for security_id in sorted(candidate_names):
            for candidate_name in candidate_names[security_id]:
                score = float(fuzz.token_sort_ratio(normalized, normalize_name(candidate_name)))
                if score < threshold:
                    continue
                previous = best.get(security_id)
                if previous is None or score > previous[0]:
                    best[security_id] = (score, candidate_name)
        for security_id, (score, candidate_name) in best.items():
            rows.append(
                {
                    "security_name": name,
                    "candidate_security_id": security_id,
                    "candidate_name": candidate_name,
                    "score": score,
                }
            )
    if not rows:
        return pl.DataFrame(schema=_PROPOSAL_SCHEMA)
    return pl.DataFrame(rows, schema=_PROPOSAL_SCHEMA).sort(
        ["security_name", "score", "candidate_security_id"],
        descending=[False, True, False],
    )
