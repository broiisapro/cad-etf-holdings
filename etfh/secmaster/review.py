"""Approve name proposals into the reviewed overrides file (D54).

The build only ever *proposes* matches; this module is how a human turns a
proposal into a real decision. :func:`approve` selects proposals (a whole
confidence class, named normalized names, or both) and returns global
``name`` overrides; :func:`append_overrides` writes them to
``config/security_overrides.csv`` without touching a single existing row.

Neither function runs during a build, and neither merges anything by itself:
the overrides file stays the only source of reviewed name matches.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import polars as pl

from etfh.secmaster.overrides import Override, load_overrides

__all__ = ["approve", "append_overrides"]

_HEADER = "match_type,match_value,security_id,reviewer,reviewed_on,note"
_CONFIDENCES = frozenset({"exact", "fuzzy"})
_PROPOSAL_COLUMNS = (
    "normalized_name",
    "candidate_security_id",
    "candidate_name",
    "score",
    "confidence",
)


def approve(
    proposals: pl.DataFrame,
    *,
    confidence: str | None = None,
    names: Sequence[str] = (),
    reviewer: str,
    reviewed_on: date,
) -> list[Override]:
    """Turn selected proposals into global name overrides for review.

    Selection is either a confidence class (``"exact"`` or ``"fuzzy"``), one
    or more normalized names, or both (intersection). A selected name must
    resolve to exactly one candidate security id: a name with two ids is
    ambiguous by construction (share classes, dual listings) and raises
    ``ValueError`` instead of picking one. ``reviewer`` must be non-empty:
    an approval without a name attached is not a review.

    The returned overrides use ``match_type="name"`` and
    ``match_value="*|<normalized name>"``, sorted by that value so repeated
    runs are deterministic. Nothing is written here; use
    :func:`append_overrides` for that.
    """
    reviewer_name = reviewer.strip()
    if not reviewer_name:
        raise ValueError("reviewer must not be empty")
    if confidence is not None and confidence not in _CONFIDENCES:
        raise ValueError(f"confidence must be one of {sorted(_CONFIDENCES)} or None")
    wanted = tuple(name.strip() for name in names if name and name.strip())
    if confidence is None and not wanted:
        raise ValueError("approve requires confidence=... or at least one name")
    missing = [column for column in _PROPOSAL_COLUMNS if column not in proposals.columns]
    if missing:
        raise ValueError(f"proposals frame is missing columns: {', '.join(missing)}")

    selected = proposals
    if confidence is not None:
        selected = selected.filter(pl.col("confidence") == confidence)
    if wanted:
        selected = selected.filter(pl.col("normalized_name").is_in(list(wanted)))
    if selected.height == 0:
        return []

    best_score: dict[str, float] = {}
    best_confidence: dict[str, str] = {}
    ids: dict[str, set[str]] = {}
    for row in selected.iter_rows(named=True):
        normalized = str(row["normalized_name"])
        security_id = str(row["candidate_security_id"])
        ids.setdefault(normalized, set()).add(security_id)
        score = float(row["score"])
        if normalized not in best_score or score > best_score[normalized]:
            best_score[normalized] = score
            best_confidence[normalized] = str(row["confidence"])
    ambiguous = sorted(name for name, candidates in ids.items() if len(candidates) != 1)
    if ambiguous:
        raise ValueError(
            "cannot approve ambiguous name(s) with more than one candidate id: "
            + ", ".join(ambiguous)
        )

    overrides: list[Override] = []
    for normalized in sorted(ids):
        overrides.append(
            Override(
                match_type="name",
                match_value=f"*|{normalized}",
                security_id=next(iter(ids[normalized])),
                reviewer=reviewer_name,
                reviewed_on=reviewed_on,
                note=(
                    "approved name proposal "
                    f"(confidence={best_confidence[normalized]}, "
                    f"score={best_score[normalized]:.1f})"
                ),
            )
        )
    return overrides


def append_overrides(path: Path, overrides: Sequence[Override]) -> int:
    """Append new overrides to the reviewed CSV and return how many were added.

    The file keeps its exact header and its existing rows byte for byte, in
    their existing order; new rows are appended in a stable sort by
    ``match_type`` and ``match_value``. A row whose ``(match_type,
    match_value)`` key already exists is skipped when it carries the same
    ``security_id`` and raises ``ValueError`` when it would change an existing
    reviewed decision to a different id. Nothing is written when no row is
    new, so re-running an approval is a no-op.

    A missing file is created with the contract header. The write is atomic
    (temporary file next to ``path``, then replace).
    """
    path = Path(path)
    existing = load_overrides(path) if path.exists() else []
    existing_ids: dict[tuple[str, str], str] = {
        (item.match_type, item.match_value): item.security_id for item in existing
    }
    added: dict[tuple[str, str], Override] = {}
    for override in overrides:
        key: tuple[str, str] = (override.match_type, override.match_value)
        known = existing_ids.get(key)
        if known is not None:
            if known != override.security_id:
                raise ValueError(
                    f"{path}: {override.match_type} {override.match_value!r} is already "
                    f"reviewed as {known}, refusing to change it to {override.security_id}"
                )
            continue
        previous = added.get(key)
        if previous is not None:
            if previous.security_id != override.security_id:
                raise ValueError(
                    f"conflicting approvals for {override.match_type} "
                    f"{override.match_value!r}: {previous.security_id} and "
                    f"{override.security_id}"
                )
            continue
        added[key] = override
    if not added:
        return 0

    if path.exists():
        text = path.read_bytes().decode("utf-8")
        if text and not text.endswith("\n"):
            text += "\n"
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = _HEADER + "\n"
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for key in sorted(added):
        override = added[key]
        writer.writerow(
            [
                override.match_type,
                override.match_value,
                override.security_id,
                override.reviewer,
                override.reviewed_on.isoformat(),
                override.note,
            ]
        )
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes((text + buffer.getvalue()).encode("utf-8"))
    temporary.replace(path)
    return len(added)
