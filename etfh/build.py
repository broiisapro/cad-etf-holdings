"""Deterministic rebuild of the derived tables from the raw store.

``build`` reads every :class:`~etfh.types.RawFile` in the raw store, finds a
parser, applies the security master and validation, and writes
``<out>/etfh.duckdb`` plus Parquet exports and review reports. The build is
always a full rebuild: the same raw store produces byte-identical tables (D19,
D15).

The implementation lands in the build issue; this module currently fixes the
shared interface (``BuildResult`` and the dependency-injection signature) that
the CLI and tests build against.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from etfh.secmaster.resolve import SecurityMaster
    from etfh.validate import QaFinding

__all__ = ["BuildResult", "build"]


@dataclass(frozen=True)
class BuildResult:
    """Summary of one rebuild.

    Counts are rows written to each table; ``n_file_versions`` counts every
    stored version (included and excluded), while ``n_holdings`` covers the
    included files only.
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


def build(
    raw_root: Path,
    out_dir: Path,
    *,
    config_dir: Path = Path("config"),
    security_master: SecurityMaster | None = None,
    validator: Callable[..., list[QaFinding]] | None = None,
) -> BuildResult:
    """Rebuild DuckDB, Parquet exports and reports from ``raw_root``.

    ``security_master`` and ``validator`` are injection points: when omitted,
    the real :class:`~etfh.secmaster.resolve.SecurityMaster` and
    :func:`etfh.validate.validate_file` are imported lazily. Tests pass fakes
    that follow the same contracts.
    """
    raise NotImplementedError
