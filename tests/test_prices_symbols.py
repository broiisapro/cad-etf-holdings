"""symbol_for and cached_figi_resolver: row → price-source symbol.

``cached_figi_resolver`` reads the on-disk JSON cache that
:class:`etfh.secmaster.openfigi.FigiLookup` writes. The fixture below reproduces
that format exactly (field names and values, one file per cache key) so the
tests exercise the real contract, not a mirror of it; nothing here touches the
network and nothing writes to a cache the resolver owns.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from etfh.prices import cached_figi_resolver, symbol_for


def _write_cache_entry(
    cache_dir: Path,
    id_type: str,
    value: str,
    composite: str | None,
    *,
    mic: str | None = None,
    exch_code: str | None = None,
    ambiguous: bool = False,
) -> Path:
    """Write one cache file the way ``FigiLookup._write_cache`` does."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        f"{id_type}_{value}_{mic or 'none'}_{exch_code or 'none'}",
    )
    digest = hashlib.sha256(
        "|".join((id_type, value, mic or "", exch_code or "")).encode()
    ).hexdigest()[:12]
    path = cache_dir / f"{safe}-{digest}.json"
    path.write_text(
        json.dumps(
            {
                "id_type": id_type,
                "value": value,
                "mic": mic,
                "exch_code": exch_code,
                "composite_figi": composite,
                "ambiguous": ambiguous,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _us_security(cache_dir: Path, isin: str, composite: str, ticker: str) -> None:
    """An ISIN that maps to ``composite`` plus the US ticker cached for it."""
    _write_cache_entry(cache_dir, "ID_ISIN", isin, composite)
    _write_cache_entry(cache_dir, "TICKER", ticker, composite, exch_code="US")


@pytest.mark.parametrize(
    ("security_id", "security_ticker", "exchange", "expected"),
    [
        (None, None, None, None),
        ("ISIN:CA0000000001", None, None, None),
        ("CUSIP:000000001", None, None, None),
        (None, "AAPL", "XNAS", "AAPL"),
        ("TKR:XNAS:AAPL", None, None, "AAPL"),
        (None, "BRK.B", "XNYS", "BRK-B"),
        (None, "BRK-B", "XNYS", "BRK-B"),
        ("TKR:XNYS:BRK.B", None, None, "BRK-B"),
        (None, "aapl", "NASDAQ GS", "AAPL"),
        (None, "SPY", "NYSE ARCA", "SPY"),
        (None, "IBM", "NYSE", "IBM"),
        (None, "AAPL", "XASE", "AAPL"),
        (None, "AAPL", "BATS", "AAPL"),
        (None, "AAPL", "TSX", None),
        (None, "AAPL", "XTSE", None),
        ("TKR:XTSE:RY", None, None, None),
        ("TKR:XTSE:RY", None, "XTSE", None),
        (None, "AAPL", "US", None),
        (None, "AAPL", None, None),
        (None, None, "XNAS", None),
        ("ISIN:CA0000000001", "AAPL", "TSX", None),
    ],
)
def test_symbol_for_is_us_only_and_total(
    security_id: str | None, security_ticker: str | None, exchange: str | None, expected: str | None
) -> None:
    assert symbol_for(security_id, security_ticker, exchange) == expected


def test_cached_figi_resolver_maps_an_isin_to_its_us_ticker(tmp_path: Path) -> None:
    cache = tmp_path / "openfigi"
    _us_security(cache, "US0378331005", "BBG000B9XRY4", "AAPL")
    resolve = cached_figi_resolver(cache)

    assert resolve("ISIN:US0378331005", None, None) == "AAPL"
    assert resolve(None, None, None) is None


def test_cached_figi_resolver_maps_a_figi_id_directly(tmp_path: Path) -> None:
    cache = tmp_path / "openfigi"
    _us_security(cache, "US0378331005", "BBG000B9XRY4", "AAPL")
    resolve = cached_figi_resolver(cache)

    assert resolve("FIGI:BBG000B9XRY4", None, None) == "AAPL"


def test_cached_figi_resolver_maps_a_cusip_id(tmp_path: Path) -> None:
    cache = tmp_path / "openfigi"
    _write_cache_entry(cache, "ID_CUSIP", "037833100", "BBG000B9XRY4")
    _write_cache_entry(cache, "TICKER", "AAPL", "BBG000B9XRY4", exch_code="US")
    resolve = cached_figi_resolver(cache)

    assert resolve("CUSIP:037833100", None, None) == "AAPL"


def test_cached_figi_resolver_ignores_a_canadian_only_listing(tmp_path: Path) -> None:
    """A composite with no US listing is not a US price source."""
    cache = tmp_path / "openfigi"
    _write_cache_entry(cache, "ID_ISIN", "CA6330671034", "BBG000BZQ983")
    _write_cache_entry(cache, "TICKER", "XYZ", "BBG000BZQ983", exch_code="CN")
    resolve = cached_figi_resolver(cache)

    assert resolve("ISIN:CA6330671034", None, None) is None


def test_cached_figi_resolver_refuses_two_us_tickers(tmp_path: Path) -> None:
    cache = tmp_path / "openfigi"
    _write_cache_entry(cache, "ID_ISIN", "US0378331005", "BBG000B9XRY4")
    _write_cache_entry(cache, "TICKER", "AAPL", "BBG000B9XRY4", exch_code="US")
    _write_cache_entry(cache, "TICKER", "APL", "BBG000B9XRY4", exch_code="US")
    resolve = cached_figi_resolver(cache)

    assert resolve("ISIN:US0378331005", None, None) is None


def test_cached_figi_resolver_accepts_a_single_us_venue(tmp_path: Path) -> None:
    """``exchCode`` "UW" is Nasdaq, so the listing is US even without "US"."""
    cache = tmp_path / "openfigi"
    _write_cache_entry(cache, "ID_ISIN", "US0378331005", "BBG000B9XRY4")
    _write_cache_entry(cache, "TICKER", "AAPL", "BBG000B9XRY4", exch_code="UW")
    _write_cache_entry(cache, "ID_ISIN", "US5949181045", "BBG000BPH459")
    _write_cache_entry(cache, "TICKER", "MSFT", "BBG000BPH459", mic="XNAS")
    resolve = cached_figi_resolver(cache)

    assert resolve("ISIN:US0378331005", None, None) == "AAPL"
    assert resolve("ISIN:US5949181045", None, None) == "MSFT"


@pytest.mark.parametrize("cached_ticker", ["BRK/B", "BRK.B", "brk/b"])
def test_cached_figi_resolver_formats_class_shares(tmp_path: Path, cached_ticker: str) -> None:
    cache = tmp_path / "openfigi"
    _write_cache_entry(cache, "ID_ISIN", "US0846707026", "BBG000DW76Y6")
    _write_cache_entry(cache, "TICKER", cached_ticker, "BBG000DW76Y6", exch_code="US")
    resolve = cached_figi_resolver(cache)

    assert resolve("ISIN:US0846707026", None, None) == "BRK-B"


def test_cached_figi_resolver_without_a_cache_dir_behaves_like_symbol_for(
    tmp_path: Path,
) -> None:
    resolve = cached_figi_resolver(tmp_path / "missing")
    rows = [
        (None, None, None),
        ("ISIN:US0378331005", None, None),
        ("CUSIP:037833100", None, None),
        ("FIGI:BBG000B9XRY4", None, None),
        (None, "AAPL", "XNAS"),
        ("TKR:XNYS:BRK.B", None, None),
        (None, "AAPL", "XTSE"),
        (None, "AAPL", "US"),
    ]
    assert [resolve(*row) for row in rows] == [symbol_for(*row) for row in rows]


def test_cached_figi_resolver_skips_malformed_cache_files(tmp_path: Path) -> None:
    cache = tmp_path / "openfigi"
    cache.mkdir()
    (cache / "broken.json").write_text("{not json", encoding="utf-8")
    (cache / "list.json").write_text("[1, 2]", encoding="utf-8")
    (cache / "odd.json").write_text(json.dumps({"id_type": "ID_ISIN"}), encoding="utf-8")
    resolve = cached_figi_resolver(cache)

    assert resolve("ISIN:US0378331005", None, None) is None


def test_cached_figi_resolver_ignores_a_cached_ambiguity(tmp_path: Path) -> None:
    cache = tmp_path / "openfigi"
    _write_cache_entry(cache, "ID_ISIN", "US0378331005", "BBG000B9XRY4", ambiguous=True)
    _write_cache_entry(cache, "TICKER", "AAPL", "BBG000B9XRY4", exch_code="US")
    resolve = cached_figi_resolver(cache)

    assert resolve("ISIN:US0378331005", None, None) is None


def test_cached_figi_resolver_prefers_the_row_ticker_over_the_cache(tmp_path: Path) -> None:
    cache = tmp_path / "openfigi"
    _write_cache_entry(cache, "ID_ISIN", "US0378331005", "BBG000B9XRY4")
    _write_cache_entry(cache, "TICKER", "AAPL", "BBG000B9XRY4", exch_code="US")
    resolve = cached_figi_resolver(cache)

    assert resolve("ISIN:US0378331005", "MSFT", "XNYS") == "MSFT"


def test_cached_figi_resolver_reads_the_cache_once_per_resolver(tmp_path: Path) -> None:
    """Answers and the index are kept in memory: a later resolver sees new files."""
    cache = tmp_path / "openfigi"
    _us_security(cache, "US0378331005", "BBG000B9XRY4", "AAPL")
    resolve = cached_figi_resolver(cache)
    assert resolve("ISIN:US0378331005", None, None) == "AAPL"

    _us_security(cache, "US5949181045", "BBG000BPH459", "MSFT")
    assert resolve("ISIN:US5949181045", None, None) is None
    assert cached_figi_resolver(cache)("ISIN:US5949181045", None, None) == "MSFT"


class _ExplodingClient:
    """A transport that fails the test if the resolver tries to use the network."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("cached_figi_resolver must not open an HTTP client")


def test_cached_figi_resolver_never_uses_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "openfigi"
    _us_security(cache, "US0378331005", "BBG000B9XRY4", "AAPL")
    monkeypatch.setattr("etfh.secmaster.openfigi.httpx.Client", _ExplodingClient)
    resolve = cached_figi_resolver(cache)

    assert resolve("ISIN:US0378331005", None, None) == "AAPL"
    assert resolve("ISIN:CA0000000001", None, None) is None
    assert resolve(None, "AAPL", "XNAS") == "AAPL"
