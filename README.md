# cad-etf-holdings

cad-etf-holdings collects the portfolio holdings Canadian ETF issuers publish, normalizes them into one point-in-time dataset, and answers questions like "how much do these two funds overlap?" and "which ETFs added this stock last quarter?". Collection is deliberately polite: every source has a recorded terms-of-use decision, and nothing is fetched or redistributed without one.

![Dashboard overlap view: two synthetic funds share 24.84% of their portfolios](docs/img/overlap.png)

Status: Phase 1 release candidate. The pipeline runs end to end on the committed synthetic fixtures and on live files from the approved sources; the public release waits for the final scheduled-collection checks.

## What it does

- **Collects what issuers publish.** Daily holdings files (CSV, XLSX, JSON) and quarterly portfolio disclosure PDFs, stored unmodified with URL, fetch time and SHA-256 so every number can be traced back to its file.
- **Normalizes it into one table.** Weights as fractions, exchanges as ISO 10383 MICs, asset classes, and a security master that uses a provided ISIN/CUSIP first, then an exchange-qualified ticker, then a human-reviewed name override — never a silent merge.
- **Keeps it point-in-time.** Every distinct file content is a version with a `known_at` timestamp; analytics for a date use only files known by then, and a republished file becomes a new version instead of overwriting the old one.
- **Detects changes.** Adds, drops and weight changes between consecutive portfolio dates, per fund.
- **Measures overlap and concentration.** Pairwise overlap (`Σ min(wᵢ, wⱼ)`), top-10 weight, Herfindahl index, holding counts and sector/country breakdowns, shown in a Streamlit dashboard and available through the CLI and derived-aggregate exports.

## Responsible collection

- **Terms gate.** Every source has a recorded terms-of-use decision in `config/sources.yaml`. A source that is not `allowed` or `allowed_with_conditions` is refused in code — both when `collect` picks collectors and again on every HTTP request. The synthetic sources never make a request.
- **Polite requests.** At most one request per second per host, slower when robots.txt `Crawl-delay` or the source's recorded conditions say so. robots.txt is honoured per RFC 9309, retries use exponential backoff with jitter, and `Retry-After` is respected up to a cap.
- **Identifiable client.** The User-Agent comes from `ETFH_USER_AGENT` and carries a contact address; it is never spoofed. No logins, no paywalls, no headless browsers. A source that needs JavaScript or blocks the client is marked unsupported instead of worked around.
- **Nothing raw is redistributed.** Downloads and the normalized holdings of real funds stay outside git. The public tree holds code, synthetic fixtures and derived aggregates (overlap and concentration statistics) only, unless a source's terms record explicitly permits more.

## Quick start (synthetic data)

Everything below runs offline against the committed fixtures in `tests/fixtures/`.

```bash
uv sync --frozen
uv run ruff check . && uv run mypy etfh
uv run pytest -q                                                    # offline suite
uv run etfh build --raw tests/fixtures/raw_synthetic --out /tmp/etfh
uv run etfh qa --db /tmp/etfh/etfh.duckdb                           # exit 0; 2 injected bad files reported
uv run etfh overlap SYNA SYNB --asof 2026-11-02 --db /tmp/etfh/etfh.duckdb   # 0.2484
uv run etfh changes --fund SYNA --from 2026-10-29 --to 2026-11-02 --db /tmp/etfh/etfh.duckdb
uv run etfh spotcheck --raw tests/fixtures/raw_synthetic --db /tmp/etfh/etfh.duckdb --n 5
uv run etfh export --db /tmp/etfh/etfh.duckdb --out /tmp/etfh-export --asof 2026-11-02
ETFH_DB=/tmp/etfh/etfh.duckdb uv run streamlit run etfh/dashboard/app.py
```

The overlap value matches `tests/fixtures/expected_overlap.json`. `etfh build` is a full rebuild, not an incremental update: the same raw store produces byte-identical Parquet. `etfh spotcheck` re-reads the latest included file per issuer and prints sampled raw values next to the normalized weights, so a parsing bug is visible against the file the numbers came from. `etfh export` writes derived aggregates only (`overlap_matrix.csv`, `concentration.csv`, `sector_breakdown.csv`) — never holdings rows, names or individual weights.

To preview the public demo (synthetic store built into a temporary directory at startup, no real data, no network):

```bash
uv run streamlit run streamlit_app.py
# or: ETFH_DEMO=1 uv run streamlit run etfh/dashboard/app.py
```

## Running on real data

1. Copy `.env.example` to `.env` and set `ETFH_USER_AGENT` to a contact-bearing identifier (quote values containing spaces or parentheses). `OPENFIGI_API_KEY` is optional and only raises the OpenFIGI rate limit.
2. Collect, build and check:

```bash
uv run etfh collect --date today --raw ./raw     # logs one line per fund/source; terms-refused sources print "skipped: terms"
uv run etfh build --raw ./raw --out ./build      # add --openfigi for cached composite FIGI ids
uv run etfh qa --db ./build/etfh.duckdb          # exit 0 only when every errored file is excluded and reported
```

3. Review the name proposals the build wrote next to the database:

```bash
uv run etfh review list --db ./build/etfh.duckdb --confidence exact
uv run etfh review approve --db ./build/etfh.duckdb --confidence exact \
  --reviewer "your name" --dry-run               # drop --dry-run to append the overrides
uv run etfh build --raw ./raw --out ./build      # overrides take effect on the next build
```

`etfh qa --max-unmatched-share X` turns the unmatched weight share into a gate and exits 3 when an included daily file exceeds it (quarterly top-holdings lists are not held to it). `ops/run_collect.sh` runs the whole pass daily (it loads `.env` itself), and `ops/install_launchd.sh` installs that script as a launchd job; the job's exit status is QA's status.

## Architecture

One deterministic pipeline: collectors write every downloaded file unmodified into a raw store with a SHA-256 sidecar; per-issuer parsers declare their weight unit and produce the frozen holdings schema; validation and the security master turn that into the included `holdings` table plus `qa_events`; the build writes DuckDB and parquet, file versions, the additive `sources` table and change detection; analytics reads only what was known by the requested date and feeds the CLI, the Streamlit dashboard and derived exports. The raw store is the source of truth and the build is always from scratch, so results are reproducible and auditable. The full design, package layout and decision records are in [`docs/architecture.md`](docs/architecture.md); per-epic detail is in [`docs/epic-00-foundation.md`](docs/epic-00-foundation.md), [`docs/epic-01-pipeline-core.md`](docs/epic-01-pipeline-core.md) and [`docs/epic-02-real-data-integration.md`](docs/epic-02-real-data-integration.md).

## Data quality

- **Validation before anything is derived.** Pandera checks every parsed frame; file-level checks cover weight sums (with a tolerance that accounts for the printed unit and row count), duplicates, future as-of dates, unit contradictions, and quarterly disclosures against the total printed in the document. A file that fails gets no holdings rows but keeps a `file_versions` row and a `qa_events` entry, so it is visible rather than silently dropped.
- **A QA report with an exit code.** `etfh qa` reports every errored file, the daily and quarterly unmatched weight shares with per-issuer medians, and the worst funds. It exits 0 only when every errored file is excluded and reported, 1 when the tables are missing or an error is not accounted for, and 3 when `--max-unmatched-share` is exceeded.
- **Unmatched means unmatched.** A holding without identifier evidence stays unmatched and is reported (`reports/unmatched.csv`, `reports/name_proposals.csv`, `reports/fuzzy_proposals.csv`). Name-only rows are resolved only by a human-reviewed override that records the reviewer and review date; fuzzy proposals are suggestions, never applied automatically.
- **Traceable numbers.** `etfh spotcheck` prints raw file values next to normalized weights, and every holdings row carries the SHA-256 of the file it came from.

## Limitations

- **Coverage follows permissions, not ambition.** Eight issuer sources are approved and the universe has 52 real funds plus 7 synthetic fixtures. Some issuer files are unsupported by design (bot challenge or JavaScript-only pages) and are never worked around.
- **Quarterly disclosures are top holdings only.** They are analysed as `partial` and labelled "top holdings only" in the dashboard; they never mix into a fund's daily series.
- **Name-only issuers stay partly unmatched.** Exact name proposals have been reviewed and committed, but fuzzy matches require a human decision per name.
- **OpenFIGI is optional and off by default.** Without `--openfigi`, ticker-only and ISIN issuers share fewer ids; with it, a cold cache needs network access.
- **No history before collection started.** Historical backfill from older disclosures is Phase 2, not Phase 1.
- **Real holdings are not distributed.** Only derived aggregates are shareable; there is no public dataset of individual fund holdings.
- **Scheduling is local.** Collection currently runs as a launchd job on one Mac; moving it to GitHub Actions is Phase 2.
- **Research software.** This is an engineering and data-quality project, not investment advice.

## Licence

MIT — see [`LICENSE`](LICENSE). Copyright (c) 2026 Moksh Siruvani.
