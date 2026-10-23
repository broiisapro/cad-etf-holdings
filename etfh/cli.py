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
- ``qa --db PATH [--max-unmatched-share X] [--asof D]`` prints the QA report
  and exits 0 only when every errored file is excluded and reported; the
  optional threshold makes it exit 3 when an included daily file's unmatched
  weight share exceeds ``X``, and ``--asof`` measures those shares on the
  versions known by ``D`` (wave-05 review #3).
- ``flows --fund F --from D1 --to D2 --db PATH [--asof D] [--json]`` prints
  estimated creation/redemption flows (D67) and always the method line.
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
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from statistics import median
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import duckdb
import polars as pl
import typer

from etfh import qa as qa_module
from etfh.analytics import FundNotFoundError, OverlapResult
from etfh.analytics import changes as compute_changes
from etfh.analytics import flows as compute_flows
from etfh.analytics import overlap as compute_overlap
from etfh.backfill import BackfillError, backfill_qpd
from etfh.build import build as run_build
from etfh.build import connect as connect_duckdb
from etfh.collect.registry import CollectError, collector_for
from etfh.export import export_aggregates
from etfh.http import PoliteClient
from etfh.parse.registry import find_parser
from etfh.registry import Fund, Source, TermsNotAllowedError, load_funds, load_sources
from etfh.secmaster.openfigi import FigiLookup
from etfh.secmaster.overrides import load_overrides
from etfh.secmaster.review import append_overrides, approve
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
_QA_QUALITY_KINDS = ("daily", "quarterly")
_QUARTERLY_KIND = "quarterly_disclosure"
_WORST_FUNDS = 10

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
            help="measure unmatched weight on versions known by this date (YYYY-MM-DD)",
        ),
    ] = None,
) -> None:
    """Report validation results, excluded files and unmatched weight.

    With ``--asof`` the unmatched-weight section describes the versions a
    point-in-time query at that date would use (``unmatched_shares(con, asof)``,
    wave-05 review #3); findings and file counts still cover the whole store.
    """
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
    if asof_date is not None:
        typer.echo(
            f"as of {asof_date.isoformat()}: unmatched weight uses only versions "
            "known by then (America/Toronto cutoff)"
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
    forward-filled value (D67). Every output carries the method label.
    """
    start = _parse_date(from_date, "--from")
    end = _parse_date(to_date, "--to")
    asof_date = _parse_date(asof, "--asof") if asof is not None else end
    connection = _connect(db)
    try:
        frame = compute_flows(connection, fund, start, end, asof=asof_date)
    except FundNotFoundError as error:
        typer.echo(f"error: unknown fund {error.fund!r} in {db}", err=True)
        raise typer.Exit(code=2) from None
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


def _qa_summary(
    con: duckdb.DuckDBPyConnection,
    *,
    max_unmatched_share: float | None,
    asof: date | None,
) -> qa_module.QaSummary:
    """Build the QA summary, optionally measuring unmatched weight as of a date.

    ``qa.summarize`` reads every included file; the ``--asof`` path replaces
    only its unmatched-weight section with :func:`etfh.qa.unmatched_shares` at
    that date, keeping the file findings whole-store (they describe the store,
    not a point in time). The grouping mirrors the report's daily/quarterly
    split.
    """
    summary = qa_module.summarize(con, max_unmatched_share=max_unmatched_share)
    if asof is None or summary.tables_missing:
        return summary
    frame = qa_module.unmatched_shares(con, asof)
    shares = tuple(
        qa_module.UnmatchedShare(
            fund_ticker=str(row["fund_ticker"]),
            as_of_date=row["as_of_date"],
            source_id=str(row["source_id"]),
            unmatched_weight=float(row["unmatched_weight"]),
            total_weight=float(row["total_weight"]),
            share=float(row["share"]),
        )
        for row in frame.iter_rows(named=True)
    )
    metadata = _qa_source_metadata(con)
    shares_by_issuer: dict[str, dict[str, list[float]]] = {kind: {} for kind in _QA_QUALITY_KINDS}
    worst_by_fund: dict[str, dict[str, qa_module.UnmatchedShare]] = {
        kind: {} for kind in _QA_QUALITY_KINDS
    }
    for row in shares:
        issuer, quality = metadata.get(row.source_id, (row.source_id, "daily"))
        shares_by_issuer[quality].setdefault(issuer, []).append(row.share)
        current = worst_by_fund[quality].get(row.fund_ticker)
        if current is None or (row.share, row.as_of_date, row.source_id) > (
            current.share,
            current.as_of_date,
            current.source_id,
        ):
            worst_by_fund[quality][row.fund_ticker] = row
    quality_summaries = []
    for quality in _QA_QUALITY_KINDS:
        quality_summaries.append(
            qa_module.UnmatchedQuality(
                kind=quality,
                issuer_medians=tuple(
                    (issuer, float(median(values)))
                    for issuer, values in sorted(shares_by_issuer[quality].items())
                ),
                worst_funds=tuple(
                    sorted(
                        (row for row in worst_by_fund[quality].values() if row.share > 0),
                        key=lambda row: (
                            -row.share,
                            row.fund_ticker,
                            row.as_of_date.isoformat(),
                            row.source_id,
                        ),
                    )[:_WORST_FUNDS]
                ),
            )
        )
    over_threshold: tuple[qa_module.UnmatchedShare, ...] = ()
    if max_unmatched_share is not None:
        over_threshold = tuple(
            row
            for row in shares
            if row.share > max_unmatched_share
            and metadata.get(row.source_id, (row.source_id, "daily"))[1] == "daily"
        )
    return dataclasses.replace(
        summary,
        unmatched_shares=shares,
        unmatched_quality=tuple(quality_summaries),
        unmatched_over_threshold=over_threshold,
    )


def _qa_source_metadata(con: duckdb.DuckDBPyConnection) -> dict[str, tuple[str, str]]:
    """``source_id -> (issuer, 'daily'|'quarterly')`` from the additive ``sources``."""
    rows: list[tuple[object, ...]] | None = None
    for query in (
        "SELECT source_id, issuer, kind, format FROM sources",
        "SELECT source_id, issuer, kind, NULL AS format FROM sources",
    ):
        try:
            rows = con.execute(query).fetchall()
        except duckdb.Error:
            continue
        break
    if rows is None:
        return {}
    metadata: dict[str, tuple[str, str]] = {}
    for source_id, issuer, kind, source_format in rows:
        quarterly = str(kind) == _QUARTERLY_KIND or str(source_format) == "pdf"
        metadata[str(source_id)] = (
            str(issuer) if issuer is not None else str(source_id),
            "quarterly" if quarterly else "daily",
        )
    return metadata


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
