"""Deterministic fakes and paths shared by the build tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from etfh.build import BuildResult, build
from etfh.secmaster.identifiers import cusip_to_isin
from etfh.storage import RawStore
from etfh.types import FileFormat, ParsedFile, RawFile
from etfh.validate import QaFinding

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
RAW_ROOT = FIXTURES / "raw_synthetic"
CONFIG_DIR = REPO_ROOT / "config"

METRICS_SOURCE_ID = "syn_metrics"
METRICS_FUND = "SYNM"


class FakeSecurityMaster:
    """Contract-compatible security master: deterministic identifier mapping.

    Mirrors the D23 order (provided ISIN, derived ISIN for CA/US CUSIPs, plain
    CUSIP, exchange-qualified ticker, cash) using the real identifier helpers so
    tests reproduce the committed expected overlap.
    """

    def assign(self, frame: pl.DataFrame) -> pl.DataFrame:
        derived = pl.struct("cusip", "country").map_elements(
            _derive_isin, return_dtype=pl.String, skip_nulls=False
        )
        identifier = (
            pl.when(pl.col("asset_class") == "cash")
            .then(pl.concat_str([pl.lit("CASH:"), pl.col("currency")]))
            .when(pl.col("isin").is_not_null())
            .then(pl.concat_str([pl.lit("ISIN:"), pl.col("isin")]))
            .when(derived.is_not_null())
            .then(pl.concat_str([pl.lit("ISIN:"), derived]))
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


def _derive_isin(row: dict[str, Any]) -> str | None:
    cusip = row["cusip"]
    country = row["country"]
    if not cusip or country not in {"CA", "US"}:
        return None
    return cusip_to_isin(cusip, country)


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


def make_metrics_config(tmp_path: Path) -> Path:
    """A copy of the repo config plus one synthetic fund-metrics source and fund.

    The extra source keeps the committed config untouched (sources.yaml belongs
    to another workstream) while giving the metrics tests a source the parsers
    can claim. Its kind is ``synthetic`` because the build decides what is
    metrics-only from the parsers, not from the source kind.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ("validation.yaml",):
        (config_dir / name).write_bytes((CONFIG_DIR / name).read_bytes())
    sources = yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8"))
    sources["sources"].append(
        {
            "id": METRICS_SOURCE_ID,
            "issuer": "synthetic",
            "kind": "synthetic",
            "format": "json",
            "url_patterns": ["synthetic://fixtures/<TICKER>-metrics.json"],
            "update_frequency": "daily",
            "supported": True,
            "notes": "test-only fund-metrics source",
            "terms": {
                "url": "synthetic://fixtures/syn_metrics",
                "reviewer": "synthetic fixture",
                "reviewed_on": date(2026, 10, 23),
                "decision": "allowed",
                "redistribution_allowed": True,
            },
        }
    )
    (config_dir / "sources.yaml").write_text(
        yaml.safe_dump(sources, sort_keys=False), encoding="utf-8"
    )
    funds = (CONFIG_DIR / "funds.csv").read_text(encoding="utf-8")
    if not funds.endswith("\n"):
        funds += "\n"
    funds += (
        f"{METRICS_FUND},synthetic,Synthetic Meridian Metrics Fund,equity,"
        f"{METRICS_SOURCE_ID},XTSE,\n"
    )
    (config_dir / "funds.csv").write_text(funds, encoding="utf-8")
    return config_dir


def metrics_payload(
    rows: Sequence[tuple[date, float | None, float | None, float | None]],
    *,
    currency: str | None = "CAD",
) -> bytes:
    """JSON metrics history in the shape the test parser reads."""
    payload = [
        {
            "date": as_of.isoformat(),
            "nav": nav,
            "shares": shares,
            "aum": aum,
            "currency": currency,
        }
        for as_of, nav, shares, aum in rows
    ]
    return json.dumps(payload).encode("utf-8")


def write_metrics_raw(
    raw_root: Path,
    content: bytes,
    *,
    fetched_at: datetime,
    fund_ticker: str = METRICS_FUND,
    source_id: str = METRICS_SOURCE_ID,
    as_of_hint: date | None = None,
) -> RawFile:
    return RawStore(raw_root).write(
        content,
        source_id=source_id,
        issuer="synthetic",
        fund_ticker=fund_ticker,
        url=f"synthetic://fixtures/{fund_ticker}-metrics.json",
        fetched_at=fetched_at,
        ext="json",
        content_type="application/json",
        as_of_hint=as_of_hint,
    )


class FakeMetricsParser:
    """Reads the test JSON metrics history into a ``ParsedFundMetrics`` frame.

    With ``fixed_rows`` the parser ignores the file bytes and returns those
    rows, which lets the tests attach metrics to a holdings file without
    inventing a second file format.
    """

    formats = frozenset({FileFormat.json})

    def __init__(
        self,
        *,
        source_id: str = METRICS_SOURCE_ID,
        claims: Callable[[RawFile], bool] | None = None,
        published_at: datetime | None = None,
        old_sha: str | None = None,
        raises: str | None = None,
        fixed_rows: Sequence[tuple[date, float | None, float | None, float | None]] | None = None,
        currency: str | None = "CAD",
    ) -> None:
        self.source_id = source_id
        self.published_at = published_at
        self.old_sha = old_sha
        self.raises = raises
        self.fixed_rows = fixed_rows
        self.currency = currency
        self.claims = claims if claims is not None else (lambda raw: raw.source_id == source_id)

    def can_parse(self, raw: RawFile) -> bool:
        return self.claims(raw)

    def parse(self, raw: RawFile) -> ParsedFile:
        if self.raises is not None:
            raise ValueError(self.raises)
        if self.fixed_rows is not None:
            history = [
                {
                    "date": as_of.isoformat(),
                    "nav": nav,
                    "shares": shares,
                    "aum": aum,
                    "currency": self.currency,
                }
                for as_of, nav, shares, aum in self.fixed_rows
            ]
        else:
            history = json.loads(raw.path.read_text(encoding="utf-8"))
        rows = [
            {
                "fund_ticker": raw.fund_ticker,
                "as_of_date": date.fromisoformat(row["date"]),
                "nav_per_unit": row["nav"],
                "shares_outstanding": row["shares"],
                "aum": row["aum"],
                "currency": row["currency"],
                "source_id": self.source_id,
                "raw_file_sha256": self.old_sha or raw.sha256,
            }
            for row in history
        ]
        frame = pl.DataFrame(
            rows,
            schema={
                "fund_ticker": pl.String,
                "as_of_date": pl.Date,
                "nav_per_unit": pl.Float64,
                "shares_outstanding": pl.Float64,
                "aum": pl.Float64,
                "currency": pl.String,
                "source_id": pl.String,
                "raw_file_sha256": pl.String,
            },
        )
        return ParsedFile(
            raw=raw,
            as_of_date=max(row["as_of_date"] for row in rows),
            published_at=self.published_at,
            frame=frame,
        )


def no_metrics_findings(parsed: ParsedFile, *, source: Any, config: Any) -> list[QaFinding]:
    """A metrics validator that accepts everything (E's real checks land separately)."""
    return []


def metrics_error(parsed: ParsedFile, *, source: Any, config: Any) -> list[QaFinding]:
    return [QaFinding("nav_positive", "error", "nav_per_unit is not positive")]


def build_metrics_db(
    out_dir: Path,
    *,
    raw_root: Path,
    config_dir: Path,
    metrics_parsers: Sequence[Any],
    metrics_validator: Any = no_metrics_findings,
    validator: Any = fake_validator,
    security_master: Any = None,
    monkeypatch: Any = None,
) -> BuildResult:
    """Build with a fixed set of metrics parsers instead of the discovered ones."""
    import etfh.build as build_module

    assert monkeypatch is not None
    monkeypatch.setattr(
        build_module, "find_metrics_parsers", lambda raw, source: list(metrics_parsers)
    )
    return build(
        raw_root,
        out_dir,
        config_dir=config_dir,
        security_master=FakeSecurityMaster() if security_master is None else security_master,
        validator=validator,
        metrics_validator=metrics_validator,
    )
