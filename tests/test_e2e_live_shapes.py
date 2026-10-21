"""Integration tests for the live document shapes on fictional look-alikes.

The wave-03 live run exposed Purpose fund documents that publish their holdings
under ``portfolio.holdings_data`` as a dict of named sections (the fund-of-funds
"Holdings Breakdown") instead of ``Level4.holdings``. These tests pin the
look-alike structure offline: the section list is parsed, the source's own
complete exposure breakdown marks the file ``partial_list`` (D52), and a
document whose only row is a physical quantity (ounces, no portfolio weight)
parses to one commodity holding at 100% (wave 04 handles that shape instead of
excluding it, so KILO is usable).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import polars as pl
import pytest

from etfh import qa
from etfh.build import BuildResult, build
from etfh.parse.purpose import PurposeParser
from etfh.storage import RawStore
from etfh.types import RawFile
from tests._build_helpers import CONFIG_DIR

_FETCHED_AT = datetime(2026, 10, 16, 22, 0, tzinfo=UTC)
_AS_OF = date(2026, 10, 16)


def _sectioned_document() -> dict[str, object]:
    """A fictional fund-of-funds document with a sectioned holdings dict."""
    return {
        "fund": {"ticker": "PDF", "name": "Fictional Purpose Fund"},
        "assetClass": [{"fields": {"className": "Balanced"}}],
        "portfolio": {
            "dt": _AS_OF.isoformat(),
            "Level1": {
                "asset_class": {
                    "Fixed Income": 40.0,
                    "Equities": 45.0,
                    "Cash": 15.0,
                }
            },
            "Level4": {},
            "holdings_data": {
                "Holding Breakdown": [
                    {"name": "Northwind Aggregate Bond Index", "weight": "24.50%"},
                    {"name": "Cedarline Equity Fund", "weight": "22.25%"},
                    {"name": "Harbourside Dividend Fund", "weight": "15.75%"},
                ]
            },
        },
    }


def _quantity_document() -> dict[str, object]:
    """A fictional commodity document whose only row is a physical quantity."""
    return {
        "fund": {"ticker": "PDF", "name": "Fictional Purpose Commodity Fund"},
        "portfolio": {
            "dt": _AS_OF.isoformat(),
            "Level4": {"holdings": [{"name": "Gold Bars", "weight": "191,266 oz"}]},
        },
    }


def _raw_file(path: Path, *, sha: str) -> RawFile:
    return RawFile(
        source_id="purpose_daily",
        issuer="purpose",
        fund_ticker="PDF",
        as_of_hint=_AS_OF,
        fetched_at=_FETCHED_AT,
        url=f"https://example.test/{path.name}",
        path=path,
        sha256=sha,
        content_type="application/json",
        ext="json",
    )


def test_sectioned_holdings_dict_is_parsed_and_marked_partial(tmp_path: Path) -> None:
    path = tmp_path / "sectioned.json"
    path.write_text(json.dumps(_sectioned_document()), encoding="utf-8")
    parsed = PurposeParser().parse(_raw_file(path, sha="a" * 64))
    assert parsed.frame.height == 3
    assert parsed.frame["security_name"].to_list() == [
        "Northwind Aggregate Bond Index",
        "Cedarline Equity Fund",
        "Harbourside Dividend Fund",
    ]
    assert parsed.frame["weight"].to_list() == pytest.approx([0.245, 0.2225, 0.1575])
    assert "partial_list=true" in parsed.notes
    assert "declared_unit=percent" in parsed.notes


def test_quantity_only_document_parses_as_one_commodity_holding(tmp_path: Path) -> None:
    path = tmp_path / "quantity.json"
    path.write_text(json.dumps(_quantity_document()), encoding="utf-8")
    parsed = PurposeParser().parse(_raw_file(path, sha="b" * 64))
    assert parsed.frame.height == 1
    row = parsed.frame.to_dicts()[0]
    assert row["security_name"] == "Gold Bars"
    assert row["asset_class"] == "commodity"
    assert row["weight"] == 1.0
    assert row["security_ticker"] is None
    assert "commodity_quantity=191,266 oz" in parsed.notes


def _write_document(store: RawStore, document: dict[str, object], *, fetched_at: datetime) -> None:
    store.write(
        json.dumps(document).encode("utf-8"),
        source_id="purpose_daily",
        issuer="purpose",
        fund_ticker="PDF",
        url="https://example.test/holdings.json",
        fetched_at=fetched_at,
        ext="json",
        content_type="application/json",
        as_of_hint=_AS_OF,
    )


def _build_mixed_store(tmp_path: Path) -> tuple[Path, BuildResult]:
    raw_root = tmp_path / "raw"
    store = RawStore(raw_root)
    _write_document(store, _sectioned_document(), fetched_at=_FETCHED_AT)
    _write_document(store, _quantity_document(), fetched_at=_FETCHED_AT.replace(hour=23))
    result = build(raw_root, tmp_path / "out", config_dir=CONFIG_DIR)
    return tmp_path / "out", result


def test_build_includes_a_partial_list_and_a_commodity_file(
    tmp_path: Path,
) -> None:
    _out_dir, result = _build_mixed_store(tmp_path)
    assert result.n_file_versions == 2
    assert result.n_included_files == 2
    assert result.n_excluded_files == 0
    assert result.n_holdings == 4
    connection = duckdb.connect(str(result.db_path), read_only=True)
    try:
        versions = connection.execute(
            "SELECT status, COUNT(*) FROM file_versions GROUP BY 1 ORDER BY 1"
        ).fetchall()
        events = connection.execute(
            'SELECT "check", severity, excluded FROM qa_events ORDER BY "check"'
        ).fetchall()
        summary = qa.summarize(connection)
    finally:
        connection.close()
    assert versions == [("included", 2)]
    assert ("partial_list", "info", False) in events
    assert all(severity != "error" for _check, severity, _excluded in events)
    assert qa.exit_code(summary) == 0


def test_holdings_frame_keeps_the_normalized_shape(tmp_path: Path) -> None:
    _out_dir, result = _build_mixed_store(tmp_path)
    frame = pl.read_parquet(result.out_dir / "parquet" / "holdings.parquet")
    assert frame.columns == [
        "fund_ticker",
        "as_of_date",
        "known_at",
        "file_version",
        "security_id",
        "security_name",
        "security_ticker",
        "exchange",
        "isin",
        "cusip",
        "asset_class",
        "sector",
        "country",
        "currency",
        "weight",
        "shares",
        "market_value",
        "source_id",
        "raw_file_sha256",
    ]
    assert frame["fund_ticker"].unique().to_list() == ["PDF"]
    assert frame["weight"].sum() == pytest.approx(1.625)
