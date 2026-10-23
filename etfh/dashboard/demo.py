"""Synthetic demo mode for the public dashboard deployment (D62).

The public demo must never show real issuer holdings, and it must work on a
host that has no raw store at all. Demo mode therefore builds the committed
synthetic fixtures into a fresh temporary directory at startup and points the
app at that database:

- enable it with ``ETFH_DEMO=1`` (or any of ``true``/``yes``/``on``) or by
  passing ``--demo`` after Streamlit's ``--`` separator;
- ``ETFH_DB`` is ignored, so a real store on the same machine is never opened;
- the build reads local CSV/XLSX/JSON/PDF fixtures only, so it never touches
  the network;
- the result is cached with :func:`streamlit.cache_resource`, so the build
  runs once per server process rather than on every interaction.

Streamlit Community Cloud needs no dependency file for this app: the official
docs list ``uv.lock`` as the first recognised dependency file and Community
Cloud runs ``uv`` from the repository root, where this project commits
``pyproject.toml`` and ``uv.lock``.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import streamlit as st

from etfh.build import build


def _find_repo_root() -> Path:
    """Locate the checkout that holds the fixtures and config.

    Works for a source checkout, an editable install and a wheel installed by
    Streamlit Community Cloud: walk up from this file first, then fall back to
    the working directory (which Cloud always sets to the repository root).
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "tests" / "fixtures" / "raw_synthetic").is_dir() and (
            parent / "config" / "sources.yaml"
        ).is_file():
            return parent
    working_directory = Path.cwd()
    if (working_directory / "tests" / "fixtures" / "raw_synthetic").is_dir():
        return working_directory
    return here.parents[2]


REPO_ROOT = _find_repo_root()

DEMO_RAW_DIR = REPO_ROOT / "tests" / "fixtures" / "raw_synthetic"
"""Committed synthetic raw store (D31); never a real issuer store."""

DEMO_CONFIG_DIR = REPO_ROOT / "config"
"""Committed source, fund and validation config."""

DEMO_BANNER = (
    "Demo data — synthetic funds only. No live issuer data is collected, stored or shown here."
)
"""Banner shown above every demo page."""

DEMO_LATEST_ASOF = date(2100, 1, 1)
"""Sentinel as-of date used to select a fund's newest known version in demo mode.

The synthetic fixtures carry dates in 2026, so using today's date would show an
empty portfolio whenever the demo runs before those dates are known. A far
future date asks the analytics layer for the latest version it has instead of
hard-coding a fixture date.
"""

DEMO_ENV = "ETFH_DEMO"
"""Environment variable that enables demo mode."""

DEMO_FLAG = "--demo"
"""Command-line switch that enables demo mode."""

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class DemoError(RuntimeError):
    """The committed demo fixtures are missing or cannot be built."""


@dataclass(frozen=True, slots=True)
class DemoDatabase:
    """The temporary database built from the synthetic fixtures."""

    db_path: Path
    raw_dir: Path
    _tempdir: tempfile.TemporaryDirectory[str] = field(repr=False, compare=False)


def is_demo(
    environ: Mapping[str, str] | None = None,
    argv: Sequence[str] | None = None,
) -> bool:
    """True when demo mode is requested by the environment or the command line.

    ``environ`` and ``argv`` are injectable so tests do not have to mutate the
    process environment or the real ``sys.argv``.
    """
    values = os.environ if environ is None else environ
    arguments = sys.argv[1:] if argv is None else argv
    if any(argument == DEMO_FLAG for argument in arguments):
        return True
    return values.get(DEMO_ENV, "").strip().casefold() in _TRUTHY


def build_demo_database(
    *,
    raw_dir: Path = DEMO_RAW_DIR,
    config_dir: Path = DEMO_CONFIG_DIR,
) -> DemoDatabase:
    """Build ``raw_dir`` into a temporary directory and return its database.

    Raises :class:`DemoError` when the fixtures are absent, which is the clear
    failure mode for an installed package that does not ship ``tests/``.
    """
    raw_dir = Path(raw_dir)
    if not raw_dir.is_dir():
        raise DemoError(
            f"Demo fixtures not found at {raw_dir}. Run the dashboard from a "
            "clone of the repository so tests/fixtures/raw_synthetic exists."
        )
    tempdir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(prefix="etfh-demo-")
    out_dir = Path(tempdir.name)
    try:
        build(raw_dir, out_dir, config_dir=Path(config_dir))
    except Exception as exc:  # pragma: no cover - defensive cleanup
        tempdir.cleanup()
        raise DemoError(f"Could not build the synthetic demo database: {exc}") from exc
    return DemoDatabase(db_path=out_dir / "etfh.duckdb", raw_dir=raw_dir, _tempdir=tempdir)


@st.cache_resource(show_spinner="Building the synthetic demo database…")
def demo_database() -> DemoDatabase:
    """Process-wide cached demo build, created on first use."""
    return build_demo_database()
