"""Tests for ``etfh spotcheck`` (issue #77)."""

from __future__ import annotations

import shutil
from pathlib import Path

import polars as pl
import pytest
import yaml

from etfh.build import BuildResult, build
from etfh.spotcheck import spotcheck
from etfh.units import parse_number
from tests._build_helpers import (
    CONFIG_DIR,
    RAW_ROOT,
    FakeSecurityMaster,
    fake_validator,
)

_ISSUERS = {
    "syn_csv": "issuer_csv",
    "syn_xlsx": "issuer_xlsx",
    "syn_json": "issuer_json",
    "syn_pdf": "issuer_pdf",
}


def _four_issuer_config(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy(CONFIG_DIR / "funds.csv", directory / "funds.csv")
    shutil.copy(CONFIG_DIR / "validation.yaml", directory / "validation.yaml")
    data = yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8"))
    for entry in data["sources"]:
        if entry["id"] in _ISSUERS:
            entry["issuer"] = _ISSUERS[entry["id"]]
    (directory / "sources.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return directory


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, BuildResult]:
    root = tmp_path_factory.mktemp("spotcheck")
    config = _four_issuer_config(root / "config")
    result = build(
        RAW_ROOT,
        root / "out",
        config_dir=config,
        security_master=FakeSecurityMaster(),
        validator=fake_validator,
    )
    return config, result


def _header_lines(report: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in report.splitlines():
        if line.startswith("source="):
            fields = dict(part.split("=", 1) for part in line.split(" ") if "=" in part)
            headers[fields["issuer"]] = line
    return headers


def test_every_format_reports_raw_values_next_to_normalized_weights(
    built: tuple[Path, BuildResult],
) -> None:
    config, result = built
    report = spotcheck(RAW_ROOT, result.db_path, n=2, seed=3, config_dir=config)
    headers = _header_lines(report)
    assert set(headers) == {"issuer_csv", "issuer_json", "issuer_pdf", "issuer_xlsx"}
    assert "source=syn_csv" in headers["issuer_csv"]
    assert "source=syn_xlsx" in headers["issuer_xlsx"]
    assert "source=syn_json" in headers["issuer_json"]
    assert "source=syn_pdf" in headers["issuer_pdf"]
    assert "stated_total=0.8235 parsed_total=0.8235" in headers["issuer_pdf"]
    assert "weight_sum=" in headers["issuer_csv"]
    rows = [line for line in report.splitlines() if line.startswith("  ")]
    assert len(rows) == 8
    for row in rows:
        assert " | raw=" in row
        assert " | weight=" in row
        assert "(not located)" not in row


def test_raw_values_match_the_normalized_weights(
    built: tuple[Path, BuildResult],
) -> None:
    config, result = built
    report = spotcheck(RAW_ROOT, result.db_path, n=4, seed=1, config_dir=config)
    scales = {
        "issuer_csv": 100.0,  # declared percent
        "issuer_xlsx": 1.0,  # declared fraction
        "issuer_json": 1.0,  # declared fraction
    }
    issuer = ""
    for line in report.splitlines():
        if line.startswith("source="):
            issuer = line.split("issuer=", 1)[1].split(" ", 1)[0]
            continue
        if not line.startswith("  ") or issuer == "issuer_pdf":
            continue
        raw = line.split(" | raw=", 1)[1].split(" | weight=", 1)[0]
        weight = float(line.rsplit("weight=", 1)[1])
        value = parse_number(raw)
        assert value is not None
        assert value == pytest.approx(weight * scales[issuer], abs=1e-9)


def test_selection_is_deterministic_per_seed(built: tuple[Path, BuildResult]) -> None:
    config, result = built
    first = spotcheck(RAW_ROOT, result.db_path, n=3, seed=7, config_dir=config)
    second = spotcheck(RAW_ROOT, result.db_path, n=3, seed=7, config_dir=config)
    assert first == second
    assert first.startswith("spotcheck n=3 seed=7\n")


def test_issuer_filter_selects_one_issuer(built: tuple[Path, BuildResult]) -> None:
    config, result = built
    report = spotcheck(
        RAW_ROOT, result.db_path, n=2, seed=0, issuer="issuer_json", config_dir=config
    )
    headers = _header_lines(report)
    assert set(headers) == {"issuer_json"}
    assert "fund=SYNE" in headers["issuer_json"]


def test_unknown_issuer_reports_nothing_found(built: tuple[Path, BuildResult]) -> None:
    config, result = built
    report = spotcheck(RAW_ROOT, result.db_path, issuer="nope", config_dir=config)
    assert "no included files found for issuer 'nope'" in report


def test_spotcheck_writes_nothing(built: tuple[Path, BuildResult]) -> None:
    config, result = built
    before = sorted(str(path.relative_to(RAW_ROOT)) for path in RAW_ROOT.rglob("*"))
    cwd = Path.cwd()
    before_cwd = sorted(str(path.relative_to(cwd)) for path in cwd.glob("*.csv"))
    spotcheck(RAW_ROOT, result.db_path, n=5, seed=0, config_dir=config)
    after = sorted(str(path.relative_to(RAW_ROOT)) for path in RAW_ROOT.rglob("*"))
    after_cwd = sorted(str(path.relative_to(cwd)) for path in cwd.glob("*.csv"))
    assert before == after
    assert before_cwd == after_cwd


def test_zero_or_negative_n_is_rejected(built: tuple[Path, BuildResult]) -> None:
    config, result = built
    with pytest.raises(ValueError, match="n must be at least 1"):
        spotcheck(RAW_ROOT, result.db_path, n=0, config_dir=config)


def test_raw_csv_rows_are_read_in_file_order(built: tuple[Path, BuildResult]) -> None:
    """The raw cell shown is the file's own cell, not a re-rendered number."""
    config, result = built
    report = spotcheck(
        RAW_ROOT, result.db_path, n=10, seed=2, issuer="issuer_csv", config_dir=config
    )
    raw_values = [
        line.split(" | raw=", 1)[1].split(" | weight=", 1)[0]
        for line in report.splitlines()
        if line.startswith("  ")
    ]
    source = pl.read_csv(
        RAW_ROOT / "synthetic" / "SYNA" / "2026-11-03" / "20261103T220000Z.csv",
        has_header=True,
        skip_rows=4,
        schema_overrides={"Weight (%)": pl.String},
    )
    cells = {str(value) for value in source["Weight (%)"].to_list()}
    assert set(raw_values) <= cells
