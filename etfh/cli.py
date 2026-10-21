"""Command-line interface for cad-etf-holdings.

Commands:

- ``collect --date YYYY-MM-DD|today --raw DIR [--config DIR]`` logs exactly one
  line per fund/source pair and never contacts a source the terms gate refuses.
- ``parse --raw DIR --out DIR [--config DIR]`` writes every parseable frame to
  ``<out>/parsed.parquet``.
- ``build --raw DIR --out DIR [--config DIR] [--openfigi [--figi-cache DIR]]``
  rebuilds the DuckDB and exports; ``--openfigi`` assigns cached composite
  FIGI ids to the rows that map.
- ``qa --db PATH [--max-unmatched-share X]`` prints the QA report and exits 0
  only when every errored file is excluded and reported; the optional
  threshold makes it exit 3 when an included daily file's unmatched weight
  share exceeds ``X``.
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

import importlib
import json
import os
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal, Protocol, cast
from zoneinfo import ZoneInfo

import duckdb
import polars as pl
import typer

from etfh import qa as qa_module
from etfh.analytics import FundNotFoundError, OverlapResult
from etfh.analytics import changes as compute_changes
from etfh.analytics import overlap as compute_overlap
from etfh.build import build as run_build
from etfh.build import connect as connect_duckdb
from etfh.collect.registry import CollectError, collector_for
from etfh.export import export_aggregates
from etfh.http import PoliteClient
from etfh.parse.registry import find_parser
from etfh.registry import Fund, Source, TermsNotAllowedError, load_funds, load_sources
from etfh.secmaster.openfigi import FigiLookup
from etfh.secmaster.overrides import Override, load_overrides
from etfh.spotcheck import spotcheck as run_spotcheck
from etfh.storage import RawStore
from etfh.types import Decision, SourceKind

__all__ = ["app"]

_TORONTO = ZoneInfo("America/Toronto")
_DEFAULT_FIGI_CACHE = Path.home() / ".cache" / "etfh" / "openfigi"
_DEFAULT_OVERRIDES = Path("config") / "security_overrides.csv"

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
    typer.echo(f"changes {result.n_changes} rows")
    typer.echo(f"qa events {result.n_qa_events}")
    typer.echo(f"unmatched {result.n_unmatched}")
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
) -> None:
    """Report validation results, excluded files and unmatched weight."""
    connection = _connect(db)
    try:
        try:
            summary = qa_module.summarize(connection, max_unmatched_share=max_unmatched_share)
        except ValueError as error:
            raise typer.BadParameter(str(error), param_hint="--max-unmatched-share") from None
        code = qa_module.exit_code(summary)
    finally:
        connection.close()
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


class _ReviewApi(Protocol):
    """The reviewed-name functions the CLI needs (``etfh.secmaster.review``).

    Declared as a protocol so the CLI can be tested against a contract fake
    while the module is still being written, and so mypy checks the calls the
    CLI makes.
    """

    def approve(
        self,
        proposals: pl.DataFrame,
        *,
        confidence: str | None = None,
        names: Sequence[str] = (),
        reviewer: str,
        reviewed_on: date,
    ) -> Sequence[Override]: ...

    def append_overrides(self, path: Path, overrides: Sequence[Override]) -> int: ...


def _review_api() -> _ReviewApi:
    """Return the reviewed-name module (D54), or fail with a clear message."""
    try:
        module = importlib.import_module("etfh.secmaster.review")
    except ModuleNotFoundError:
        typer.echo(
            "error: name review is not available in this build (etfh.secmaster.review is missing)",
            err=True,
        )
        raise typer.Exit(code=2) from None
    return cast(_ReviewApi, module)


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
    api = _review_api()
    reviewed_on = datetime.now(tz=_TORONTO).date()
    approved = list(
        api.approve(
            proposals,
            confidence=confidence,
            names=wanted,
            reviewer=reviewer,
            reviewed_on=reviewed_on,
        )
    )
    existing = _override_keys(overrides)
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
    added = api.append_overrides(overrides, new)
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
    if source is None:
        typer.echo(f"error source={source_id} fund={fund.ticker}: unknown source")
        return
    if source.terms.decision not in (Decision.allowed, Decision.allowed_with_conditions):
        typer.echo(
            f"skipped: terms source={source_id} fund={fund.ticker} "
            f"decision={source.terms.decision.value}"
        )
        return
    if not source.supported:
        typer.echo(f"skipped: unsupported source={source_id} fund={fund.ticker}")
        return
    if source.kind is SourceKind.synthetic:
        typer.echo(f"skipped: synthetic source={source_id} fund={fund.ticker}")
        return
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


def _override_keys(path: Path) -> set[tuple[str, str]]:
    """``(match_type, match_value)`` pairs already recorded in ``path``."""
    if not Path(path).is_file():
        return set()
    return {(override.match_type, override.match_value) for override in load_overrides(path)}


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
