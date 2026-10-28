"""Integrity checks for the raw store.

The raw store (D20) is a directory tree of unmodified issuer files, each with a
``<fetched_at>.meta.json`` sidecar recording its SHA-256. D77 moves that tree
into a separate private data repository, so the tree needs a check that is safe
outside the collector which wrote it: ``verify_raw_store`` walks the store once,
recomputes every data file's SHA-256 in chunks and reports *all* problems
instead of stopping at the first one.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from etfh.storage import read_meta

__all__ = [
    "ProblemKind",
    "RawCheckReport",
    "RawProblem",
    "verify_raw_store",
]

ProblemKind = Literal[
    "missing_sidecar",
    "orphan_sidecar",
    "sha_mismatch",
    "bad_sidecar",
    "unexpected_path",
]

_META_SUFFIX = ".meta.json"
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STAMP = re.compile(r"^\d{8}T\d{6}Z$")
_EXT = re.compile(r"^[a-z0-9]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class RawProblem:
    """One problem found in a raw store.

    ``path`` is relative to the store root and POSIX style. It names the file
    the problem is about: the data file for ``missing_sidecar`` and
    ``sha_mismatch``, the sidecar for ``orphan_sidecar`` and ``bad_sidecar``,
    and the misplaced entry itself for ``unexpected_path``.
    """

    path: str
    kind: ProblemKind
    detail: str


@dataclass(frozen=True)
class RawCheckReport:
    """Outcome of :func:`verify_raw_store`."""

    files_checked: int
    problems: tuple[RawProblem, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class SyncReport:
    """Outcome of :func:`sync_raw_store`.

    ``copied`` lists the data files (relative POSIX paths) whose pair was
    written, or would be written on a dry run. ``identical`` counts pairs
    already present with the same bytes. ``conflicts`` lists relative paths
    where the destination holds different bytes; nothing was written for them.
    """

    copied: tuple[str, ...]
    identical: int
    conflicts: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.conflicts


def _iter_entries(root: Path) -> Iterator[Path]:
    """Every entry under ``root`` in a stable order, without following symlinks."""
    entries = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    yield from entries


def _valid_stamp(stem: str) -> bool:
    if not _STAMP.fullmatch(stem):
        return False
    try:
        datetime(
            int(stem[0:4]),
            int(stem[4:6]),
            int(stem[6:8]),
            int(stem[9:11]),
            int(stem[11:13]),
            int(stem[13:15]),
            tzinfo=UTC,
        )
    except ValueError:
        return False
    return True


def _layout_problem(relative: Path, *, is_sidecar: bool) -> str | None:
    """Return why ``relative`` is not a raw-store path, or ``None`` when it is."""
    parts = relative.parts
    if len(parts) != 4:
        return "expected <issuer>/<ticker>/<YYYY-MM-DD>/<fetched_at>.<ext>"
    issuer, ticker, day, name = parts
    for segment in (issuer, ticker):
        if not segment or segment.startswith("."):
            return f"invalid directory name {segment!r}"
    if not _DATE_DIR.fullmatch(day):
        return f"invalid as-of directory {day!r} (expected YYYY-MM-DD)"
    try:
        date.fromisoformat(day)
    except ValueError:
        return f"invalid as-of directory {day!r} (not a calendar date)"
    if is_sidecar:
        if not name.endswith(_META_SUFFIX):
            return f"sidecar name {name!r} does not end with {_META_SUFFIX}"
        stem = name[: -len(_META_SUFFIX)]
    else:
        stem, dot, ext = name.rpartition(".")
        if not dot or not _EXT.fullmatch(ext):
            return f"invalid file extension in {name!r}"
    if not _valid_stamp(stem):
        return f"invalid fetch stamp {stem!r} (expected YYYYMMDDTHHMMSSZ)"
    return None


def _sidecar_for(data_relative: Path) -> Path:
    return data_relative.with_name(f"{data_relative.stem}{_META_SUFFIX}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _is_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _data_files(root: Path) -> list[Path]:
    """Valid-layout data files relative to ``root``, sorted."""
    data: list[Path] = []
    for path in _iter_entries(root):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root)
        if path.name.endswith(_META_SUFFIX):
            continue
        if _layout_problem(relative, is_sidecar=False) is None:
            data.append(relative)
    return data


def verify_raw_store(root: Path) -> RawCheckReport:
    """Check every data file against its sidecar and report every problem.

    ``files_checked`` counts the data files found at a valid store path. A data
    file without a sidecar, a sidecar without a data file, unreadable metadata,
    a SHA-256 mismatch and anything outside the layout each become one
    :class:`RawProblem`; verification continues after each of them.

    Raises :class:`FileNotFoundError` when ``root`` is not a directory, so a
    typo cannot silently verify an empty store.
    """
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"raw store root is not a directory: {root}")

    problems: list[RawProblem] = []
    data_files: list[Path] = []
    sidecars: set[Path] = set()
    for path in _iter_entries(root):
        relative = path.relative_to(root)
        if path.is_symlink():
            problems.append(RawProblem(relative.as_posix(), "unexpected_path", "symbolic link"))
            continue
        if not path.is_file():
            continue
        is_sidecar = path.name.endswith(_META_SUFFIX)
        layout = _layout_problem(relative, is_sidecar=is_sidecar)
        if layout is not None:
            problems.append(RawProblem(relative.as_posix(), "unexpected_path", layout))
            continue
        if is_sidecar:
            sidecars.add(relative)
        else:
            data_files.append(relative)

    # Group by fetch stamp: two data files with different extensions but the
    # same stamp would share one sidecar, which the store cannot represent.
    groups: dict[tuple[Path, str], list[Path]] = {}
    for data_relative in data_files:
        groups.setdefault((data_relative.parent, data_relative.stem), []).append(data_relative)

    paired_sidecars: set[Path] = set()
    for (parent, stem), members in sorted(groups.items(), key=lambda item: str(item[0])):
        if len(members) > 1:
            detail = f"{len(members)} data files share the fetch stamp {stem}"
            for member in members:
                problems.append(RawProblem(member.as_posix(), "unexpected_path", detail))
            paired_sidecars.add(parent / f"{stem}{_META_SUFFIX}")
            continue

        data_relative = members[0]
        sidecar_relative = _sidecar_for(data_relative)
        if sidecar_relative not in sidecars:
            problems.append(
                RawProblem(
                    data_relative.as_posix(),
                    "missing_sidecar",
                    f"expected {sidecar_relative.name} next to the data file",
                )
            )
            continue
        paired_sidecars.add(sidecar_relative)
        try:
            meta = read_meta(root / sidecar_relative)
        except (OSError, ValueError) as exc:
            problems.append(
                RawProblem(
                    sidecar_relative.as_posix(),
                    "bad_sidecar",
                    f"cannot read metadata: {exc}",
                )
            )
            continue
        recorded = meta.get("sha256")
        if not isinstance(recorded, str) or not _SHA256.fullmatch(recorded):
            problems.append(
                RawProblem(
                    sidecar_relative.as_posix(),
                    "bad_sidecar",
                    "metadata has no valid lowercase sha256",
                )
            )
            continue
        actual = _sha256_file(root / data_relative)
        if actual != recorded:
            problems.append(
                RawProblem(
                    data_relative.as_posix(),
                    "sha_mismatch",
                    f"sidecar sha256 {recorded[:12]}… does not match file sha256 {actual[:12]}…",
                )
            )

    for sidecar_relative in sorted(sidecars - paired_sidecars):
        stem = sidecar_relative.name[: -len(_META_SUFFIX)]
        problems.append(
            RawProblem(
                sidecar_relative.as_posix(),
                "orphan_sidecar",
                f"no data file for fetch stamp {stem}",
            )
        )

    problems.sort(key=lambda problem: (problem.path, problem.kind, problem.detail))
    return RawCheckReport(files_checked=len(data_files), problems=tuple(problems))
