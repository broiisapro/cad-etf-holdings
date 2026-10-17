"""Deterministic fakes and paths shared by the build tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from etfh.build import BuildResult, build
from etfh.storage import RawStore
from etfh.types import ParsedFile
from etfh.validate import QaFinding

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
RAW_ROOT = FIXTURES / "raw_synthetic"
CONFIG_DIR = REPO_ROOT / "config"


class FakeSecurityMaster:
    """Contract-compatible security master: deterministic identifier mapping."""

    def assign(self, frame: pl.DataFrame) -> pl.DataFrame:
        identifier = (
            pl.when(pl.col("asset_class") == "cash")
            .then(pl.concat_str([pl.lit("CASH:"), pl.col("currency")]))
            .when(pl.col("isin").is_not_null())
            .then(pl.concat_str([pl.lit("ISIN:"), pl.col("isin")]))
            .when(pl.col("cusip").is_not_null())
            .then(pl.concat_str([pl.lit("CUSIP:"), pl.col("cusip")]))
            .when(pl.col("security_ticker").is_not_null() & pl.col("exchange").is_not_null())
            .then(
                pl.concat_str(
                    [pl.lit("TKR:"), pl.col("exchange"), pl.lit(":"), pl.col("security_ticker")]
                )
            )
            .otherwise(pl.lit(None, dtype=pl.String))
        )
        return frame.with_columns(identifier.alias("security_id"))


def fake_validator(
    parsed: ParsedFile, *, source: Any, fund_asset_class: Any, config: Any
) -> list[QaFinding]:
    """Marks the fixture set's two bad files as errors until E's checks land."""
    ticker = parsed.frame["fund_ticker"][0] if parsed.frame.height else ""
    if ticker == "SYNC":
        return [QaFinding("weight_sum", "error", "weights sum to 0.8500 (expected 1.0000)")]
    if ticker == "SYND":
        return [QaFinding("duplicate_security", "error", "Northwind Materials Inc. twice")]
    return []


def build_fixture_db(
    out_dir: Path,
    *,
    raw_root: Path = RAW_ROOT,
    security_master: Any = None,
    validator: Any = fake_validator,
) -> BuildResult:
    return build(
        raw_root,
        out_dir,
        config_dir=CONFIG_DIR,
        security_master=FakeSecurityMaster() if security_master is None else security_master,
        validator=validator,
    )


def copy_raw_files(
    source_root: Path,
    destination_root: Path,
    wanted: set[tuple[str, date]],
) -> None:
    """Copy the fixture files for the given (fund ticker, as-of hint) pairs."""
    source_store = RawStore(source_root)
    destination_store = RawStore(destination_root)
    for raw in source_store.iter_raw():
        if (raw.fund_ticker, raw.as_of_hint) not in wanted:
            continue
        destination_store.write(
            raw.path.read_bytes(),
            source_id=raw.source_id,
            issuer=raw.issuer,
            fund_ticker=raw.fund_ticker,
            url=raw.url,
            fetched_at=raw.fetched_at,
            ext=raw.ext,
            content_type=raw.content_type,
            as_of_hint=raw.as_of_hint,
        )
