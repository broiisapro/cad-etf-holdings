"""CLI surface of the phase-3 commands: arguments, gates, keys and exit codes.

The happy paths live next to the behaviour they prove (``tests/test_weekly.py``
for the digest site, ``tests/test_e2e_phase3.py`` for the event study and the
raw-store tools). This file covers what a user types wrongly or what the terms
gate and the environment refuse: argument validation, the ``skipped: terms``
line for a source recorded as ``pending_review`` (D3), the missing
``TIINGO_API_KEY`` message, and the 1/2 exit codes those paths must return.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from etfh.cli import app
from etfh.prices import CLOSE_SCHEMA
from tests._build_helpers import CONFIG_DIR, RAW_ROOT

runner = CliRunner()
_WIDE = {"COLUMNS": "200"}
NEW_COMMANDS = ("weekly", "prices", "event-study", "verify-raw", "sync-raw")


def _run(*args: str) -> object:
    return runner.invoke(app, list(args), env=_WIDE)


def _config_with_price_decision(tmp_path: Path, decision: str) -> Path:
    """A copy of the repository config with ``tiingo_prices`` set to ``decision``."""
    config = tmp_path / "config"
    config.mkdir()
    for name in ("funds.csv", "validation.yaml"):
        shutil.copy(CONFIG_DIR / name, config / name)
    sources = (CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8")
    marker = "  - id: tiingo_prices"
    start = sources.index(marker)
    end = sources.find("\n  - id: ", start + len(marker))
    block = sources[start : end if end != -1 else len(sources)]
    replaced = block.replace("decision: allowed_with_conditions", f"decision: {decision}")
    assert replaced != block, "tiingo_prices no longer records allowed_with_conditions"
    (config / "sources.yaml").write_text(
        sources[:start] + replaced + sources[end if end != -1 else len(sources) :],
        encoding="utf-8",
    )
    return config


def test_help_lists_the_phase_three_commands() -> None:
    result = _run("--help")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    for command in NEW_COMMANDS:
        assert command in result.output, command  # type: ignore[attr-defined]


def test_prices_help_lists_fetch() -> None:
    result = _run("prices", "--help")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "fetch" in result.output  # type: ignore[attr-defined]


def test_prices_fetch_refuses_a_source_pending_review(tmp_path: Path) -> None:
    """D3: any decision other than allowed* means the file is never fetched."""
    config = _config_with_price_decision(tmp_path, "pending_review")
    result = _run(
        "prices",
        "fetch",
        "--symbols",
        "AAPL",
        "--from",
        "2026-10-01",
        "--to",
        "2026-10-10",
        "--raw",
        str(tmp_path / "raw"),
        "--config",
        str(config),
    )
    assert result.exit_code == 2, result.output  # type: ignore[attr-defined]
    assert "skipped: terms" in result.output  # type: ignore[attr-defined]
    assert "decision=pending_review" in result.output  # type: ignore[attr-defined]
    assert not (tmp_path / "raw").exists()


def test_prices_fetch_prefers_the_terms_gate_over_the_missing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused source is refused even when a key exists: no request is prepared."""
    monkeypatch.setenv("TIINGO_API_KEY", "not-a-real-key")
    config = _config_with_price_decision(tmp_path, "not_allowed")
    result = _run(
        "prices",
        "fetch",
        "--symbols",
        "AAPL",
        "--from",
        "2026-10-01",
        "--to",
        "2026-10-10",
        "--raw",
        str(tmp_path / "raw"),
        "--config",
        str(config),
    )
    assert result.exit_code == 2, result.output  # type: ignore[attr-defined]
    assert "skipped: terms" in result.output  # type: ignore[attr-defined]
    assert "TIINGO_API_KEY" not in result.output  # type: ignore[attr-defined]
    assert not (tmp_path / "raw").exists()


def test_prices_fetch_without_a_key_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    result = _run(
        "prices",
        "fetch",
        "--symbols",
        "AAPL",
        "--from",
        "2026-10-01",
        "--to",
        "2026-10-10",
        "--raw",
        str(tmp_path / "raw"),
        "--config",
        str(CONFIG_DIR),
    )
    assert result.exit_code == 2, result.output  # type: ignore[attr-defined]
    assert "TIINGO_API_KEY is not set" in result.output  # type: ignore[attr-defined]
    assert not (tmp_path / "raw").exists()


def test_prices_fetch_rejects_a_reversed_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TIINGO_API_KEY", "not-a-real-key")
    result = _run(
        "prices",
        "fetch",
        "--symbols",
        "AAPL",
        "--from",
        "2026-10-10",
        "--to",
        "2026-10-01",
        "--raw",
        str(tmp_path / "raw"),
        "--config",
        str(CONFIG_DIR),
    )
    assert result.exit_code == 2, result.output  # type: ignore[attr-defined]
    assert "--from must not be after --to" in result.output  # type: ignore[attr-defined]


def test_prices_fetch_requires_at_least_one_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TIINGO_API_KEY", "not-a-real-key")
    result = _run(
        "prices",
        "fetch",
        "--symbols",
        " , ",
        "--from",
        "2026-10-01",
        "--to",
        "2026-10-10",
        "--raw",
        str(tmp_path / "raw"),
        "--config",
        str(CONFIG_DIR),
    )
    assert result.exit_code == 2, result.output  # type: ignore[attr-defined]
    assert "--symbols must name at least one symbol" in result.output  # type: ignore[attr-defined]


def test_prices_fetch_rejects_a_bad_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIINGO_API_KEY", "not-a-real-key")
    result = _run(
        "prices",
        "fetch",
        "--symbols",
        "AAPL",
        "--from",
        "yesterday",
        "--to",
        "2026-10-10",
        "--raw",
        str(tmp_path / "raw"),
        "--config",
        str(CONFIG_DIR),
    )
    assert result.exit_code == 2, result.output  # type: ignore[attr-defined]
    assert "is not a date in YYYY-MM-DD form or 'today'" in result.output  # type: ignore[attr-defined]


class _FakePrices:
    """Stand-in for ``TiingoPrices`` so the printing path needs no network."""

    def __init__(self, frame: pl.DataFrame, calls: list[tuple[list[str], date, date]]) -> None:
        self.frame = frame
        self.calls = calls

    @classmethod
    def from_env(cls, raw: Path, *, sources: object, budget_path: Path) -> _FakePrices:
        assert Path(budget_path).name == "price_budget.json"
        assert str(Path(raw)) == "raw-store"
        return cls(_frame, _call_log)

    def __enter__(self) -> _FakePrices:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def daily_closes(self, symbols: list[str], start: date, end: date) -> pl.DataFrame:
        self.calls.append((list(symbols), start, end))
        return self.frame


_call_log: list[tuple[list[str], date, date]] = []
_frame = pl.DataFrame(
    {
        "symbol": ["AAPL", "AAPL", "MSFT"],
        "date": [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 1)],
        "close": [100.0, 101.5, 250.0],
    },
    schema=CLOSE_SCHEMA,
)


def test_prices_fetch_prints_one_line_per_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TIINGO_API_KEY", "not-a-real-key")
    monkeypatch.setattr("etfh.cli.TiingoPrices", _FakePrices)
    _call_log.clear()
    result = _run(
        "prices",
        "fetch",
        "--symbols",
        "aapl, msft ,aapl",
        "--from",
        "2026-10-01",
        "--to",
        "2026-10-10",
        "--raw",
        "raw-store",
        "--config",
        str(CONFIG_DIR),
    )
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert _call_log == [(["AAPL", "MSFT"], date(2026, 10, 1), date(2026, 10, 10))]
    assert "AAPL: 2 row(s) 2026-10-01..2026-10-02" in result.output  # type: ignore[attr-defined]
    assert "MSFT: 1 row(s) 2026-10-01..2026-10-01" in result.output  # type: ignore[attr-defined]
    assert "prices: 3 row(s) for 2 of 2 symbol(s)" in result.output  # type: ignore[attr-defined]


def test_event_study_rejects_a_bad_window() -> None:
    for window in ("-5;5", "5,-5", "a,b"):
        result = _run(
            "event-study",
            "--db",
            "missing.duckdb",
            "--window",
            window,
        )
        assert result.exit_code == 2, (window, result.output)  # type: ignore[attr-defined]
        assert "window" in result.output  # type: ignore[attr-defined]


def test_event_study_rejects_a_min_weight_outside_zero_one() -> None:
    for value in ("0", "1.5", "-0.01"):
        result = _run("event-study", "--db", "missing.duckdb", "--min-weight", value)
        assert result.exit_code == 2, (value, result.output)  # type: ignore[attr-defined]
        assert "--min-weight must be a fraction in (0, 1]" in result.output  # type: ignore[attr-defined]


def test_event_study_needs_a_database(tmp_path: Path) -> None:
    result = _run(
        "event-study",
        "--db",
        str(tmp_path / "nope.duckdb"),
        "--source",
        "synthetic",
    )
    assert result.exit_code == 2, result.output  # type: ignore[attr-defined]
    assert "error: database not found" in result.output  # type: ignore[attr-defined]


def test_event_study_from_tiingo_without_a_key_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    from etfh.build import build

    db = build(RAW_ROOT, tmp_path / "build", config_dir=CONFIG_DIR).db_path
    result = _run(
        "event-study",
        "--db",
        str(db),
        "--raw",
        str(tmp_path / "raw"),
        "--out",
        str(tmp_path / "out"),
        "--config",
        str(CONFIG_DIR),
    )
    assert result.exit_code == 2, result.output  # type: ignore[attr-defined]
    assert "TIINGO_API_KEY is not set" in result.output  # type: ignore[attr-defined]
    assert not (tmp_path / "out").exists()


def test_verify_raw_reports_every_problem_and_exits_1(tmp_path: Path) -> None:
    store = tmp_path / "raw"
    shutil.copytree(RAW_ROOT, store)
    victim = sorted(store.rglob("*.xlsx"))[0]
    victim.write_bytes(b"not the file the sidecar describes")
    orphan = victim.parent / "20260101T000000Z.meta.json"
    orphan.write_text("{}", encoding="utf-8")

    result = _run("verify-raw", "--raw", str(store))
    assert result.exit_code == 1, result.output  # type: ignore[attr-defined]
    assert "sha_mismatch" in result.output  # type: ignore[attr-defined]
    assert "orphan_sidecar" in result.output  # type: ignore[attr-defined]
    assert "problem(s)" in result.output  # type: ignore[attr-defined]


def test_verify_raw_on_a_missing_directory_exits_2(tmp_path: Path) -> None:
    result = _run("verify-raw", "--raw", str(tmp_path / "nope"))
    assert result.exit_code == 2, result.output  # type: ignore[attr-defined]
    assert "error:" in result.output  # type: ignore[attr-defined]


def test_sync_raw_never_overwrites_a_conflicting_file(tmp_path: Path) -> None:
    destination = tmp_path / "copy"
    assert _run("sync-raw", "--from", str(RAW_ROOT), "--to", str(destination)).exit_code == 0  # type: ignore[attr-defined]
    victim = sorted(path for path in destination.rglob("*.csv"))[0]
    original = victim.read_bytes()
    victim.write_bytes(original + b"\n")

    result = _run("sync-raw", "--from", str(RAW_ROOT), "--to", str(destination))
    assert result.exit_code == 1, result.output  # type: ignore[attr-defined]
    assert "conflict:" in result.output  # type: ignore[attr-defined]
    assert "not overwritten" in result.output  # type: ignore[attr-defined]
    assert victim.read_bytes() == original + b"\n"


def test_sync_raw_dry_run_writes_nothing(tmp_path: Path) -> None:
    destination = tmp_path / "dry"
    result = _run("sync-raw", "--from", str(RAW_ROOT), "--to", str(destination), "--dry-run")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "15 copied" in result.output  # type: ignore[attr-defined]
    assert "dry run: nothing written" in result.output  # type: ignore[attr-defined]
    assert not destination.exists()


def test_sync_raw_refuses_an_unclean_source(tmp_path: Path) -> None:
    source = tmp_path / "unclean"
    shutil.copytree(RAW_ROOT, source)
    sorted(source.rglob("*.csv"))[0].write_bytes(b"corrupted")
    result = _run("sync-raw", "--from", str(source), "--to", str(tmp_path / "dst"))
    assert result.exit_code == 1, result.output  # type: ignore[attr-defined]
    assert "problem(s)" in result.output  # type: ignore[attr-defined]
    assert not (tmp_path / "dst").exists()
