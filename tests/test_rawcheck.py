"""Tests for raw-store verification."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from etfh.rawcheck import RawCheckReport, RawProblem, verify_raw_store
from etfh.storage import RawStore

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNTHETIC_STORE = REPO_ROOT / "tests" / "fixtures" / "raw_synthetic"
FETCHED = datetime(2026, 10, 29, 22, 0, 0, tzinfo=UTC)
DAY = date(2026, 10, 29)
CONTENT = b"ticker,weight\nMAPL,0.052\n"


def _write(store: RawStore, content: bytes = CONTENT, **overrides: object) -> Path:
    kwargs: dict[str, object] = {
        "source_id": "syn_csv",
        "issuer": "synthetic",
        "fund_ticker": "SYNA",
        "url": "synthetic://fixtures/SYNA.csv",
        "fetched_at": FETCHED,
        "ext": "csv",
        "content_type": "text/csv",
        "as_of_hint": DAY,
        "http_status": 200,
    }
    kwargs.update(overrides)
    raw = store.write(content, **kwargs)
    return raw.path


def _relative_files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def _kinds(report: RawCheckReport) -> set[str]:
    return {problem.kind for problem in report.problems}


def test_clean_store_verifies(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    _write(store)
    _write(
        store,
        content=b"ticker,weight\nSHOP,0.03\n",
        fund_ticker="SYNB",
        fetched_at=FETCHED + timedelta(hours=1),
    )

    report = verify_raw_store(tmp_path)

    assert report.ok
    assert report.problems == ()
    assert report.files_checked == 2


def test_missing_sidecar_detected(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    data = _write(store)
    data.with_name("20261029T220000Z.meta.json").unlink()

    report = verify_raw_store(tmp_path)

    assert not report.ok
    assert report.problems == (
        RawProblem(
            "synthetic/SYNA/2026-10-29/20261029T220000Z.csv",
            "missing_sidecar",
            "expected 20261029T220000Z.meta.json next to the data file",
        ),
    )
    assert report.files_checked == 1


def test_orphan_sidecar_detected(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    data = _write(store)
    data.unlink()

    report = verify_raw_store(tmp_path)

    assert not report.ok
    assert [problem.kind for problem in report.problems] == ["orphan_sidecar"]
    assert report.problems[0].path == "synthetic/SYNA/2026-10-29/20261029T220000Z.meta.json"
    assert report.files_checked == 0


def test_sha_mismatch_detected(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    data = _write(store)
    data.write_bytes(CONTENT + b"VOO,0.01\n")

    report = verify_raw_store(tmp_path)

    assert [problem.kind for problem in report.problems] == ["sha_mismatch"]
    assert report.problems[0].path == "synthetic/SYNA/2026-10-29/20261029T220000Z.csv"
    assert "does not match" in report.problems[0].detail


@pytest.mark.parametrize(
    ("sidecar_text", "detail"),
    [
        ("{not json", "cannot read metadata"),
        ("[]", "cannot read metadata"),
        ('{"fetched_at": "2026-10-29T22:00:00+00:00"}', "no valid lowercase sha256"),
        ('{"sha256": "ABC"}', "no valid lowercase sha256"),
    ],
)
def test_bad_sidecar_detected(tmp_path: Path, sidecar_text: str, detail: str) -> None:
    store = RawStore(tmp_path)
    data = _write(store)
    sidecar = data.with_name("20261029T220000Z.meta.json")
    sidecar.write_text(sidecar_text, encoding="utf-8")

    report = verify_raw_store(tmp_path)

    assert [problem.kind for problem in report.problems] == ["bad_sidecar"]
    assert report.problems[0].path == "synthetic/SYNA/2026-10-29/20261029T220000Z.meta.json"
    assert detail in report.problems[0].detail


def test_stray_file_is_unexpected_path(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    _write(store)
    (tmp_path / "notes.txt").write_text("scratch", encoding="utf-8")

    report = verify_raw_store(tmp_path)

    assert [problem.kind for problem in report.problems] == ["unexpected_path"]
    assert report.problems[0].path == "notes.txt"


@pytest.mark.parametrize(
    "relative",
    [
        "synthetic/SYNA/2026-13-01/20261029T220000Z.csv",
        "synthetic/SYNA/2026-10-29/not-a-stamp.csv",
        "synthetic/SYNA/2026-10-29/20261329T220000Z.csv",
        "synthetic/SYNA/2026-10-29/20261029T220000Z.CSV",
        "synthetic/SYNA/2026-10-29/too/deep/20261029T220000Z.csv",
        "synthetic/.hidden/2026-10-29/20261029T220000Z.csv",
        "synthetic/SYNA/2026-10-29/20261029T220000Z.meta.json.bak",
    ],
)
def test_wrong_layout_is_unexpected_path(tmp_path: Path, relative: str) -> None:
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(b"data")

    report = verify_raw_store(tmp_path)

    assert "unexpected_path" in _kinds(report)
    assert any(problem.path == relative for problem in report.problems)


def test_symlink_is_unexpected_path(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    data = _write(store)
    (tmp_path / "link.csv").symlink_to(data)

    report = verify_raw_store(tmp_path)

    assert [problem.kind for problem in report.problems] == ["unexpected_path"]
    assert report.problems[0].path == "link.csv"


def test_two_data_files_sharing_a_stamp_are_unexpected(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    data = _write(store)
    sibling = data.with_suffix(".json")
    sibling.write_bytes(b"{}")

    report = verify_raw_store(tmp_path)

    assert [problem.kind for problem in report.problems] == [
        "unexpected_path",
        "unexpected_path",
    ]
    assert {problem.path for problem in report.problems} == {
        "synthetic/SYNA/2026-10-29/20261029T220000Z.csv",
        "synthetic/SYNA/2026-10-29/20261029T220000Z.json",
    }


def test_reports_every_problem_without_stopping(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    missing = _write(store, fund_ticker="SYNA")
    missing.with_name("20261029T220000Z.meta.json").unlink()
    _write(
        store,
        content=b"ticker,weight\nSHOP,0.03\n",
        fund_ticker="SYNB",
        fetched_at=FETCHED + timedelta(hours=1),
    ).write_bytes(b"changed")
    orphan = _write(
        store,
        content=b"ticker,weight\nCSU,0.02\n",
        fund_ticker="SYNC",
        fetched_at=FETCHED + timedelta(hours=2),
    )
    orphan.unlink()
    (tmp_path / "stray.bin").write_bytes(b"junk")

    report = verify_raw_store(tmp_path)

    assert _kinds(report) == {
        "missing_sidecar",
        "sha_mismatch",
        "orphan_sidecar",
        "unexpected_path",
    }
    assert len(report.problems) == 4
    assert report.files_checked == 2


def test_missing_root_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not a directory"):
        verify_raw_store(tmp_path / "missing")
