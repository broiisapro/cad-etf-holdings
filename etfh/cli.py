"""Command-line interface for cad-etf-holdings."""

from __future__ import annotations

import sys
from typing import NoReturn

import typer

app = typer.Typer(
    name="etfh",
    help="Point-in-time Canadian ETF holdings: collect, normalize, validate and analyze.",
    no_args_is_help=True,
)


def _not_implemented() -> NoReturn:
    typer.echo("not implemented yet", err=True)
    raise typer.Exit(code=2)


@app.command()
def collect() -> None:
    """Collect raw holdings files from sources whose terms allow collection."""
    _not_implemented()


@app.command()
def parse() -> None:
    """Parse raw holdings files into the normalized holdings schema."""
    _not_implemented()


@app.command()
def build() -> None:
    """Rebuild the DuckDB database and Parquet exports from the raw store."""
    _not_implemented()


@app.command()
def qa() -> None:
    """Report validation results and excluded files."""
    _not_implemented()


@app.command()
def overlap() -> None:
    """Compute pairwise holdings overlap between two funds as of a date."""
    _not_implemented()


@app.command()
def changes() -> None:
    """List holdings changes for a fund between two dates."""
    _not_implemented()


if __name__ == "__main__":
    sys.exit(app())
