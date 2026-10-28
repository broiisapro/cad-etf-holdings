"""Command-line interface for cad-etf-holdings.

Commands:

- ``collect --date YYYY-MM-DD|today --raw DIR [--config DIR]`` logs exactly one
  line per fund/source pair and never contacts a source the terms gate refuses.
- ``collect-aum --date YYYY-MM-DD|today --raw DIR [--config DIR]`` collects fund
  metrics (NAV per unit, shares outstanding, AUM) from ``fund_metrics`` sources
  only, through the same terms gate; ``purpose_metrics`` is derived from the
  already-collected ``purpose_daily`` files and prints a ``derived:`` line.
- ``parse --raw DIR --out DIR [--config DIR]`` writes every parseable frame to
  ``<out>/parsed.parquet``.
- ``build --raw DIR --out DIR [--config DIR] [--openfigi [--figi-cache DIR]]``
  rebuilds the DuckDB and exports; ``--openfigi`` assigns cached composite
  FIGI ids to the rows that map.
- ``qa --db PATH [--max-unmatched-share X] [--asof D]
  [--format text|markdown|json]`` prints the QA report and exits 0 only when
  every errored file is excluded and reported; the optional threshold makes it
  exit 3 when an included daily file's unmatched weight share exceeds ``X``;
  ``--asof`` makes every section point-in-time (only versions known by ``D``),
  and ``--format markdown`` prints a publish-safe job-summary report while
  ``--format json`` prints the same numbers for machines (wave-05 review #3,
  interfaces wave 08 §1).
- ``flows --fund F --from D1 --to D2 --db PATH [--asof D] [--json]`` prints
  estimated creation/redemption flows (D67) and always the method line. A fund
  that has holdings but no metrics yet is reported as such (exit 0), never as
  an unknown fund; unknown funds still exit 2.
- ``backfill-qpd --issuer ISSUER --quarters N --raw DIR [--config DIR]``
  fetches historical quarterly disclosures and reports every fund-quarter as
  fetched, present or missing.
- ``review list --db PATH [--confidence exact|fuzzy] [--limit N]`` lists the
  name-match proposals the build wrote next to the database.
- ``review approve --db PATH (--confidence exact|fuzzy | --name NAME ...)
  --reviewer NAME [--overrides PATH] [--dry-run]`` records the human-approved
  proposals as global name overrides (D54); ``--reviewer`` is mandatory.
- ``overlap A B --asof D --db PATH [--json]`` prints pairwise overlap; ``--json``
  prints the same shape as ``tests/fixtures/expected_overlap.json`` with
  four-decimal weight strings.
- ``changes --fund F --from D1 --to D2 --db PATH`` prints recorded changes.
- ``spotcheck --raw DIR --db PATH [--n 5] [--seed 0] [--issuer NAME]`` prints
  sampled raw values next to normalized weights for the latest file per issuer.
- ``export --db PATH --out DIR [--asof D] [--funds F1,F2,...]`` writes derived
  aggregates only (FR-1.12).
- ``weekly --db PATH --out DIR [--week-end D] [--weeks N]
  [--public/--internal] [--demo]`` writes the static weekly digest site
  (FR-3.1): one document per week end, validated, then rendered to ``DIR``.
  ``--demo`` builds the committed synthetic store in a temporary directory and
  ignores ``--db`` (D62).
- ``prices fetch --symbols A,B --from D --to D --raw DIR`` fetches daily
  adjusted closes from the metered, terms-gated source and stores the raw
  responses (D78/D84); the request budget is shared between commands.
- ``event-study --db PATH --raw DIR --out DIR [--source tiingo|synthetic]
  [--benchmark SPY] [--min-weight X] [--window -5,5]
  [--model market_adjusted|market_model]`` measures abnormal returns around
  large additions (FR-3.2) and writes the public aggregates plus the internal
  per-event table.
- ``verify-raw --raw DIR`` / ``sync-raw --from SRC --to DST [--dry-run]``
  check the raw store against its sidecars and copy verified pairs into
  another store (interfaces wave 08 §2).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import duckdb
import polars as pl
import typer

from etfh import qa as qa_module
from etfh.analytics import FundNotFoundError, OverlapResult, funds_available
from etfh.analytics import changes as compute_changes
from etfh.analytics import flows as compute_flows
from etfh.analytics import overlap as compute_overlap
from etfh.backfill import BackfillError, backfill_qpd
from etfh.build import build as run_build
from etfh.build import connect as connect_duckdb
from etfh.collect.registry import CollectError, collector_for
from etfh.digest import build_digest, default_week_end, validate_digest
from etfh.events import large_additions
from etfh.eventstudy import EventStudyResult, run_event_study, write_outputs
from etfh.export import export_aggregates
from etfh.http import PoliteClient
from etfh.parse.registry import find_parser
from etfh.prices import (
    ApiKeyMissingError,
    BudgetExceededError,
    PriceFetchError,
    PriceSourceError,
    SyntheticPrices,
    TiingoPrices,
    symbol_for,
)
from etfh.rawcheck import sync_raw_store, verify_raw_store
from etfh.registry import Fund, Source, TermsNotAllowedError, load_funds, load_sources
from etfh.secmaster.openfigi import FigiLookup
from etfh.secmaster.overrides import load_overrides
from etfh.secmaster.review import append_overrides, approve
from etfh.site import render_site
from etfh.spotcheck import spotcheck as run_spotcheck
from etfh.storage import RawStore
from etfh.types import Decision, SourceKind

__all__ = ["app"]

_TORONTO = ZoneInfo("America/Toronto")
_DEFAULT_FIGI_CACHE = Path.home() / ".cache" / "etfh" / "openfigi"
_DEFAULT_OVERRIDES = Path("config") / "security_overrides.csv"

# D67: every flows output carries this label, never a bare number.
_FLOW_METHOD = "Δshares × NAV (estimate; ignores distributions and in-kind effects)"
# Metrics sources with no collector read files another source already stores
# (interfaces-06 §1: purpose_metrics parses purpose_daily raw files).
_DERIVED_METRICS = {"purpose_metrics": "purpose_daily"}
_QA_FORMATS = ("text", "markdown", "json")

_NAME_PROPOSAL_SCHEMA = {
    "normalized_name": pl.String,
    "example_name": pl.String,
    "country": pl.String,
    "n_funds": pl.Int64,
    "total_weight": pl.Float64,
    "candidate_security_id": pl.String,
    "candidate_name": pl.String,
    "score": pl.Float64,
    "confidence": pl.String,
}
_CONFIDENCE_CLASSES = ("exact", "fuzzy")

_PARSED_SCHEMA = {
    "fund_ticker": pl.String,
    "as_of_date": pl.Date,
    "security_name": pl.String,
    "security_ticker": pl.String,
    "exchange": pl.String,
    "isin": pl.String,
    "cusip": pl.String,
    "asset_class": pl.String,
    "sector": pl.String,
    "country": pl.String,
    "currency": pl.String,
    "weight": pl.Float64,
    "shares": pl.Float64,
    "market_value": pl.Float64,
    "source_id": pl.String,
    "raw_file_sha256": pl.String,
}

app = typer.Typer(
    name="etfh",
    help="Point-in-time Canadian ETF holdings: collect, normalize, validate and analyze.",
    no_args_is_help=True,
)


@app.command()
def collect(
    date_value: Annotated[
        str, typer.Option("--date", help="YYYY-MM-DD or 'today' in America/Toronto")
    ] = "today",
    raw: Annotated[Path, typer.Option("--raw", help="raw store directory")] = Path("raw"),
    config: Annotated[Path, typer.Option("--config", help="config directory")] = Path("config"),
) -> None:
    """Collect raw holdings files from sources whose terms allow collection."""
    day = _parse_date(date_value, "--date")
    sources = load_sources(config / "sources.yaml")
    funds = load_funds(config / "funds.csv", sources)
    store = RawStore(raw)
    with PoliteClient() as client:
        for fund in sorted(funds, key=lambda fund: fund.ticker):
            for source_id in fund.source_ids:
                source = sources.get(source_id)
                # Fund metrics are collected by ``collect-aum`` (D66); the
                # holdings pass must not touch them, exactly as the metrics
                # pass ignores holdings sources.
                if source is not None and _is_metrics_source(source):
                    continue
                _collect_pair(fund, source_id, sources, day, client, store)


@app.command("collect-aum")
def collect_aum(
    date_value: Annotated[
        str, typer.Option("--date", help="YYYY-MM-DD or 'today' in America/Toronto")
    ] = "today",
    raw: Annotated[Path, typer.Option("--raw", help="raw store directory")] = Path("raw"),
    config: Annotated[Path, typer.Option("--config", help="config directory")] = Path("config"),
) -> None:
    """Collect daily fund metrics (NAV, shares outstanding, AUM) only.

    Same gate and bound collectors as ``collect`` (D3): only sources of kind
    ``fund_metrics`` whose terms decision allows collection are contacted.
    ``purpose_metrics`` has no collector; its numbers are derived from the
    ``purpose_daily`` files that ``collect`` already stores, so the command
    reports that derivation instead of fetching anything.
    """
    day = _parse_date(date_value, "--date")
    sources = load_sources(config / "sources.yaml")
    funds = load_funds(config / "funds.csv", sources)
    store = RawStore(raw)
    derived: set[str] = set()
    with PoliteClient() as client:
        for fund in sorted(funds, key=lambda fund: fund.ticker):
            for source_id in fund.source_ids:
                source = sources.get(source_id)
                if source is None or not _is_metrics_source(source):
                    continue
                if not _gate_source(source_id, fund.ticker, source):
                    continue
                origin = _DERIVED_METRICS.get(source_id)
                if origin is not None and collector_for(source) is None:
                    if source_id not in derived:
                        typer.echo(f"derived: {source_id} from {origin}")
                        derived.add(source_id)
                    continue
                _collect_pair(fund, source_id, sources, day, client, store)


@app.command()
def parse(
    raw: Annotated[Path, typer.Option("--raw", help="raw store directory")] = Path("raw"),
    out: Annotated[Path, typer.Option("--out", help="output directory")] = Path("build"),
    config: Annotated[Path, typer.Option("--config", help="config directory")] = Path("config"),
) -> None:
    """Parse raw holdings files into one Parquet frame per parsed file."""
    sources = load_sources(config / "sources.yaml")
    frames: list[pl.DataFrame] = []
    parsed_count = 0
    failures = 0
    for raw_file in RawStore(raw).iter_raw():
        source = sources.get(raw_file.source_id)
        if source is None:
            typer.echo(f"error {raw_file.path}: unknown source {raw_file.source_id!r}")
            failures += 1
            continue
        if source.kind is SourceKind.prices:
            # Price responses share the raw store (D78/D84) but no holdings
            # parser ever claims them; they are not a parse failure.
            continue
        parser = find_parser(raw_file, source)
        if parser is None:
            typer.echo(f"error {raw_file.path}: no parser for .{raw_file.ext} files")
            failures += 1
            continue
        try:
            parsed = parser.parse(raw_file)
        except Exception as error:
            typer.echo(f"error {raw_file.path}: {type(error).__name__}: {error}")
            failures += 1
            continue
        frames.append(parsed.frame)
        parsed_count += 1
        typer.echo(f"parsed {raw_file.path} rows={parsed.frame.height} as_of={parsed.as_of_date}")
    combined = pl.concat(frames, how="vertical") if frames else pl.DataFrame(schema=_PARSED_SCHEMA)
    target = out / "parsed.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    combined.write_parquet(target)
    typer.echo(
        f"wrote {combined.height} rows to {target} ({parsed_count} files parsed, {failures} failed)"
    )


@app.command()
def build(
    raw: Annotated[Path, typer.Option("--raw", help="raw store directory")] = Path("raw"),
    out: Annotated[Path, typer.Option("--out", help="output directory")] = Path("build"),
    config: Annotated[Path, typer.Option("--config", help="config directory")] = Path("config"),
    openfigi: Annotated[
        bool, typer.Option("--openfigi", help="assign cached OpenFIGI composite ids")
    ] = False,
    figi_cache: Annotated[
        Path,
        typer.Option("--figi-cache", help="OpenFIGI cache directory"),
    ] = _DEFAULT_FIGI_CACHE,
) -> None:
    """Rebuild the DuckDB database and Parquet exports from the raw store."""
    figi = None
    if openfigi:
        figi = FigiLookup(figi_cache, api_key=os.environ.get("OPENFIGI_API_KEY"))
    result = run_build(raw, out, config_dir=config, figi=figi)
    typer.echo(f"database {result.db_path}")
    typer.echo(
        f"file versions {result.n_file_versions} "
        f"({result.n_included_files} included, {result.n_excluded_files} excluded)"
    )
    typer.echo(f"holdings {result.n_holdings} rows")
    typer.echo(f"fund metrics {result.n_fund_metrics} rows")
    typer.echo(f"changes {result.n_changes} rows")
    typer.echo(f"qa events {result.n_qa_events}")
    typer.echo(f"unmatched {result.n_unmatched}")
    typer.echo(f"name proposals {result.n_name_proposals}")
    if openfigi:
        typer.echo(f"figi rows {result.n_figi}")


@app.command()
def qa(
    db: Annotated[Path, typer.Option("--db", help="built DuckDB file")],
    max_unmatched_share: Annotated[
        float | None,
        typer.Option(
            "--max-unmatched-share",
            help="exit 3 when a daily file's unmatched weight share exceeds this fraction",
        ),
    ] = None,
    asof: Annotated[
        str | None,
        typer.Option(
            "--asof",
            help="use only versions known by this date (YYYY-MM-DD)",
        ),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option(
            "--format",
            help="text (default), markdown (publish-safe) or json",
        ),
    ] = "text",
) -> None:
    """Report validation results, excluded files and unmatched weight.

    With ``--asof`` every section uses only versions known by that date
    (``summarize(..., asof=...)``, D7/D30). ``--format markdown`` prints a
    publish-safe summary for a workflow job summary (interfaces wave 08 §1);
    ``--format json`` prints the same numbers for machines. Exit codes are
    unchanged: 1 for an inconsistent database, 3 for the unmatched threshold,
    0 otherwise.
    """
    if output_format not in _QA_FORMATS:
        raise typer.BadParameter(
            f"--format must be one of {', '.join(_QA_FORMATS)}", param_hint="--format"
        )
    asof_date = _parse_date(asof, "--asof") if asof is not None else None
    connection = _connect(db)
    try:
        try:
            summary = _qa_summary(
                connection,
                max_unmatched_share=max_unmatched_share,
                asof=asof_date,
            )
        except ValueError as error:
            raise typer.BadParameter(str(error), param_hint="--max-unmatched-share") from None
        code = qa_module.exit_code(summary)
    finally:
        connection.close()
    if output_format == "markdown":
        typer.echo(qa_module.render_markdown(summary))
    elif output_format == "json":
        typer.echo(qa_module.render_json(summary))
    else:
        if asof_date is not None:
            typer.echo(
                f"as of {asof_date.isoformat()}: every section uses only versions "
                "known by the as-of date (America/Toronto cutoff)"
            )
        typer.echo(qa_module.render(summary))
    if code != 0:
        raise typer.Exit(code=code)


@app.command()
def overlap(
    fund_a: Annotated[str, typer.Argument(help="first fund ticker")],
    fund_b: Annotated[str, typer.Argument(help="second fund ticker")],
    asof: Annotated[str, typer.Option("--asof", help="YYYY-MM-DD or 'today'")],
    db: Annotated[Path, typer.Option("--db", help="built DuckDB file")],
    json_output: Annotated[
        bool, typer.Option("--json", help="print machine-readable JSON")
    ] = False,
) -> None:
    """Compute pairwise holdings overlap between two funds as of a date."""
    asof_date = _parse_date(asof, "--asof")
    connection = _connect(db)
    try:
        result = compute_overlap(connection, fund_a, fund_b, asof_date)
    except FundNotFoundError as error:
        typer.echo(f"error: unknown fund {error.fund!r} in {db}", err=True)
        raise typer.Exit(code=2) from None
    finally:
        connection.close()
    if json_output:
        typer.echo(_overlap_json(result))
        return
    typer.echo(
        f"overlap {result.fund_a} / {result.fund_b} as of {result.asof.isoformat()}: "
        f"{result.overlap:.4f} ({result.overlap:.2%})"
    )
    if result.partial:
        typer.echo("top holdings only: one side is a quarterly disclosure")
    typer.echo(f"{result.common.height} common holdings")
    for row in result.common.iter_rows(named=True):
        typer.echo(
            f"  {row['security_id']}  weight_a={row['weight_a']:.4f} weight_b={row['weight_b']:.4f}"
        )


@app.command()
def changes(
    fund: Annotated[str, typer.Option("--fund", help="fund ticker")],
    from_date: Annotated[str, typer.Option("--from", help="YYYY-MM-DD or 'today'")],
    to_date: Annotated[str, typer.Option("--to", help="YYYY-MM-DD or 'today'")],
    db: Annotated[Path, typer.Option("--db", help="built DuckDB file")],
) -> None:
    """List holdings changes for a fund between two dates."""
    start = _parse_date(from_date, "--from")
    end = _parse_date(to_date, "--to")
    connection = _connect(db)
    try:
        frame = compute_changes(connection, fund, start, end)
    finally:
        connection.close()
    typer.echo(
        f"{frame.height} changes for {fund} between {start.isoformat()} and {end.isoformat()}"
    )
    for row in frame.iter_rows(named=True):
        before = "-" if row["weight_before"] is None else f"{row['weight_before']:.4f}"
        after = "-" if row["weight_after"] is None else f"{row['weight_after']:.4f}"
        delta = "-" if row["delta"] is None else f"{row['delta']:+.4f}"
        security = row["security_id"] if row["security_id"] is not None else "(unmatched)"
        typer.echo(
            f"  {row['from_as_of'].isoformat()} -> {row['to_as_of'].isoformat()}  "
            f"{row['change_type']}  {security}  {before} -> {after}  delta={delta}"
        )


@app.command()
def flows(
    fund: Annotated[str, typer.Option("--fund", help="fund ticker")],
    from_date: Annotated[str, typer.Option("--from", help="YYYY-MM-DD or 'today'")],
    to_date: Annotated[str, typer.Option("--to", help="YYYY-MM-DD or 'today'")],
    db: Annotated[Path, typer.Option("--db", help="built DuckDB file")],
    asof: Annotated[
        str | None,
        typer.Option("--asof", help="known-by date (default: --to); YYYY-MM-DD or 'today'"),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="print machine-readable JSON")
    ] = False,
) -> None:
    """Estimate daily creation/redemption flows from shares outstanding and NAV.

    flow = (shares_t - shares_prev) x NAV_t for the same fund and as-of date;
    ``shares_prev`` is the previous as-of date that has a share count, never a
    forward-filled value (D67). Every output carries the method label. A known
    fund with no metrics rows reports ``no fund metrics for <fund> yet`` and
    exits 0; an unknown fund exits 2.
    """
    start = _parse_date(from_date, "--from")
    end = _parse_date(to_date, "--to")
    asof_date = _parse_date(asof, "--asof") if asof is not None else end
    connection = _connect(db)
    try:
        try:
            frame = compute_flows(connection, fund, start, end, asof=asof_date)
        except FundNotFoundError:
            # ``flows`` needs metrics rows; a fund that only has holdings is
            # known to the dataset and simply has no metrics yet (wave-06
            # review #5). Unknown funds still exit 2.
            if not _has_holdings(connection, fund):
                typer.echo(f"error: unknown fund {fund!r} in {db}", err=True)
                raise typer.Exit(code=2) from None
            _report_no_metrics(fund, start, end, asof_date, json_output)
            return
        except duckdb.CatalogException as error:
            # A database built before fund metrics existed has no table at
            # all; the same "no metrics yet" message is the honest answer.
            if "fund_metrics" not in str(error) or not _has_holdings(connection, fund):
                raise
            _report_no_metrics(fund, start, end, asof_date, json_output)
            return
    finally:
        connection.close()
    method = _flow_method(frame)
    if json_output:
        typer.echo(_flows_json(fund, start, end, asof_date, frame, method))
        return
    typer.echo(
        f"flows {fund} {start.isoformat()}..{end.isoformat()} "
        f"as of {asof_date.isoformat()}: {frame.height} row(s)"
    )
    for row in frame.iter_rows(named=True):
        previous = row["prev_as_of_date"]
        previous_text = previous.isoformat() if previous is not None else "-"
        gap = row["gap_days"]
        gap_text = "-" if gap is None else str(gap)
        shares = _display_number(row["shares_outstanding"], 4)
        nav = _display_number(row["nav_per_unit"], 4)
        flow = _display_number(row["flow_estimate"], 2, sign=True)
        typer.echo(
            f"  {row['as_of_date'].isoformat()} prev={previous_text} gap_days={gap_text} "
            f"shares={shares} nav={nav} flow={flow}"
        )
    typer.echo(f"method: {method}")


@app.command("backfill-qpd")
def backfill_qpd_command(
    issuer: Annotated[str, typer.Option("--issuer", help="issuer as recorded in sources.yaml")],
    quarters: Annotated[int, typer.Option("--quarters", help="calendar quarters to request")],
    raw: Annotated[Path, typer.Option("--raw", help="raw store directory")] = Path("raw"),
    config: Annotated[Path, typer.Option("--config", help="config directory")] = Path("config"),
) -> None:
    """Fetch historical quarterly disclosures and report every fund-quarter.

    Only issuers with an approved, supported ``quarterly_disclosure`` source
    are backfilled (D68); each requested fund-quarter is reported as fetched,
    already present, or missing with a reason, and a missing quarter never
    fails the command.
    """
    if quarters < 1:
        typer.echo("error: --quarters must be at least 1", err=True)
        raise typer.Exit(code=2)
    try:
        results = backfill_qpd(issuer, quarters, raw, config_dir=config)
    except BackfillError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=2) from None
    counts = {"fetched": 0, "present": 0, "missing": 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
        path = f" {result.raw.path}" if result.raw is not None else ""
        typer.echo(
            f"{result.status:<7} {result.fund_ticker} "
            f"{result.quarter_end.isoformat()} {result.detail}{path}"
        )
    typer.echo(
        f"backfill {issuer}: {counts['fetched']} fetched, {counts['present']} present, "
        f"{counts['missing']} missing ({len(results)} fund-quarter(s))"
    )


@app.command()
def spotcheck(
    raw: Annotated[Path, typer.Option("--raw", help="raw store directory")] = Path("raw"),
    db: Annotated[Path, typer.Option("--db", help="built DuckDB file")] = Path("build/etfh.duckdb"),
    n: Annotated[int, typer.Option("--n", help="holdings to sample per issuer")] = 5,
    seed: Annotated[int, typer.Option("--seed", help="deterministic sample seed")] = 0,
    issuer: Annotated[str | None, typer.Option("--issuer", help="check only this issuer")] = None,
    config: Annotated[Path, typer.Option("--config", help="config directory")] = Path("config"),
) -> None:
    """Sample holdings against their raw file values for the latest file per issuer."""
    if not Path(db).is_file():
        typer.echo(f"error: database not found: {db}", err=True)
        raise typer.Exit(code=2)
    try:
        report = run_spotcheck(raw, db, n=n, seed=seed, issuer=issuer, config_dir=config)
    except ValueError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(report, nl=False)


@app.command()
def export(
    db: Annotated[Path, typer.Option("--db", help="built DuckDB file")] = Path("build/etfh.duckdb"),
    out: Annotated[Path, typer.Option("--out", help="output directory")] = Path("exports"),
    asof: Annotated[str, typer.Option("--asof", help="YYYY-MM-DD or 'today'")] = "today",
    funds: Annotated[
        str, typer.Option("--funds", help="comma-separated fund tickers (default: all)")
    ] = "",
) -> None:
    """Write derived aggregates (overlap, concentration, sector breakdown)."""
    asof_date = _parse_date(asof, "--asof")
    fund_list = [part.strip() for part in funds.split(",") if part.strip()] or None
    connection = _connect(db)
    try:
        paths = export_aggregates(connection, out, asof=asof_date, funds=fund_list)
    except FundNotFoundError as error:
        typer.echo(f"error: unknown fund {error.fund!r} in {db}", err=True)
        raise typer.Exit(code=2) from None
    finally:
        connection.close()
    for path in paths:
        typer.echo(f"wrote {path}")


_DEMO_SOURCE = "synthetic"
_PRICE_SOURCE_ID = "tiingo_prices"


@app.command()
def weekly(
    db: Annotated[
        Path, typer.Option("--db", help="built DuckDB file (ignored with --demo)")
    ] = Path("build/etfh.duckdb"),
    out: Annotated[Path, typer.Option("--out", help="site output directory")] = Path("site"),
    week_end: Annotated[
        str | None,
        typer.Option(
            "--week-end",
            help="last day of the newest week (YYYY-MM-DD); default: the most recent Friday",
        ),
    ] = None,
    weeks: Annotated[
        int, typer.Option("--weeks", help="how many week ends to publish, newest first")
    ] = 1,
    public: Annotated[
        bool,
        typer.Option(
            "--public/--internal",
            help="apply the D79 publication policy (default) or include security lines",
        ),
    ] = True,
    demo: Annotated[
        bool,
        typer.Option("--demo", help="build the committed synthetic store and ignore --db"),
    ] = False,
    config: Annotated[Path, typer.Option("--config", help="config directory")] = Path("config"),
) -> None:
    """Build the weekly digest documents and render the static site (FR-3.1).

    One document is built per week end (interfaces wave 08 §3), each is checked
    with :func:`etfh.digest.validate_digest` and the set is rendered into ``out``
    (interfaces wave 08 §4). A week whose funds have no comparable portfolio is
    reported and left out of the site; when no requested week has any holding the
    command exits 1.

    ``--demo`` builds the committed synthetic store into a temporary directory
    (D62), ignores ``--db``, marks every document ``"demo": true`` and takes the
    newest week end from the synthetic data's latest as-of date instead of the
    clock. An explicit ``--week-end`` still wins, so older demo weeks can be
    rebuilt. ``--public`` (the default) shows security names, identifiers and
    weights only for funds the terms allow (D79); ``--internal`` includes them
    for every fund and must not be published.
    """
    if weeks < 1:
        typer.echo("error: --weeks must be at least 1", err=True)
        raise typer.Exit(code=2)
    sources = load_sources(config / "sources.yaml")
    funds = load_funds(config / "funds.csv", sources)

    demo_database = None
    if demo:
        from etfh.dashboard.demo import build_demo_database

        try:
            demo_database = build_demo_database(config_dir=config)
        except Exception as error:
            typer.echo(f"error: {error}", err=True)
            raise typer.Exit(code=2) from None
        connection = connect_duckdb(demo_database.db_path, read_only=True)
    else:
        connection = _connect(db)

    try:
        newest: date
        if week_end is not None:
            newest = _parse_date(week_end, "--week-end")
        elif demo_database is not None:
            latest = _synthetic_latest_asof(connection)
            if latest is None:
                typer.echo("error: the demo store has no holdings", err=True)
                raise typer.Exit(code=1)
            newest = latest
        else:
            newest = default_week_end(datetime.now(tz=_TORONTO).date())
        week_ends = [newest - timedelta(days=7 * index) for index in range(weeks)]
        documents = []
        for end in week_ends:
            try:
                document = build_digest(
                    connection, end, public=public, sources=sources, funds=funds
                )
            except duckdb.Error as error:
                typer.echo(f"error: cannot read the database: {error}", err=True)
                raise typer.Exit(code=1) from None
            document["demo"] = demo
            try:
                validate_digest(document)
            except ValueError as error:
                typer.echo(f"error: week ending {end.isoformat()}: {error}", err=True)
                raise typer.Exit(code=1) from None
            if not document["funds"]:
                typer.echo(f"skipped week ending {end.isoformat()}: no comparable portfolio")
                continue
            typer.echo(
                f"week ending {end.isoformat()}: {len(document['funds'])} fund(s), "
                f"{sum(len(fund['changes']) for fund in document['funds'])} change line(s)"
            )
            documents.append(document)
    finally:
        connection.close()
        if demo_database is not None:
            demo_database._tempdir.cleanup()

    if not documents:
        typer.echo(
            f"error: no holdings for the requested week(s) "
            f"{week_ends[-1].isoformat()}..{week_ends[0].isoformat()}",
            err=True,
        )
        raise typer.Exit(code=1)

    written = render_site(documents, out)
    typer.echo(f"wrote {len(written)} file(s) to {Path(out)}")
    typer.echo(f"index {Path(out) / 'index.html'}")
    if not public:
        typer.echo("internal document: security-level lines included, do not publish")


prices_app = typer.Typer(
    help="Fetch and store daily prices for the event study (D78/D84).",
    no_args_is_help=True,
)


@prices_app.command(name="fetch")
def prices_fetch(
    symbols: Annotated[str, typer.Option("--symbols", help="comma-separated ticker symbols")],
    from_date: Annotated[str, typer.Option("--from", help="YYYY-MM-DD")],
    to_date: Annotated[str, typer.Option("--to", help="YYYY-MM-DD")],
    raw: Annotated[Path, typer.Option("--raw", help="raw store directory")] = Path("raw"),
    config: Annotated[Path, typer.Option("--config", help="config directory")] = Path("config"),
    budget: Annotated[
        Path | None,
        typer.Option(
            "--budget",
            help="shared request budget file (default: <raw>/../.etfh/price_budget.json)",
        ),
    ] = None,
) -> None:
    """Fetch daily adjusted closes and store the raw responses.

    The source is ``tiingo_prices``; it is contacted only when its terms
    decision allows it (D3/D84), so a source recorded as ``pending_review``
    prints ``skipped: terms`` and exits 2 exactly like collection. Without
    ``TIINGO_API_KEY`` the command exits 2 with a clear message before any
    request. Responses are cached in the raw store and reused, and the free-plan
    request budget is read and written before every request; the budget file is
    shared with ``etfh event-study`` so the plan limits are honoured across
    commands. ``--raw`` only ever receives price files, never holdings.
    """
    start = _parse_date(from_date, "--from")
    end = _parse_date(to_date, "--to")
    if start > end:
        typer.echo("error: --from must not be after --to", err=True)
        raise typer.Exit(code=2)
    wanted = _parse_symbols(symbols)
    if not wanted:
        typer.echo("error: --symbols must name at least one symbol", err=True)
        raise typer.Exit(code=2)
    sources = load_sources(config / "sources.yaml")
    source = sources.get(_PRICE_SOURCE_ID)
    if not _gate_price_source(source):
        raise typer.Exit(code=2)
    assert source is not None  # narrowed by _gate_price_source
    budget_path = budget if budget is not None else _default_price_budget(raw)
    try:
        prices = TiingoPrices.from_env(raw, sources=sources, budget_path=budget_path)
    except ApiKeyMissingError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=2) from None
    except PriceSourceError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=2) from None
    try:
        with prices:
            frame = prices.daily_closes(wanted, start, end)
    except TermsNotAllowedError as error:
        typer.echo(f"skipped: terms source={_PRICE_SOURCE_ID} ({error})")
        raise typer.Exit(code=2) from None
    except BudgetExceededError as error:
        typer.echo(f"error: {error} ({budget_path})", err=True)
        raise typer.Exit(code=2) from None
    except PriceFetchError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=1) from None
    for symbol in wanted:
        rows = frame.filter(pl.col("symbol") == symbol)
        if rows.is_empty():
            typer.echo(f"{symbol}: no rows")
            continue
        days = rows["date"].to_list()
        typer.echo(f"{symbol}: {rows.height} row(s) {days[0].isoformat()}..{days[-1].isoformat()}")
    typer.echo(
        f"prices: {frame.height} row(s) for {frame['symbol'].n_unique()} of "
        f"{len(wanted)} symbol(s); raw store {Path(raw)}"
    )


@app.command("event-study")
def event_study(
    db: Annotated[Path, typer.Option("--db", help="built DuckDB file")] = Path("build/etfh.duckdb"),
    raw: Annotated[Path, typer.Option("--raw", help="raw store directory")] = Path("raw"),
    out: Annotated[Path, typer.Option("--out", help="output directory")] = Path("event-study"),
    source: Annotated[
        Literal["tiingo", "synthetic"],
        typer.Option("--source", help="price source: the metered real one (default) or offline"),
    ] = "tiingo",
    benchmark: Annotated[str, typer.Option("--benchmark", help="benchmark symbol")] = "SPY",
    min_weight: Annotated[
        float, typer.Option("--min-weight", help="minimum weight for an add/increase")
    ] = 0.01,
    window: Annotated[
        str, typer.Option("--window", help="event window as low,high (e.g. -5,5)")
    ] = "-5,5",
    model: Annotated[
        Literal["market_adjusted", "market_model"],
        typer.Option("--model", help="abnormal-return model"),
    ] = "market_adjusted",
    config: Annotated[Path, typer.Option("--config", help="config directory")] = Path("config"),
    budget: Annotated[
        Path | None,
        typer.Option(
            "--budget",
            help="shared request budget file (default: <raw>/.etfh/price_budget.json)",
        ),
    ] = None,
) -> None:
    """Measure abnormal returns around large additions (FR-3.2, exploratory).

    Events come from :func:`etfh.events.large_additions` and prices from the
    source named by ``--source``; identifiers stay in the internal per-event
    table while the printed summary and the two other output files carry
    aggregates only (D79/D84). ``--source synthetic`` uses deterministic offline
    prices and resolves a US-listed ticker to the security's own ticker on any
    venue, because the committed fixtures are TSX-listed - demo behaviour, and
    the numbers it prints are simulated.
    """
    if not 0.0 < min_weight <= 1.0:
        typer.echo("error: --min-weight must be a fraction in (0, 1]", err=True)
        raise typer.Exit(code=2)
    low, high = _parse_window(window, "--window")
    connection = _connect(db)
    try:
        events = large_additions(connection, min_weight=min_weight)
    except duckdb.Error as error:
        typer.echo(f"error: cannot read events from {db}: {error}", err=True)
        raise typer.Exit(code=1) from None
    finally:
        connection.close()

    prices: SyntheticPrices | TiingoPrices
    resolve_symbol: Callable[[str | None, str | None, str | None], str | None]
    if source == _DEMO_SOURCE:
        typer.echo("price source: synthetic (offline simulation; demo ticker resolution)")
        prices = SyntheticPrices()
        resolve_symbol = _demo_symbol_for
    else:
        sources = load_sources(config / "sources.yaml")
        configured = sources.get(_PRICE_SOURCE_ID)
        if not _gate_price_source(configured):
            raise typer.Exit(code=2)
        budget_path = budget if budget is not None else _default_price_budget(raw)
        try:
            prices = TiingoPrices.from_env(raw, sources=sources, budget_path=budget_path)
        except (ApiKeyMissingError, PriceSourceError) as error:
            typer.echo(f"error: {error}", err=True)
            raise typer.Exit(code=2) from None
        typer.echo(f"price source: {_PRICE_SOURCE_ID} (budget {budget_path})")
        resolve_symbol = symbol_for

    try:
        with nullcontext(prices) as price_source:
            result = run_event_study(
                events,
                price_source,
                symbol_for=resolve_symbol,
                benchmark=benchmark,
                window=(low, high),
                model=model,
            )
    except (TermsNotAllowedError, BudgetExceededError, PriceSourceError) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=2) from None
    except PriceFetchError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=1) from None
    except ValueError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=2) from None

    written = write_outputs(result, out)
    typer.echo(_event_study_report(result))
    if events.is_empty():
        typer.echo(f"no large additions at min-weight {min_weight} (nothing to measure)")
    for path in written:
        typer.echo(f"wrote {path}")
    typer.echo("results are exploratory; not investment advice")


@app.command("verify-raw")
def verify_raw(
    raw: Annotated[Path, typer.Option("--raw", help="raw store directory")] = Path("raw"),
) -> None:
    """Check every raw file against its sidecar (interfaces wave 08 §2).

    Every problem is reported, one line each, and the command exits 1 when there
    is at least one; a missing directory is a usage error and exits 2.
    """
    try:
        report = verify_raw_store(raw)
    except OSError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(
        f"verify {raw}: {report.files_checked} file(s) checked, {len(report.problems)} problem(s)"
    )
    for problem in report.problems:
        typer.echo(f"{problem.kind}: {problem.path}: {problem.detail}")
    if not report.ok:
        raise typer.Exit(code=1)


@app.command("sync-raw")
def sync_raw(
    source: Annotated[Path, typer.Option("--from", help="source raw store")],
    destination: Annotated[Path, typer.Option("--to", help="destination raw store")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="report what would be copied and write nothing")
    ] = False,
) -> None:
    """Copy verified raw data-file/sidecar pairs into another store.

    A pair is written both or neither, identical pairs are skipped and a
    destination path holding different bytes is a conflict that is never
    overwritten (interfaces wave 08 §2). Exit 0 when there is no conflict, 1
    when at least one path conflicts or the source does not verify clean.
    """
    try:
        report = sync_raw_store(source, destination, dry_run=dry_run)
    except (OSError, ValueError) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        f"sync {source} -> {destination}: {len(report.copied)} copied, "
        f"{report.identical} identical, {len(report.conflicts)} conflict(s)"
    )
    for path in report.conflicts:
        typer.echo(f"conflict: {path}: destination holds different bytes, not overwritten")
    if dry_run:
        typer.echo("dry run: nothing written")
    if not report.ok:
        raise typer.Exit(code=1)


app.add_typer(prices_app, name="prices")

review_app = typer.Typer(
    help="Review name-match proposals and record approved overrides.",
    no_args_is_help=True,
)


@review_app.command(name="list")
def review_list(
    db: Annotated[Path, typer.Option("--db", help="built DuckDB file")],
    confidence: Annotated[
        Literal["exact", "fuzzy"] | None,
        typer.Option("--confidence", help="only this confidence class"),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="print at most this many proposals")
    ] = None,
) -> None:
    """List the name-match proposals written next to the built database."""
    if limit is not None and limit < 0:
        raise typer.BadParameter("must be >= 0", param_hint="--limit")
    proposals_path = _name_proposals_path(db)
    proposals = _load_proposals(proposals_path)
    total = proposals.height
    if confidence is not None:
        proposals = proposals.filter(pl.col("confidence") == confidence)
    selected = proposals.height
    if limit is not None:
        proposals = proposals.head(limit)
    typer.echo(f"name proposals {proposals_path}")
    typer.echo(f"{total} proposal(s); {selected} selected; showing {proposals.height}")
    for row in proposals.iter_rows(named=True):
        country = row["country"] if row["country"] is not None else "-"
        typer.echo(
            f"  {row['confidence']:<5} {row['score']:5.1f}  n_funds={row['n_funds']} "
            f"weight={row['total_weight']:.2%} country={country}  {row['normalized_name']}"
            f" -> {row['candidate_security_id']}"
        )


@review_app.command(name="approve")
def review_approve(
    db: Annotated[Path, typer.Option("--db", help="built DuckDB file")],
    reviewer: Annotated[str, typer.Option("--reviewer", help="who reviewed the matches")],
    confidence: Annotated[
        Literal["exact", "fuzzy"] | None,
        typer.Option("--confidence", help="approve every proposal of this confidence class"),
    ] = None,
    names: Annotated[
        list[str] | None,
        typer.Option("--name", help="approve one normalized name; repeatable"),
    ] = None,
    overrides: Annotated[
        Path, typer.Option("--overrides", help="reviewed overrides CSV to append to")
    ] = _DEFAULT_OVERRIDES,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="print what would be added and write nothing")
    ] = False,
) -> None:
    """Record approved name proposals as global name overrides (D54).

    Approval is always explicit: either a whole confidence class
    (--confidence exact) or one or more normalized names (--name). Proposals
    are never applied by the build itself.
    """
    reviewer = reviewer.strip()
    if not reviewer:
        typer.echo("error: --reviewer is required and must not be empty", err=True)
        raise typer.Exit(code=2)
    wanted = tuple(name.strip() for name in (names or []) if name.strip())
    if confidence is None and not wanted:
        typer.echo(
            "error: pass --confidence exact|fuzzy or at least one --name to approve",
            err=True,
        )
        raise typer.Exit(code=2)
    proposals_path = _name_proposals_path(db)
    proposals = _load_proposals(proposals_path)
    unknown = [name for name in wanted if name not in set(proposals["normalized_name"].to_list())]
    if unknown:
        typer.echo(
            f"error: no proposal for {', '.join(repr(name) for name in unknown)} "
            f"in {proposals_path}",
            err=True,
        )
        raise typer.Exit(code=2)
    reviewed_on = datetime.now(tz=_TORONTO).date()
    try:
        approved = approve(
            proposals,
            confidence=confidence,
            names=wanted,
            reviewer=reviewer,
            reviewed_on=reviewed_on,
        )
    except ValueError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=2) from None
    existing = _existing_overrides(overrides)
    conflicts = [
        override
        for override in approved
        if (override.match_type, override.match_value) in existing
        and existing[(override.match_type, override.match_value)] != override.security_id
    ]
    if conflicts:
        typer.echo(
            "error: a selected proposal was already reviewed as a different security id: "
            + ", ".join(
                f"{override.match_value} -> {existing[(override.match_type, override.match_value)]}"
                for override in conflicts
            ),
            err=True,
        )
        raise typer.Exit(code=2)
    new = [
        override
        for override in approved
        if (override.match_type, override.match_value) not in existing
    ]
    if not approved:
        typer.echo("nothing to approve: the selection matches no proposal")
        return
    if dry_run:
        typer.echo(
            f"dry run: would add {len(new)} of {len(approved)} name override(s) "
            f"for reviewer={reviewer!r} reviewed_on={reviewed_on.isoformat()}"
        )
        for override in new:
            typer.echo(f"  {override.match_type} {override.match_value} -> {override.security_id}")
        typer.echo(f"dry run: nothing written to {overrides}")
        return
    try:
        added = append_overrides(overrides, approved)
    except ValueError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(
        f"approved {added} name override(s) for reviewer={reviewer!r} "
        f"reviewed_on={reviewed_on.isoformat()} in {overrides}"
    )
    for override in new:
        typer.echo(f"  {override.match_type} {override.match_value} -> {override.security_id}")
    if added < len(approved):
        typer.echo(f"{len(approved) - added} selected proposal(s) already had an override")


app.add_typer(review_app, name="review")


def _collect_pair(
    fund: Fund,
    source_id: str,
    sources: Mapping[str, Source],
    day: date,
    client: PoliteClient,
    store: RawStore,
) -> None:
    source = sources.get(source_id)
    if not _gate_source(source_id, fund.ticker, source):
        return
    assert source is not None  # narrowed by _gate_source
    collector = collector_for(source)
    if collector is None:
        typer.echo(f"error source={source_id} fund={fund.ticker}: no collector registered")
        return
    try:
        files = collector.fetch(fund, day, client, store)
    except CollectError as error:
        typer.echo(f"error source={source_id} fund={fund.ticker}: {error.reason}")
        return
    except TermsNotAllowedError as error:
        typer.echo(f"skipped: terms source={source_id} fund={fund.ticker} ({error})")
        return
    if not files:
        typer.echo(f"unchanged source={source_id} fund={fund.ticker}")
    elif len(files) == 1:
        typer.echo(f"fetched source={source_id} fund={fund.ticker} {files[0].path}")
    else:
        typer.echo(f"fetched source={source_id} fund={fund.ticker} {len(files)} files")


def _gate_source(source_id: str, fund_ticker: str, source: Source | None) -> bool:
    """Apply the terms/unsupported/synthetic gate and log the refusal line.

    Returns True only when ``source`` may be contacted. The same lines are used
    by ``collect`` and ``collect-aum`` so a refused source never reaches a
    collector lookup (D3).
    """
    if source is None:
        typer.echo(f"error source={source_id} fund={fund_ticker}: unknown source")
        return False
    if source.terms.decision not in (Decision.allowed, Decision.allowed_with_conditions):
        typer.echo(
            f"skipped: terms source={source_id} fund={fund_ticker} "
            f"decision={source.terms.decision.value}"
        )
        return False
    if not source.supported:
        typer.echo(f"skipped: unsupported source={source_id} fund={fund_ticker}")
        return False
    if source.kind is SourceKind.synthetic:
        typer.echo(f"skipped: synthetic source={source_id} fund={fund_ticker}")
        return False
    return True


def _is_metrics_source(source: Source) -> bool:
    """True for ``SourceKind.fund_metrics`` (D66)."""
    return source.kind is SourceKind.fund_metrics


def _synthetic_latest_asof(con: duckdb.DuckDBPyConnection) -> date | None:
    """The newest as-of date the built store holds, or None when it holds none.

    ``weekly --demo`` uses it as its newest week end: the committed fixtures
    are dated in 2026, so a clock-based week would be empty until that date.
    """
    try:
        row = con.execute("SELECT max(as_of_date) FROM holdings").fetchone()
    except duckdb.Error:
        return None
    if row is None or row[0] is None:
        return None
    latest: date = row[0]
    return latest


def _parse_symbols(value: str) -> list[str]:
    """Split ``--symbols A,B`` into unique upper-case symbols, keeping order."""
    seen: dict[str, None] = {}
    for part in value.split(","):
        symbol = part.strip().upper()
        if symbol:
            seen.setdefault(symbol, None)
    return list(seen)


def _gate_price_source(source: Source | None) -> bool:
    """Apply the terms/unsupported/synthetic gate to ``tiingo_prices`` (D3).

    Mirrors :func:`_gate_source` line for line, but a price fetch is keyed by a
    ticker symbol rather than a fund in ``funds.csv``, so the label names the
    source only. Returns True only when the source may be contacted.
    """
    label = f"source={_PRICE_SOURCE_ID}"
    if source is None:
        typer.echo(f"error {label}: unknown source", err=True)
        return False
    if source.terms.decision not in (Decision.allowed, Decision.allowed_with_conditions):
        typer.echo(f"skipped: terms {label} decision={source.terms.decision.value}")
        return False
    if not source.supported:
        typer.echo(f"skipped: unsupported {label}")
        return False
    if source.kind is SourceKind.synthetic:
        typer.echo(f"skipped: synthetic {label}")
        return False
    return True


def _default_price_budget(raw: Path) -> Path:
    """The budget file shared by ``prices fetch`` and ``event-study``.

    ``<raw>/../.etfh/price_budget.json``: next to the raw store but never inside
    it, because it is command state and not a collected file, and the raw store
    is synced file-for-file to the data repository (D77).
    """
    return Path(raw).expanduser().absolute().parent / ".etfh" / "price_budget.json"


def _demo_symbol_for(
    security_id: str | None,
    security_ticker: str | None,
    exchange: str | None,
) -> str | None:
    """Demo ticker resolution for ``--source synthetic``: the security's ticker.

    The committed fixtures are TSX-listed, so the real
    :func:`etfh.prices.symbol_for` (US venues only) would skip every synthetic
    event as ``no_symbol``. The offline demo therefore prices the security's own
    ticker whatever the venue, which is what makes the synthetic end-to-end run
    land on day 0. It is demo behaviour, never used for the real source.
    """
    del security_id, exchange
    if security_ticker is None:
        return None
    symbol = security_ticker.strip().upper().replace(".", "-")
    return symbol or None


def _parse_window(value: str, option: str) -> tuple[int, int]:
    """Parse ``--window low,high`` into a pair of relative days."""
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        raise typer.BadParameter(f"{value!r} is not 'low,high' (e.g. -5,5)", param_hint=option)
    try:
        low, high = int(parts[0]), int(parts[1])
    except ValueError:
        raise typer.BadParameter(
            f"{value!r} is not 'low,high' with integer days", param_hint=option
        ) from None
    if low > high:
        raise typer.BadParameter(
            f"window must be ordered low..high, got {low},{high}", param_hint=option
        )
    return low, high


def _event_study_report(result: EventStudyResult) -> str:
    """The public summary as text: aggregates only, no fund or security identity."""
    summary = result.to_public_json()
    skipped = summary.get("skipped", {})
    skipped_text = ", ".join(f"{reason}={count}" for reason, count in sorted(skipped.items()))
    bootstrap = summary.get("bootstrap", {})
    window = summary.get("window", ())
    lines = [
        "event study — exploratory aggregate statistics, not investment advice",
        (
            f"events {summary.get('n_events', 0)}: used {summary.get('n_used', 0)}"
            + (f"; skipped {skipped_text}" if skipped_text else "")
        ),
        (
            f"model {summary.get('model')} · benchmark {summary.get('benchmark')} · "
            f"window {list(window)} · bootstrap {bootstrap.get('resamples')} resamples "
            f"({bootstrap.get('method')}, {bootstrap.get('level')})"
        ),
    ]
    windows = summary.get("windows", {})
    for label, stats in windows.items():
        mean_car = stats.get("mean_car")
        if mean_car is None:
            lines.append(f"  CAR{label}: not estimable (n={stats.get('n', 0)})")
            continue
        t_stat = stats.get("t_stat")
        low = stats.get("ci_low")
        high = stats.get("ci_high")
        ci = "n/a" if low is None or high is None else f"[{low:+.4f}, {high:+.4f}]"
        t_text = "n/a" if t_stat is None else f"{t_stat:+.2f}"
        lines.append(
            f"  CAR{label}: mean {mean_car:+.4f} t={t_text} "
            f"bootstrap 95% CI {ci} n={stats.get('n', 0)}"
        )
    return "\n".join(lines)


def _qa_summary(
    con: duckdb.DuckDBPyConnection,
    *,
    max_unmatched_share: float | None,
    asof: date | None,
) -> qa_module.QaSummary:
    """Build the QA summary, optionally point-in-time at ``asof`` (D7/D30).

    ``qa.summarize`` owns the whole aggregation, including the ``--asof``
    filtering of every section, so the CLI only wires options and formats.
    """
    return qa_module.summarize(con, max_unmatched_share=max_unmatched_share, asof=asof)


def _has_holdings(con: duckdb.DuckDBPyConnection, fund: str) -> bool:
    """True when the built database carries holdings rows for ``fund``.

    A fund with holdings but no fund metrics is a known fund whose metrics have
    not been collected yet; only a fund absent from the holdings table is
    unknown. A database without a holdings table answers False.
    """
    try:
        return fund in funds_available(con)
    except duckdb.Error:
        return False


def _report_no_metrics(fund: str, start: date, end: date, asof: date, json_output: bool) -> None:
    """Explain that a known fund has no metrics yet, keeping JSON valid."""
    if json_output:
        typer.echo(_no_metrics_json(fund, start, end, asof))
        return
    typer.echo(f"no fund metrics for {fund} yet")
    typer.echo(f"method: {_FLOW_METHOD}")


def _no_metrics_json(fund: str, start: date, end: date, asof: date) -> str:
    """Empty flow payload for a fund without metrics, in the ``--json`` shape."""
    payload = {
        "fund": fund,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "asof": asof.isoformat(),
        "method": _FLOW_METHOD,
        "rows": [],
        "message": f"no fund metrics for {fund} yet",
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _flow_method(frame: pl.DataFrame) -> str:
    """The D67 label the rows carry, falling back to the contract string."""
    if frame.height and "method" in frame.columns:
        methods = {str(value) for value in frame["method"].to_list() if value is not None}
        if len(methods) == 1:
            return methods.pop()
    return _FLOW_METHOD


def _flows_json(
    fund: str,
    start: date,
    end: date,
    asof: date,
    frame: pl.DataFrame,
    method: str,
) -> str:
    rows = []
    for row in frame.iter_rows(named=True):
        previous = row["prev_as_of_date"]
        rows.append(
            {
                "as_of_date": row["as_of_date"].isoformat(),
                "shares_outstanding": _decimal_or_none(row["shares_outstanding"], 4),
                "nav_per_unit": _decimal_or_none(row["nav_per_unit"], 4),
                "prev_as_of_date": previous.isoformat() if previous is not None else None,
                "prev_shares": _decimal_or_none(row["prev_shares"], 4),
                "gap_days": row["gap_days"],
                "flow_estimate": _decimal_or_none(row["flow_estimate"], 2),
            }
        )
    payload = {
        "fund": fund,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "asof": asof.isoformat(),
        "method": method,
        "rows": rows,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _decimal_or_none(value: float | None, places: int) -> str | None:
    return None if value is None else f"{float(value):.{places}f}"


def _display_number(value: float | None, places: int, *, sign: bool = False) -> str:
    if value is None:
        return "-"
    flag = "+" if sign else ""
    return f"{float(value):{flag},.{places}f}"


def _overlap_json(result: OverlapResult) -> str:
    payload = {
        "fund_a": result.fund_a,
        "fund_b": result.fund_b,
        "asof": result.asof.isoformat(),
        "overlap": f"{result.overlap:.4f}",
        "common_count": result.common.height,
        "common": [
            {
                "security_id": row["security_id"],
                "weight_a": f"{row['weight_a']:.4f}",
                "weight_b": f"{row['weight_b']:.4f}",
            }
            for row in result.common.iter_rows(named=True)
        ],
    }
    return json.dumps(payload, indent=2)


def _name_proposals_path(db: Path) -> Path:
    """The proposals report that ``etfh build`` writes next to the database."""
    if not Path(db).is_file():
        typer.echo(f"error: database not found: {db}", err=True)
        raise typer.Exit(code=2)
    path = Path(db).parent / "reports" / "name_proposals.csv"
    if not path.is_file():
        typer.echo(
            f"error: name proposals not found: {path} (run etfh build --out {Path(db).parent})",
            err=True,
        )
        raise typer.Exit(code=2)
    return path


def _load_proposals(path: Path) -> pl.DataFrame:
    """Read the proposals report, failing clearly on an unexpected shape."""
    try:
        frame = pl.read_csv(path, schema=_NAME_PROPOSAL_SCHEMA)
    except pl.exceptions.PolarsError as error:
        typer.echo(f"error: cannot read name proposals {path}: {error}", err=True)
        raise typer.Exit(code=2) from None
    unknown = sorted(set(frame["confidence"].to_list()) - set(_CONFIDENCE_CLASSES))
    if unknown:
        typer.echo(
            f"error: {path} has unknown confidence value(s): {', '.join(map(str, unknown))}",
            err=True,
        )
        raise typer.Exit(code=2)
    return frame


def _existing_overrides(path: Path) -> dict[tuple[str, str], str]:
    """``(match_type, match_value) -> security_id`` already recorded in ``path``."""
    if not Path(path).is_file():
        return {}
    return {
        (override.match_type, override.match_value): override.security_id
        for override in load_overrides(path)
    }


def _parse_date(value: str, option: str) -> date:
    if value == "today":
        return datetime.now(tz=_TORONTO).date()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(
            f"{value!r} is not a date in YYYY-MM-DD form or 'today'", param_hint=option
        ) from None


def _connect(db: Path) -> duckdb.DuckDBPyConnection:
    if not Path(db).is_file():
        typer.echo(f"error: database not found: {db}", err=True)
        raise typer.Exit(code=2)
    return connect_duckdb(db, read_only=True)
