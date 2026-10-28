"""TiingoPrices: header auth, raw-store provenance, caching, budget and the gate."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest

from etfh.http import PoliteClient
from etfh.prices import (
    CLOSE_SCHEMA,
    ApiKeyMissingError,
    BudgetExceededError,
    PriceBudget,
    PriceFetchError,
    PriceSourceError,
    TiingoPrices,
)
from etfh.registry import Source, TermsNotAllowedError, load_sources
from etfh.storage import read_meta
from etfh.types import Decision
from tests.conftest import FakeClock
from tests.test_prices import CONFIG_DIR, scratch_config

USER_AGENT = "cad-etf-holdings-tests (https://example.com)"
KEY = "sentinel-key-do-not-log-0123456789"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
START = date(2026, 10, 19)
END = date(2026, 10, 23)


def _row(day: date, close: float, adjusted: float | None = None) -> dict[str, Any]:
    return {
        "date": f"{day.isoformat()}T00:00:00.000Z",
        "close": close,
        "adjClose": close if adjusted is None else adjusted,
        "high": close + 1.0,
        "low": close - 1.0,
        "open": close,
        "volume": 1000,
        "splitFactor": 1.0,
        "divCash": 0.0,
    }


def _payload(close: float = 100.0) -> list[dict[str, Any]]:
    return [
        _row(date(2026, 10, 19), close, close - 0.5),
        _row(date(2026, 10, 20), close + 1.0, close + 0.5),
        _row(date(2026, 10, 21), close + 2.0, close + 1.5),
        _row(date(2026, 10, 22), close + 3.0, close + 2.5),
        _row(date(2026, 10, 23), close + 4.0, close + 3.5),
    ]


class FakeTiingo:
    """A MockTransport handler that plays API responses and records requests."""

    def __init__(
        self,
        payloads: dict[str, Any] | None = None,
        statuses: dict[str, int] | None = None,
    ) -> None:
        self.payloads = payloads if payloads is not None else {"AAPL": _payload()}
        self.statuses = statuses or {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="no robots")
        symbol = request.url.path.strip("/").split("/")[2]
        status = self.statuses.get(symbol, 200)
        if status != 200:
            return httpx.Response(status, json={"detail": f"Error: Ticker '{symbol}' not found"})
        return httpx.Response(200, json=self.payloads[symbol])

    @property
    def data_requests(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path != "/robots.txt"]


def _sources() -> dict[str, Source]:
    return load_sources(CONFIG_DIR / "sources.yaml")


def _polite(fake: FakeTiingo, fake_clock: FakeClock) -> PoliteClient:
    return PoliteClient(
        user_agent=USER_AGENT,
        client=httpx.Client(transport=httpx.MockTransport(fake.handler)),
        clock=fake_clock.time,
        sleep=fake_clock.sleep,
    )


def _tiingo(
    tmp_path: Path,
    fake_clock: FakeClock,
    fake: FakeTiingo,
    *,
    sources: dict[str, Source] | None = None,
    budget: PriceBudget | None = None,
    key: str = KEY,
) -> TiingoPrices:
    return TiingoPrices(
        key,
        tmp_path / "raw",
        sources=sources if sources is not None else _sources(),
        budget=budget
        if budget is not None
        else PriceBudget(tmp_path / "budget.json", clock=lambda: NOW),
        client=_polite(fake, fake_clock),
        clock=lambda: NOW,
    )


def test_authorization_header_carries_the_key_and_nothing_else_does(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    fake = FakeTiingo()
    prices = _tiingo(tmp_path, fake_clock, fake)
    prices.daily_closes(["AAPL"], START, END)
    (request,) = fake.data_requests
    assert request.headers["authorization"] == f"Token {KEY}"
    assert request.headers["user-agent"] == USER_AGENT
    assert KEY not in str(request.url)
    assert "token=" not in str(request.url)
    for path in (tmp_path / "raw").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert KEY not in text, path


def test_raw_response_is_stored_with_provenance(tmp_path: Path, fake_clock: FakeClock) -> None:
    fake = FakeTiingo()
    prices = _tiingo(tmp_path, fake_clock, fake)
    prices.daily_closes(["AAPL"], START, END)
    files = sorted((tmp_path / "raw" / "tiingo" / "AAPL").glob("*/*.json"))
    assert [path.name.endswith(".meta.json") for path in files] == [False, True]
    meta = read_meta(files[1])
    assert meta["source_id"] == "tiingo_prices"
    assert meta["issuer"] == "tiingo"
    assert meta["fund_ticker"] == "AAPL"
    assert meta["http_status"] == 200
    assert meta["as_of_hint"] == END.isoformat()
    assert "startDate=2026-10-19" in meta["url"]
    assert "endDate=2026-10-23" in meta["url"]
    assert files[0].read_bytes() == httpx.Response(200, json=_payload()).content


def test_second_call_reuses_the_stored_response(tmp_path: Path, fake_clock: FakeClock) -> None:
    fake = FakeTiingo()
    prices = _tiingo(tmp_path, fake_clock, fake)
    first = prices.daily_closes(["AAPL"], START, END)
    second = prices.daily_closes(["AAPL"], START, END)
    assert first.equals(second)
    assert len(fake.data_requests) == 1


def test_a_wider_range_fetches_again(tmp_path: Path, fake_clock: FakeClock) -> None:
    fake = FakeTiingo()
    prices = _tiingo(tmp_path, fake_clock, fake)
    prices.daily_closes(["AAPL"], START, END)
    prices.daily_closes(["AAPL"], date(2026, 10, 12), END)
    assert len(fake.data_requests) == 2


def test_budget_refuses_before_the_second_request(tmp_path: Path, fake_clock: FakeClock) -> None:
    fake = FakeTiingo({"AAPL": _payload(), "MSFT": _payload(200.0)})
    budget = PriceBudget(tmp_path / "budget.json", per_hour=1, clock=lambda: NOW)
    prices = _tiingo(tmp_path, fake_clock, fake, budget=budget)
    with pytest.raises(BudgetExceededError):
        prices.daily_closes(["AAPL", "MSFT"], START, END)
    assert len(fake.data_requests) == 1
    assert not (tmp_path / "raw" / "tiingo" / "MSFT").exists()
    assert budget.usage().hour_requests == 1


def test_unknown_ticker_is_absent_not_an_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    fake = FakeTiingo({"AAPL": _payload()}, statuses={"ZZZZ": 404})
    prices = _tiingo(tmp_path, fake_clock, fake)
    frame = prices.daily_closes(["AAPL", "ZZZZ"], START, END)
    assert frame["symbol"].unique().to_list() == ["AAPL"]
    assert frame.height == 5
    detail = (tmp_path / "raw" / "tiingo" / "ZZZZ").glob("*/*.json")
    stored = [path for path in detail if not path.name.endswith(".meta.json")]
    assert len(stored) == 1
    assert b"not found" in stored[0].read_bytes()


def test_error_status_raises_without_leaking_the_key(tmp_path: Path, fake_clock: FakeClock) -> None:
    fake = FakeTiingo({"AAPL": _payload()}, statuses={"AAPL": 403})
    prices = _tiingo(tmp_path, fake_clock, fake)
    with pytest.raises(PriceFetchError) as excinfo:
        prices.daily_closes(["AAPL"], START, END)
    assert "403" in str(excinfo.value)
    assert KEY not in str(excinfo.value)
    assert KEY not in str(tmp_path)


def test_adjusted_close_is_used_and_out_of_range_rows_are_dropped(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    payload = [
        _row(date(2026, 10, 16), 90.0, 89.0),  # before start
        *_payload(),
        _row(date(2026, 10, 26), 120.0, 119.0),  # after end
    ]
    fake = FakeTiingo({"AAPL": payload})
    prices = _tiingo(tmp_path, fake_clock, fake)
    frame = prices.daily_closes(["AAPL"], START, END)
    assert frame["date"].to_list() == [
        date(2026, 10, 19),
        date(2026, 10, 20),
        date(2026, 10, 21),
        date(2026, 10, 22),
        date(2026, 10, 23),
    ]
    assert frame["close"].to_list() == [99.5, 100.5, 101.5, 102.5, 103.5]


def test_frame_matches_the_wave_contract(tmp_path: Path, fake_clock: FakeClock) -> None:
    fake = FakeTiingo({"AAPL": _payload(), "MSFT": _payload(300.0)})
    prices = _tiingo(tmp_path, fake_clock, fake)
    frame = prices.daily_closes(["MSFT", "AAPL"], START, END)
    assert frame.columns == ["symbol", "date", "close"]
    assert frame.schema == CLOSE_SCHEMA
    assert frame.sort(["symbol", "date"]).equals(frame)


def test_rows_without_a_date_or_close_are_skipped(tmp_path: Path, fake_clock: FakeClock) -> None:
    payload = [*_payload(), {"date": "not-a-date", "close": 5.0}, {"date": "2026-10-22"}]
    fake = FakeTiingo({"AAPL": payload})
    prices = _tiingo(tmp_path, fake_clock, fake)
    frame = prices.daily_closes(["AAPL"], START, END)
    assert frame.height == 5


def test_a_non_array_response_is_a_fetch_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    fake = FakeTiingo()
    fake.payloads = {"AAPL": {"detail": "unexpected"}}
    prices = _tiingo(tmp_path, fake_clock, fake)
    with pytest.raises(PriceFetchError, match="not a JSON array"):
        prices.daily_closes(["AAPL"], START, END)


def test_pending_review_copy_refuses_before_cache_or_network(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    fake = FakeTiingo()
    allowed = _sources()
    prices = _tiingo(tmp_path, fake_clock, fake, sources=allowed)
    prices.daily_closes(["AAPL"], START, END)
    assert len(fake.data_requests) == 1

    blocked = allowed["tiingo_prices"].model_copy(
        update={
            "terms": allowed["tiingo_prices"].terms.model_copy(
                update={"decision": Decision.pending_review}
            )
        }
    )
    gated = _tiingo(tmp_path, fake_clock, fake, sources={**allowed, "tiingo_prices": blocked})
    with pytest.raises(TermsNotAllowedError):
        gated.daily_closes(["AAPL"], START, END)
    assert len(fake.data_requests) == 1  # the cache was not used either


def test_not_allowed_copy_refuses(tmp_path: Path, fake_clock: FakeClock) -> None:
    allowed = _sources()
    blocked = allowed["tiingo_prices"].model_copy(
        update={
            "terms": allowed["tiingo_prices"].terms.model_copy(
                update={"decision": Decision.not_allowed}
            )
        }
    )
    sources = {**allowed, "tiingo_prices": blocked}
    gated = _tiingo(tmp_path, fake_clock, FakeTiingo(), sources=sources)
    with pytest.raises(TermsNotAllowedError):
        gated.daily_closes(["AAPL"], START, END)


def test_scratch_config_with_allowed_with_conditions_fetches(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    config = scratch_config(tmp_path, decision="allowed_with_conditions", conditions=["internal"])
    sources = load_sources(config / "sources.yaml")
    fake = FakeTiingo()
    prices = _tiingo(tmp_path, fake_clock, fake, sources=sources)
    frame = prices.daily_closes(["AAPL"], START, END)
    assert frame.height == 5
    assert len(fake.data_requests) == 1


def test_from_env_needs_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    with pytest.raises(ApiKeyMissingError, match="TIINGO_API_KEY"):
        TiingoPrices.from_env(
            tmp_path / "raw", sources=_sources(), budget_path=tmp_path / "budget.json"
        )


def test_from_env_sends_the_environment_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    monkeypatch.setenv("TIINGO_API_KEY", KEY)
    fake = FakeTiingo()
    prices = TiingoPrices.from_env(
        tmp_path / "raw",
        sources=_sources(),
        budget_path=tmp_path / "budget.json",
        client=_polite(fake, fake_clock),
        clock=lambda: NOW,
    )
    prices.daily_closes(["AAPL"], START, END)
    (request,) = fake.data_requests
    assert request.headers["authorization"] == f"Token {KEY}"
    assert (tmp_path / "budget.json").is_file()


def test_a_config_without_the_source_is_reported(tmp_path: Path, fake_clock: FakeClock) -> None:
    with pytest.raises(PriceSourceError, match="tiingo_prices"):
        _tiingo(tmp_path, fake_clock, FakeTiingo(), sources={})


def test_empty_key_is_rejected(tmp_path: Path, fake_clock: FakeClock) -> None:
    with pytest.raises(ApiKeyMissingError):
        _tiingo(tmp_path, fake_clock, FakeTiingo(), key="   ")


def test_empty_symbol_list_makes_no_request(tmp_path: Path, fake_clock: FakeClock) -> None:
    fake = FakeTiingo()
    prices = _tiingo(tmp_path, fake_clock, fake)
    frame = prices.daily_closes([], START, END)
    assert frame.height == 0
    assert frame.schema == CLOSE_SCHEMA
    assert fake.requests == []


def test_reversed_range_is_a_caller_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    prices = _tiingo(tmp_path, fake_clock, FakeTiingo())
    with pytest.raises(ValueError, match="after end"):
        prices.daily_closes(["AAPL"], END, START)


def test_closes_are_positive(tmp_path: Path, fake_clock: FakeClock) -> None:
    prices = _tiingo(tmp_path, fake_clock, FakeTiingo({"AAPL": _payload()}))
    frame = prices.daily_closes(["AAPL"], START, END)
    assert (frame["close"] > 0).all()
    assert frame["close"].dtype == pl.Float64


@pytest.mark.network
def test_live_tiingo_returns_adjusted_closes(tmp_path: Path) -> None:
    key = os.environ.get("TIINGO_API_KEY", "")
    if not key.strip():
        pytest.skip("TIINGO_API_KEY is not set")
    prices = TiingoPrices.from_env(
        tmp_path / "raw", sources=_sources(), budget_path=tmp_path / "budget.json"
    )
    try:
        frame = prices.daily_closes(["SPY"], date(2026, 1, 5), date(2026, 1, 9))
    finally:
        prices.close()
    assert frame.schema == CLOSE_SCHEMA
    assert frame.height >= 1
    assert (frame["close"] > 0).all()
