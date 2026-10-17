"""Tests for the raw store layout, sidecars, deduplication and iteration."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from etfh.storage import RawStore, read_meta
from etfh.types import RawFile

FETCHED = datetime(2026, 10, 29, 22, 0, 0, tzinfo=UTC)
DAY = date(2026, 10, 29)
CONTENT = b"ticker,weight\nMAPL,0.052\n"


def _write(store: RawStore, content: bytes = CONTENT, **overrides: object) -> RawFile:
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
        "etag": '"abc"',
        "last_modified": "Wed, 29 Oct 2026 22:00:00 GMT",
    }
    kwargs.update(overrides)
    return store.write(content, **kwargs)


def test_layout(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    raw = _write(store)
    expected = tmp_path / "synthetic" / "SYNA" / "2026-10-29" / "20261029T220000Z.csv"
    assert raw.path == expected
    assert raw.path.read_bytes() == CONTENT
    assert raw.ext == "csv"
    assert raw.fetched_at == FETCHED


def test_bytes_are_unmodified(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    content = b"\x00\x01binary\r\ncontent\xff"
    raw = _write(store, content=content, ext="bin")
    assert raw.path.read_bytes() == content
    assert raw.sha256 == hashlib.sha256(content).hexdigest()


def test_sidecar_contents(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    raw = _write(store)
    sidecar = raw.path.with_name("20261029T220000Z.meta.json")
    meta = store.read_meta(sidecar)
    assert meta == {
        "url": "synthetic://fixtures/SYNA.csv",
        "source_id": "syn_csv",
        "issuer": "synthetic",
        "fund_ticker": "SYNA",
        "as_of_hint": "2026-10-29",
        "fetched_at": "2026-10-29T22:00:00+00:00",
        "sha256": raw.sha256,
        "content_type": "text/csv",
        "http_status": 200,
        "etag": '"abc"',
        "last_modified": "Wed, 29 Oct 2026 22:00:00 GMT",
        "bytes": len(CONTENT),
    }
    assert read_meta(sidecar) == meta


def test_identical_refetch_deduplicated(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    first = _write(store)
    second = _write(store, fetched_at=datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC))
    assert second.path == first.path
    assert second.fetched_at == first.fetched_at
    directory = first.path.parent
    assert sorted(path.name for path in directory.iterdir()) == [
        "20261029T220000Z.csv",
        "20261029T220000Z.meta.json",
    ]


def test_different_content_same_day_keeps_both(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    first = _write(store)
    second = _write(
        store,
        content=CONTENT + b"SHOP,0.03\n",
        fetched_at=datetime(2026, 10, 30, 14, 0, 0, tzinfo=UTC),
    )
    assert second.path != first.path
    assert len(store.iter_raw()) == 2


def test_naive_fetched_at_rejected(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        _write(store, fetched_at=datetime(2026, 10, 29, 22, 0, 0))  # noqa: DTZ001


def test_fetch_date_used_without_as_of_hint(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    raw = _write(store, as_of_hint=None)
    assert raw.path.parent.name == "2026-10-29"
    assert raw.as_of_hint is None


def test_iter_raw_deterministic_order(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    later = datetime(2026, 10, 30, 22, 0, 0, tzinfo=UTC)
    writes = [
        dict(
            fund_ticker="SYNB",
            issuer="synthetic",
            url="u5",
            fetched_at=later,
            as_of_hint=date(2026, 10, 30),
        ),
        dict(fund_ticker="SYNA", issuer="synthetic", url="u1", fetched_at=FETCHED, as_of_hint=DAY),
        dict(fund_ticker="SYNA", issuer="synthetic", url="u3", fetched_at=later, as_of_hint=DAY),
        dict(fund_ticker="SYNC", issuer="alpha", url="u2", fetched_at=FETCHED, as_of_hint=DAY),
    ]
    for index, kwargs in enumerate(writes):
        _write(store, content=f"content-{index}".encode(), source_id="syn_csv", ext="csv", **kwargs)
    raws = store.iter_raw()
    assert [(raw.issuer, raw.fund_ticker, raw.fetched_at) for raw in raws] == [
        ("alpha", "SYNC", FETCHED),
        ("synthetic", "SYNA", FETCHED),
        ("synthetic", "SYNA", later),
        ("synthetic", "SYNB", later),
    ]
    assert [raw.sha256 for raw in raws] == [raw.sha256 for raw in store.iter_raw()]


def test_iter_raw_empty_when_root_missing(tmp_path: Path) -> None:
    assert RawStore(tmp_path / "missing").iter_raw() == []
