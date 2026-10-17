"""Tests for the frozen Pandera schemas and DuckDB DDL."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandera.polars as pa
import polars as pl
import pytest

from etfh.schema import (
    DDL_FILE_VERSIONS,
    DDL_HOLDINGS,
    DDL_HOLDINGS_CHANGES,
    DDL_QA_EVENTS,
    FileVersion,
    Holdings,
    HoldingsChange,
    ParsedHoldings,
    QaEvent,
)

SHA = "0" * 32 + "1" * 32


def _parsed_frame(**overrides: pl.Series | list[object]) -> pl.DataFrame:
    data: dict[str, object] = {
        "fund_ticker": ["SYNA"],
        "as_of_date": pl.Series([date(2026, 10, 29)], dtype=pl.Date),
        "security_name": ["Maple Industries Inc."],
        "security_ticker": pl.Series(["MAPL"], dtype=pl.String),
        "exchange": pl.Series(["XTSE"], dtype=pl.String),
        "isin": pl.Series(["CA0000000001"], dtype=pl.String),
        "cusip": pl.Series([None], dtype=pl.String),
        "asset_class": ["equity"],
        "sector": ["Industrials"],
        "country": ["CA"],
        "currency": pl.Series(["CAD"], dtype=pl.String),
        "weight": [0.05],
        "shares": [1000.0],
        "market_value": pl.Series([50000.0], dtype=pl.Float64),
        "source_id": ["syn_csv"],
        "raw_file_sha256": [SHA],
    }
    data.update(overrides)
    return pl.DataFrame(data)


def _holdings_frame(**overrides: pl.Series | list[object]) -> pl.DataFrame:
    frame = _parsed_frame(**overrides)
    frame = frame.with_columns(
        pl.Series(
            "known_at", [datetime(2026, 10, 29, 22, tzinfo=UTC)], dtype=pl.Datetime("us", "UTC")
        ),
        pl.Series("file_version", [1], dtype=pl.Int32),
        pl.Series("security_id", ["ISIN:CA0000000001"], dtype=pl.String),
    )
    ordered = frame.select(
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
    )
    return ordered


class TestParsedHoldings:
    def test_valid_frame_passes(self) -> None:
        validated = ParsedHoldings.validate(_parsed_frame())
        assert validated.height == 1

    def test_wrong_dtype_fails(self) -> None:
        frame = _parsed_frame(weight=pl.Series(["5.2"], dtype=pl.String))
        with pytest.raises(pa.errors.SchemaError):
            ParsedHoldings.validate(frame)

    def test_bad_isin_length_fails(self) -> None:
        frame = _parsed_frame(isin=pl.Series(["US037833100"], dtype=pl.String))
        with pytest.raises(pa.errors.SchemaError):
            ParsedHoldings.validate(frame)

    @pytest.mark.parametrize("bad_weight", [float("nan"), float("inf"), -1.5, 1.5])
    def test_non_finite_or_out_of_range_weight_fails(self, bad_weight: float) -> None:
        frame = _parsed_frame(weight=[bad_weight])
        with pytest.raises(pa.errors.SchemaError):
            ParsedHoldings.validate(frame)

    def test_null_weight_fails(self) -> None:
        frame = _parsed_frame(weight=pl.Series([None], dtype=pl.Float64))
        with pytest.raises(pa.errors.SchemaError):
            ParsedHoldings.validate(frame)

    def test_negative_weight_and_negative_values_pass(self) -> None:
        frame = _parsed_frame(
            weight=[-0.45],
            shares=[-1200.0],
            market_value=pl.Series([-45000.0], dtype=pl.Float64),
        )
        assert ParsedHoldings.validate(frame).height == 1

    def test_null_shares_pass(self) -> None:
        frame = _parsed_frame(shares=pl.Series([None], dtype=pl.Float64))
        assert ParsedHoldings.validate(frame).height == 1

    def test_all_null_nullable_column_with_dtype_passes(self) -> None:
        frame = _parsed_frame(
            cusip=pl.Series([None], dtype=pl.String),
            security_ticker=pl.Series([None], dtype=pl.String),
        )
        assert ParsedHoldings.validate(frame).height == 1

    def test_bad_sha256_fails(self) -> None:
        frame = _parsed_frame(raw_file_sha256=["ABC"])
        with pytest.raises(pa.errors.SchemaError):
            ParsedHoldings.validate(frame)

    def test_uppercase_sha256_fails(self) -> None:
        frame = _parsed_frame(raw_file_sha256=["A" * 64])
        with pytest.raises(pa.errors.SchemaError):
            ParsedHoldings.validate(frame)


class TestHoldings:
    def test_valid_frame_passes(self) -> None:
        assert Holdings.validate(_holdings_frame()).height == 1

    def test_naive_known_at_fails(self) -> None:
        frame = _holdings_frame().with_columns(
            pl.Series("known_at", [datetime(2026, 10, 29, 22)], dtype=pl.Datetime("us"))  # noqa: DTZ001
        )
        with pytest.raises(pa.errors.SchemaError):
            Holdings.validate(frame)

    @pytest.mark.parametrize("bad_version", [0, -1])
    def test_file_version_below_one_fails(self, bad_version: int) -> None:
        frame = _holdings_frame().with_columns(
            pl.Series("file_version", [bad_version], dtype=pl.Int32)
        )
        with pytest.raises(pa.errors.SchemaError):
            Holdings.validate(frame)

    def test_null_security_id_allowed(self) -> None:
        frame = _holdings_frame().with_columns(pl.Series("security_id", [None], dtype=pl.String))
        assert Holdings.validate(frame).height == 1

    def test_short_line_with_null_shares_and_negative_weight_passes(self) -> None:
        frame = _holdings_frame().with_columns(
            pl.Series("weight", [-0.3], dtype=pl.Float64),
            pl.Series("shares", [None], dtype=pl.Float64),
            pl.Series("market_value", [-25000.0], dtype=pl.Float64),
        )
        assert Holdings.validate(frame).height == 1


class TestOtherModels:
    def test_file_version_status_restricted(self) -> None:
        frame = pl.DataFrame(
            {
                "source_id": ["syn_csv"],
                "fund_ticker": ["SYNA"],
                "as_of_date": pl.Series([date(2026, 10, 29)], dtype=pl.Date),
                "file_version": pl.Series([1], dtype=pl.Int32),
                "known_at": pl.Series(
                    [datetime(2026, 10, 29, 22, tzinfo=UTC)], dtype=pl.Datetime("us", "UTC")
                ),
                "raw_file_sha256": [SHA],
                "path": ["raw/synthetic/SYNA/2026-10-29/20261029T220000Z.csv"],
                "status": ["included"],
            }
        )
        assert FileVersion.validate(frame).height == 1
        with pytest.raises(pa.errors.SchemaError):
            FileVersion.validate(frame.with_columns(pl.lit("dropped").alias("status")))

    def test_qa_event_null_as_of_date_allowed(self) -> None:
        frame = pl.DataFrame(
            {
                "raw_file_sha256": [SHA],
                "source_id": ["syn_csv"],
                "fund_ticker": ["SYNA"],
                "as_of_date": pl.Series([None], dtype=pl.Date),
                "check": ["weight_sum"],
                "severity": ["error"],
                "detail": ["weights total 0.85"],
                "excluded": [True],
            }
        )
        assert QaEvent.validate(frame).height == 1
        with pytest.raises(pa.errors.SchemaError):
            QaEvent.validate(frame.with_columns(pl.lit("fatal").alias("severity")))

    def test_holdings_change_delta_nullable(self) -> None:
        frame = pl.DataFrame(
            {
                "fund_ticker": ["SYNA"],
                "from_as_of": pl.Series([date(2026, 10, 29)], dtype=pl.Date),
                "to_as_of": pl.Series([date(2026, 10, 30)], dtype=pl.Date),
                "security_id": pl.Series([None], dtype=pl.String),
                "change_type": ["add"],
                "weight_before": pl.Series([None], dtype=pl.Float64),
                "weight_after": [0.05],
                "delta": pl.Series([None], dtype=pl.Float64),
            }
        )
        assert HoldingsChange.validate(frame).height == 1


def _ddl_columns(ddl: str) -> list[str]:
    body = ddl[ddl.index("(") + 1 : ddl.rindex(")")]
    return [line.strip().split()[0] for line in body.strip().splitlines() if line.strip()]


@pytest.mark.parametrize(
    ("ddl", "model"),
    [
        (DDL_FILE_VERSIONS, FileVersion),
        (DDL_HOLDINGS, Holdings),
        (DDL_QA_EVENTS, QaEvent),
        (DDL_HOLDINGS_CHANGES, HoldingsChange),
    ],
)
def test_ddl_columns_match_models(ddl: str, model: type[pa.DataFrameModel]) -> None:
    assert _ddl_columns(ddl) == list(model.to_schema().columns)
