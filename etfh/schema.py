"""Frozen schemas for parsed and normalized holdings tables, plus DuckDB DDL.

The ``DataFrameModel`` classes define the frozen data contracts (D6). The DDL
constants create the same tables in DuckDB so ``etfh build`` can write them;
``tests/test_schema.py`` asserts that the DDL columns match the models in name
and order.

Fund metrics (D66) are additive: ``ParsedFundMetrics`` and ``FundMetrics``
follow the same pattern as the holdings models, the holdings models themselves
are unchanged, and ``fund_metrics`` is a new table rather than extra holdings
columns. One row is one fund's NAV per unit, shares outstanding and AUM on one
as-of date, as one source published them in one stored file.
"""

from __future__ import annotations

import pandera.polars as pa
import polars as pl

__all__ = [
    "DDL_FILE_VERSIONS",
    "DDL_FUND_METRICS",
    "DDL_HOLDINGS",
    "DDL_HOLDINGS_CHANGES",
    "DDL_QA_EVENTS",
    "FileVersion",
    "FundMetrics",
    "Holdings",
    "HoldingsChange",
    "ParsedFundMetrics",
    "ParsedHoldings",
    "QaEvent",
]

_HEX64_PATTERN = r"^[0-9a-f]{64}$"
_MIC_PATTERN = r"^[A-Z0-9]{4}$"
_UTC = "UTC"


class ParsedHoldings(pa.DataFrameModel):
    """Parser output: one row per fund/security holding, weights as fractions.

    Every frame produced by a parser must validate against this model before it
    enters validation or the build. Nullable columns must still be given their
    proper dtype by parsers (for example ``pl.String`` for an all-null
    ``cusip`` column), because validation is strict.

    Per D45 (correction to D6): ``shares`` and ``market_value`` are nullable
    and accept any sign, and ``weight`` is constrained to ``[-1, 1]`` so short,
    liability and derivative lines are representable.
    """

    fund_ticker: str = pa.Field(str_length={"min_value": 1})
    as_of_date: pl.Date
    security_name: str = pa.Field(str_length={"min_value": 1})
    security_ticker: str | None = pa.Field(nullable=True)
    exchange: str | None = pa.Field(nullable=True, str_matches=_MIC_PATTERN)
    isin: str | None = pa.Field(nullable=True, str_length=12)
    cusip: str | None = pa.Field(nullable=True, str_length=9)
    asset_class: str = pa.Field(str_length={"min_value": 1})
    sector: str
    country: str
    currency: str | None = pa.Field(nullable=True)
    weight: float = pa.Field(ge=-1.0, le=1.0)
    shares: float | None = pa.Field(nullable=True)
    market_value: float | None = pa.Field(nullable=True)
    source_id: str = pa.Field(str_length={"min_value": 1})
    raw_file_sha256: str = pa.Field(str_matches=_HEX64_PATTERN)


class Holdings(pa.DataFrameModel):
    """Normalized holdings table: parsed rows plus point-in-time resolution."""

    fund_ticker: str = pa.Field(str_length={"min_value": 1})
    as_of_date: pl.Date
    known_at: pl.Datetime("us", _UTC)  # type: ignore[valid-type]
    file_version: pl.Int32 = pa.Field(ge=1)
    security_id: str | None = pa.Field(nullable=True)
    security_name: str = pa.Field(str_length={"min_value": 1})
    security_ticker: str | None = pa.Field(nullable=True)
    exchange: str | None = pa.Field(nullable=True, str_matches=_MIC_PATTERN)
    isin: str | None = pa.Field(nullable=True, str_length=12)
    cusip: str | None = pa.Field(nullable=True, str_length=9)
    asset_class: str = pa.Field(str_length={"min_value": 1})
    sector: str
    country: str
    currency: str | None = pa.Field(nullable=True)
    weight: float = pa.Field(ge=-1.0, le=1.0)
    shares: float | None = pa.Field(nullable=True)
    market_value: float | None = pa.Field(nullable=True)
    source_id: str = pa.Field(str_length={"min_value": 1})
    raw_file_sha256: str = pa.Field(str_matches=_HEX64_PATTERN)


class FileVersion(pa.DataFrameModel):
    """One stored version of one raw file for one fund and as-of date."""

    source_id: str = pa.Field(str_length={"min_value": 1})
    fund_ticker: str = pa.Field(str_length={"min_value": 1})
    as_of_date: pl.Date
    file_version: pl.Int32 = pa.Field(ge=1)
    known_at: pl.Datetime("us", _UTC)  # type: ignore[valid-type]
    raw_file_sha256: str = pa.Field(str_matches=_HEX64_PATTERN)
    path: str = pa.Field(str_length={"min_value": 1})
    status: str = pa.Field(isin=["included", "excluded"])


class QaEvent(pa.DataFrameModel):
    """A validation finding for one raw file."""

    raw_file_sha256: str = pa.Field(str_matches=_HEX64_PATTERN)
    source_id: str = pa.Field(str_length={"min_value": 1})
    fund_ticker: str = pa.Field(str_length={"min_value": 1})
    as_of_date: pl.Date | None = pa.Field(nullable=True)
    check: str = pa.Field(str_length={"min_value": 1})
    severity: str = pa.Field(isin=["error", "warning", "info"])
    detail: str
    excluded: bool


class HoldingsChange(pa.DataFrameModel):
    """An add, drop or weight change between two consecutive as-of dates."""

    fund_ticker: str = pa.Field(str_length={"min_value": 1})
    from_as_of: pl.Date
    to_as_of: pl.Date
    security_id: str | None = pa.Field(nullable=True)
    change_type: str = pa.Field(isin=["add", "drop", "weight_change"])
    weight_before: float | None = pa.Field(nullable=True, ge=0.0, le=1.0)
    weight_after: float | None = pa.Field(nullable=True, ge=0.0, le=1.0)
    delta: float | None = pa.Field(nullable=True)


class ParsedFundMetrics(pa.DataFrameModel):
    """Parser output: one row per fund and as-of date (D66, interfaces-06 §2).

    A metrics file may carry a whole history (for example a NAV table), so one
    file can produce many rows with different ``as_of_date`` values. Every
    metric is nullable: issuers publish different subsets, and a missing figure
    stays null rather than being filled from another date or defaulted to zero
    (D67). Amounts are in currency units, never thousands or millions.
    """

    fund_ticker: str = pa.Field(str_length={"min_value": 1})
    as_of_date: pl.Date
    nav_per_unit: float | None = pa.Field(nullable=True, gt=0.0)
    shares_outstanding: float | None = pa.Field(nullable=True, ge=0.0)
    aum: float | None = pa.Field(nullable=True, ge=0.0)
    currency: str | None = pa.Field(nullable=True)
    source_id: str = pa.Field(str_length={"min_value": 1})
    raw_file_sha256: str = pa.Field(str_matches=_HEX64_PATTERN)


class FundMetrics(pa.DataFrameModel):
    """Normalized fund-metrics table: parsed rows plus the point-in-time keys.

    ``known_at`` and ``file_version`` follow the same D20 rules as holdings,
    keyed on the metrics source recorded in ``source_id`` (which is not
    necessarily the source of the raw file: ``purpose_metrics`` rows come from
    files collected as ``purpose_daily``).
    """

    fund_ticker: str = pa.Field(str_length={"min_value": 1})
    as_of_date: pl.Date
    known_at: pl.Datetime("us", _UTC)  # type: ignore[valid-type]
    file_version: pl.Int32 = pa.Field(ge=1)
    nav_per_unit: float | None = pa.Field(nullable=True, gt=0.0)
    shares_outstanding: float | None = pa.Field(nullable=True, ge=0.0)
    aum: float | None = pa.Field(nullable=True, ge=0.0)
    currency: str | None = pa.Field(nullable=True)
    source_id: str = pa.Field(str_length={"min_value": 1})
    raw_file_sha256: str = pa.Field(str_matches=_HEX64_PATTERN)


DDL_FILE_VERSIONS = """
CREATE TABLE IF NOT EXISTS file_versions (
    source_id VARCHAR NOT NULL,
    fund_ticker VARCHAR NOT NULL,
    as_of_date DATE NOT NULL,
    file_version INTEGER NOT NULL,
    known_at TIMESTAMPTZ NOT NULL,
    raw_file_sha256 VARCHAR NOT NULL,
    path VARCHAR NOT NULL,
    status VARCHAR NOT NULL
);
"""

DDL_HOLDINGS = """
CREATE TABLE IF NOT EXISTS holdings (
    fund_ticker VARCHAR NOT NULL,
    as_of_date DATE NOT NULL,
    known_at TIMESTAMPTZ NOT NULL,
    file_version INTEGER NOT NULL,
    security_id VARCHAR,
    security_name VARCHAR NOT NULL,
    security_ticker VARCHAR,
    exchange VARCHAR,
    isin VARCHAR,
    cusip VARCHAR,
    asset_class VARCHAR NOT NULL,
    sector VARCHAR NOT NULL,
    country VARCHAR NOT NULL,
    currency VARCHAR,
    weight DOUBLE NOT NULL,
    shares DOUBLE,
    market_value DOUBLE,
    source_id VARCHAR NOT NULL,
    raw_file_sha256 VARCHAR NOT NULL
);
"""

DDL_QA_EVENTS = """
CREATE TABLE IF NOT EXISTS qa_events (
    raw_file_sha256 VARCHAR NOT NULL,
    source_id VARCHAR NOT NULL,
    fund_ticker VARCHAR NOT NULL,
    as_of_date DATE,
    "check" VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,
    detail VARCHAR NOT NULL,
    excluded BOOLEAN NOT NULL
);
"""

DDL_HOLDINGS_CHANGES = """
CREATE TABLE IF NOT EXISTS holdings_changes (
    fund_ticker VARCHAR NOT NULL,
    from_as_of DATE NOT NULL,
    to_as_of DATE NOT NULL,
    security_id VARCHAR,
    change_type VARCHAR NOT NULL,
    weight_before DOUBLE,
    weight_after DOUBLE,
    delta DOUBLE
);
"""

DDL_FUND_METRICS = """
CREATE TABLE IF NOT EXISTS fund_metrics (
    fund_ticker VARCHAR NOT NULL,
    as_of_date DATE NOT NULL,
    known_at TIMESTAMPTZ NOT NULL,
    file_version INTEGER NOT NULL,
    nav_per_unit DOUBLE,
    shares_outstanding DOUBLE,
    aum DOUBLE,
    currency VARCHAR,
    source_id VARCHAR NOT NULL,
    raw_file_sha256 VARCHAR NOT NULL
);
"""
