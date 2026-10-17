"""Tests for the deterministic synthetic fixtures and the manifest invariants."""

from __future__ import annotations

import csv
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pdfplumber
import pytest
from openpyxl import load_workbook

from etfh.synthetic import ExpectedOverlapFixture, generate, ground_truth_holdings

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RAW_ROOT = FIXTURES / "raw_synthetic"

_XLSX_WEIGHT_COLUMN = 8
_CSV_WEIGHT_COLUMN = 8
_MAX_FIXTURE_BYTES = 2 * 1024 * 1024


def _read_csv_rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    header_index = next(index for index, row in enumerate(rows) if row and row[0] == "Ticker")
    return rows[header_index + 1 :]


def _read_xlsx_rows(path: Path) -> list[tuple[object, ...]]:
    workbook = load_workbook(path)
    values = list(workbook.active.iter_rows(values_only=True))
    header_index = next(
        index for index, row in enumerate(values) if row and row[0] == "Security Name"
    )
    return [row for row in values[header_index + 1 :] if row[0] is not None]


def _read_json_payload(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest() -> dict[str, object]:
    return json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))


def _overlap_fixture() -> ExpectedOverlapFixture:
    return ExpectedOverlapFixture.model_validate_json(
        (FIXTURES / "expected_overlap.json").read_text(encoding="utf-8")
    )


def _tree_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_two_runs_are_byte_identical(tmp_path: Path) -> None:
    generate(tmp_path / "run-a" / "raw_synthetic")
    generate(tmp_path / "run-b" / "raw_synthetic")
    assert _tree_files(tmp_path / "run-a") == _tree_files(tmp_path / "run-b")


def test_generated_tree_matches_committed_fixtures(tmp_path: Path) -> None:
    generate(tmp_path / "raw_synthetic")
    assert _tree_files(tmp_path / "raw_synthetic") == _tree_files(RAW_ROOT)
    assert (tmp_path / "manifest.json").read_bytes() == (FIXTURES / "manifest.json").read_bytes()
    assert (tmp_path / "expected_overlap.json").read_bytes() == (
        FIXTURES / "expected_overlap.json"
    ).read_bytes()


def test_fixture_size_under_two_megabytes() -> None:
    total = sum(path.stat().st_size for path in FIXTURES.rglob("*") if path.is_file())
    assert total < _MAX_FIXTURE_BYTES


def test_no_files_for_blocked_fund() -> None:
    assert not (RAW_ROOT / "synthetic" / "SYNX").exists()


def test_exactly_two_bad_files_recorded() -> None:
    manifest = _manifest()
    bad_files = manifest["bad_files"]
    assert isinstance(bad_files, list)
    assert len(bad_files) == 2
    by_fund = {entry["fund_ticker"]: entry for entry in bad_files}
    assert by_fund["SYNC"]["failing_check"] == "weight_sum"
    assert by_fund["SYND"]["failing_check"] == "duplicate_security"
    for entry in bad_files:
        assert (RAW_ROOT / str(entry["path"])).is_file()


def test_daily_file_weight_sums() -> None:
    manifest = _manifest()
    sums: dict[str, Decimal] = {}
    for ticker, fund in manifest["funds"].items():
        fmt = fund["format"]
        if fmt == "pdf":
            continue
        unit = fund["weight_unit"]
        expected = Decimal("0.85") if ticker == "SYNC" else Decimal("1")
        paths = [
            candidate
            for candidate in sorted((RAW_ROOT / "synthetic" / ticker).rglob(f"*.{fmt}"))
            if not candidate.name.endswith(".meta.json")
        ]
        for path in paths:
            if fmt == "csv":
                rows = _read_csv_rows(path)
                total = sum(Decimal(row[_CSV_WEIGHT_COLUMN]) for row in rows)
                assert any(row[0] == "CASH" for row in rows), path
            elif fmt == "xlsx":
                rows = _read_xlsx_rows(path)
                total = sum(Decimal(str(row[_XLSX_WEIGHT_COLUMN])) for row in rows)
                assert any(row[1] == "CASH" for row in rows), path
            else:
                payload = _read_json_payload(path)
                portfolio = payload["portfolio"]
                assert isinstance(portfolio, dict)
                holdings = portfolio["holdings"]
                assert isinstance(holdings, list)
                total = sum(Decimal(str(holding["weight"])) for holding in holdings)
                assert any(holding["ticker"] == "CASH" for holding in holdings), path
            if unit == "percent":
                total *= Decimal("0.01")
            assert total == expected, (path, total)
            sums[path.as_posix()] = total
    assert len(sums) == 14


def test_duplicate_security_rows_in_synd() -> None:
    rows = _read_xlsx_rows(RAW_ROOT / "synthetic" / "SYND" / "2026-10-29" / "20261029T220000Z.xlsx")
    nwm_rows = [row for row in rows if row[1] == "NWM"]
    assert len(nwm_rows) == 2
    identifiers = {row[2] or row[3] for row in nwm_rows}
    assert len(identifiers) == 2
    isin_value = next((row[2] for row in nwm_rows if row[2]), None)
    cusip_value = next((row[3] for row in nwm_rows if row[3]), None)
    assert isin_value is not None and cusip_value is not None
    assert isin_value[2:11] == cusip_value


def test_mapl_pair_uses_different_exchanges() -> None:
    manifest = _manifest()
    mapl_pair = manifest["mapl_pair"]
    assert [entry["exchange"] for entry in mapl_pair] == ["XTSE", "XNYS"]
    assert mapl_pair[0]["security_name"] != mapl_pair[1]["security_name"]
    syna_rows = _read_csv_rows(
        RAW_ROOT / "synthetic" / "SYNA" / "2026-11-02" / "20261102T220000Z.csv"
    )
    synb_rows = _read_xlsx_rows(
        RAW_ROOT / "synthetic" / "SYNB" / "2026-11-02" / "20261102T220000Z.xlsx"
    )
    syna_mapl = next(row for row in syna_rows if row[0] == "MAPL")
    synb_mapl = next(row for row in synb_rows if row[1] == "MAPL")
    assert syna_mapl[7] == "XTSE"
    assert synb_mapl[4] == "XNYS"
    assert not syna_mapl[2] and not synb_mapl[3]


def test_percent_fraction_pair_recorded() -> None:
    pair = _manifest()["percent_fraction_pair"]
    assert pair == {
        "fund_a": "SYNA",
        "unit_a": "percent",
        "fund_b": "SYNB",
        "unit_b": "fraction",
    }


def test_expected_overlap_matches_ground_truth() -> None:
    overlap = _overlap_fixture()
    weights_a = ground_truth_holdings("SYNA", date(2026, 11, 2))
    weights_b = ground_truth_holdings("SYNB", date(2026, 11, 2))
    common_ids = sorted(
        security_id
        for security_id in set(weights_a) & set(weights_b)
        if not security_id.startswith("CASH:")
    )
    assert overlap.common_count == len(common_ids)
    assert [entry.security_id for entry in overlap.common] == common_ids
    recomputed = sum(
        (min(weights_a[sid], weights_b[sid]) for sid in common_ids), Decimal(0)
    ).quantize(Decimal("0.0001"))
    assert Decimal(overlap.overlap) == recomputed
    for entry in overlap.common:
        assert Decimal(entry.weight_a) == weights_a[entry.security_id]
        assert Decimal(entry.weight_b) == weights_b[entry.security_id]


def _overlap_of(weights_a: dict[str, Decimal], weights_b: dict[str, Decimal]) -> Decimal:
    common_ids = {
        security_id
        for security_id in set(weights_a) & set(weights_b)
        if not security_id.startswith(("CASH:", "DERIV:"))
    }
    return sum((min(weights_a[sid], weights_b[sid]) for sid in common_ids), Decimal(0)).quantize(
        Decimal("0.0001")
    )


@pytest.mark.parametrize(
    "leak",
    [
        pytest.param("syna", id="late SYNA 2026-11-03 file leaks"),
        pytest.param("synb", id="republished SYNB 2026-11-02 file leaks"),
        pytest.param("both", id="both late files leak"),
    ],
)
def test_lookahead_versions_would_change_the_overlap(leak: str) -> None:
    """Each late-known file alone must move the 2026-11-02 overlap by >= 0.05."""
    overlap = _overlap_fixture()
    leaked_cutoff = datetime(2026, 11, 4, tzinfo=UTC)
    weights_a = ground_truth_holdings(
        "SYNA", date(2026, 11, 2), known_before=leaked_cutoff if leak in ("syna", "both") else None
    )
    weights_b = ground_truth_holdings(
        "SYNB", date(2026, 11, 2), known_before=leaked_cutoff if leak in ("synb", "both") else None
    )
    leaked = _overlap_of(weights_a, weights_b)
    gap = abs(leaked - Decimal(overlap.overlap))
    assert gap >= Decimal("0.05"), (leak, gap)


def test_republished_file_has_two_versions_with_one_corrected_weight() -> None:
    cutoff_before_republish = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)
    cutoff_after_republish = datetime(2026, 10, 31, 23, 0, tzinfo=UTC)
    version_1 = ground_truth_holdings(
        "SYNB", date(2026, 10, 30), known_before=cutoff_before_republish
    )
    version_2 = ground_truth_holdings(
        "SYNB", date(2026, 10, 30), known_before=cutoff_after_republish
    )
    assert version_1 != version_2
    changed = {
        security_id
        for security_id in set(version_1) | set(version_2)
        if version_1.get(security_id) != version_2.get(security_id)
    }
    assert len(changed) == 2
    assert "CASH:CAD" in changed
    assert Decimal("0.0430") == version_1["ISIN:CA600000AA15"]
    assert Decimal("0.0470") == version_2["ISIN:CA600000AA15"]
    manifest = _manifest()
    republished = manifest["republished_file"]
    assert republished["fund_ticker"] == "SYNB"
    assert [entry["note"] for entry in republished["versions"]] == [
        "file_version 1",
        "file_version 2; one weight corrected",
    ]
    for entry in republished["versions"]:
        assert (RAW_ROOT / str(entry["path"])).is_file()


def test_pdf_stated_total_equals_table_sum() -> None:
    manifest = _manifest()
    pdf_case = manifest["pdf_stated_total"]
    pdf_path = RAW_ROOT / str(pdf_case["path"])
    with pdfplumber.open(pdf_path) as pdf:
        text = pdf.pages[0].extract_text()
    assert "Summary of Investment Portfolio as at 2026-09-30" in text
    assert "Published 2026-10-28" in text
    values = [
        Decimal(match.group(2))
        for line in text.splitlines()
        if (match := re.match(r"^(.+?)\s+(\d{1,3}\.\d{2})$", line))
        and not line.startswith("Summary")
    ]
    assert len(values) == 25
    table_sum = sum(values)
    assert f"{table_sum:.2f}" == pdf_case["table_sum"] == pdf_case["stated_total"]
    assert "Total of the holdings listed above" in text


def test_security_ids_are_listed_in_manifest_as_common_identifiable() -> None:
    overlap = _overlap_fixture()
    assert len(overlap.common) == overlap.common_count == 8
    assert {entry.security_id.split(":")[0] for entry in overlap.common} == {"ISIN", "TKR"}
    assert "TKR:XNYS:ELM" in {entry.security_id for entry in overlap.common}
    assert "TKR:XNYS:MAPL" != "TKR:XTSE:MAPL"


def test_import_error_for_unknown_fund() -> None:
    with pytest.raises(KeyError):
        ground_truth_holdings("NOPE", date(2026, 11, 2))
