"""Derived-aggregates export (FR-1.12, D9).

Only aggregates leave this module: pairwise fund overlap, concentration
metrics and sector breakdowns. No holdings rows, security names, identifiers
or individual security weights are ever written, so the output can be shared
even when a source's terms forbid redistributing its holdings.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import duckdb
import polars as pl
from polars.datatypes import DataType, DataTypeClass

from etfh import analytics

__all__ = ["export_aggregates"]

_CONCENTRATION_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "fund": pl.String,
    "as_of_date": pl.Date,
    "n_holdings": pl.Int64,
    "top10_weight": pl.Float64,
    "hhi": pl.Float64,
    "partial": pl.Boolean,
}

_BREAKDOWN_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "fund": pl.String,
    "as_of_date": pl.Date,
    "sector": pl.String,
    "weight": pl.Float64,
}


def export_aggregates(
    con: duckdb.DuckDBPyConnection,
    out_dir: Path,
    *,
    asof: date,
    funds: Sequence[str] | None = None,
) -> list[Path]:
    """Write ``overlap_matrix.csv``, ``concentration.csv`` and ``sector_breakdown.csv``.

    ``funds`` defaults to every fund in the database; the input order is kept
    in the overlap matrix. The sector breakdown carries only the fund, the
    selected as-of date, the sector key and the sector's summed weight.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fund_list = list(funds) if funds is not None else analytics.funds_available(con)
    matrix = analytics.overlap_matrix(con, fund_list, asof)
    matrix_path = out_dir / "overlap_matrix.csv"
    matrix.write_csv(matrix_path)
    concentration_rows: list[dict[str, object]] = []
    breakdown_rows: list[dict[str, object]] = []
    for fund in fund_list:
        metrics = analytics.concentration(con, fund, asof)
        concentration_rows.append(
            {
                "fund": metrics.fund,
                "as_of_date": metrics.as_of_date,
                "n_holdings": metrics.n_holdings,
                "top10_weight": metrics.top10_weight,
                "hhi": metrics.hhi,
                "partial": metrics.partial,
            }
        )
        for row in analytics.breakdown(con, fund, asof, "sector").iter_rows(named=True):
            breakdown_rows.append(
                {
                    "fund": fund,
                    "as_of_date": metrics.as_of_date,
                    "sector": row["key"],
                    "weight": row["weight"],
                }
            )
    concentration = pl.DataFrame(concentration_rows, schema=_CONCENTRATION_SCHEMA)
    concentration_path = out_dir / "concentration.csv"
    concentration.write_csv(concentration_path)
    breakdown = pl.DataFrame(breakdown_rows, schema=_BREAKDOWN_SCHEMA)
    if breakdown.height:
        breakdown = breakdown.sort(["fund", "weight"], descending=[False, True])
    breakdown_path = out_dir / "sector_breakdown.csv"
    breakdown.write_csv(breakdown_path)
    return [matrix_path, concentration_path, breakdown_path]
