# Architecture — cad-etf-holdings

cad-etf-holdings collects the portfolio holdings that Canadian ETF issuers publish, normalizes them into one point-in-time dataset, detects changes, and computes overlap and concentration across funds. This document is the cross-cutting design. Per-epic detail lives in `docs/epic-NN-<slug>.md`.

## 1. Goals and constraints

- **Responsible collection.** Every source has a recorded terms-of-use decision in `config/sources.yaml`. Nothing is fetched without `allowed` or `allowed_with_conditions`. At most one request per second per host, slower when robots.txt `Crawl-delay` or recorded conditions require it. Identifiable User-Agent with a contact address. No logins, no paywalls, no headless browsers.
- **Point-in-time correctness.** Analytics as of date *t* use only files whose `known_at <= t`. A republished file is a new version, never an overwrite.
- **No silent merges.** A holding that cannot be mapped to a security through a provided identifier, an exchange-qualified ticker or a human-reviewed name override stays unmatched and is reported.
- **Reproducible.** `uv.lock` pins the environment; `etfh build` from the same raw store produces identical tables.
- **Small and local.** ~40 funds of daily files is megabytes per month. Everything runs on one laptop; tests are offline.

## 2. Data flow

```
config/sources.yaml ─┐
config/funds.csv ────┴─> registry (terms gate)
                              │ allowed sources only
                              v
                      collectors ──(etfh.http: limiter, robots, retries, UA)──> issuer sites
                              │
                              v
             raw store (unmodified files + .meta.json, SHA-256, fetched_at)   [outside git]
                              │
                              v
                      parsers (per issuer format: CSV / XLSX / JSON / QPD PDF)
                              │  HoldingsFrame (raw-normalized: units, columns)
                              v
                      validation (Pandera schema + file-level checks) ──> qa_events
                              │ passing files only
                              v
                      security master (overrides → non-security ids → ISIN/CUSIP
                      → MIC+ticker → reviewed name override) ──> unmatched + proposals
                              │                                    │
                              │                                    v
                              │                        human review: etfh review approve
                              │                                    │
                              │                                    v
                              │                    config/security_overrides.csv (next build)
                              v
                      build: file_versions, sources, holdings, holdings_changes  (DuckDB + Parquet)
                              │
                              v
                      analytics (as-of aware) ──> CLI · Streamlit dashboard · derived exports
```

## 3. Package layout

```
etfh/
  __init__.py
  types.py          RawFile, ParsedFile, Collector & Parser protocols, enums
  registry.py       load sources.yaml / funds.csv (pydantic v2), terms gate
  http.py           PoliteClient: per-host limiter, robots cache, tenacity retries, conditional GET, gate re-check
  storage.py        raw-store layout, write/iter raw files, sidecar metadata, SHA-256
  schema.py         pandera.polars models: Holdings, QaEvent, HoldingsChange; DuckDB DDL
  units.py          weight-unit normalization (percent ↔ fraction), numeric parsing helpers
  synthetic.py      deterministic synthetic issuer-format generator (CSV / XLSX / JSON / PDF)
  collect/          one module per issuer (e.g. bmo.py, evolve.py, purpose.py); qpd.py for disclosures
  parse/            one module per issuer format; qpd_pdf.py for disclosure tables
  secmaster/        identifiers.py (ISIN/CUSIP check digits, MIC map, composite exchange codes),
                    resolve.py (reviewed overrides, CASH/DERIV ids, optional FIGI ids),
                    overrides.py, names.py (normalization and proposals), review.py
                    (approve proposals → overrides file), openfigi.py (cached, off by default)
  validate.py       file-level checks → qa_events
  build.py          rebuild DuckDB from raw store; versions; holdings_changes; sources; reports
  analytics.py      overlap, top-10, Herfindahl, counts, breakdowns, all as-of aware
  cli.py            Typer app: collect, parse, build, qa, review, overlap, changes, spotcheck, export
  dashboard/app.py  Streamlit + Altair
  spotcheck.py      sampled raw values next to normalized weights
  export.py         derived aggregates only (overlap matrix, concentration, sector breakdown)
config/
  sources.yaml      source registry + terms-of-use records
  funds.csv         fund universe
  security_overrides.csv   human-reviewed identifier and name overrides
  validation.yaml   weight-sum and disclosure tolerances
ops/
  launchd/com.etfh.collect.plist   daily schedule
  run_collect.sh                   collect → build → qa, exit with QA's status
tests/
  fixtures/raw_synthetic/  synthetic raw store (committed; generated by etfh.synthetic)
  fixtures/expected_overlap.json
```

## 4. Core interfaces (frozen in Epic 0)

- **`RawFile`** (frozen dataclass): `source_id`, `issuer`, `fund_ticker`, `as_of_hint: date | None`, `fetched_at: datetime (UTC)`, `url`, `path`, `sha256`, `content_type`, `ext`.
- **`Collector` protocol:** `source_id: str`; `fetch(fund: Fund, date: date, client: PoliteClient, store: RawStore) -> list[RawFile]`. A collector never bypasses `PoliteClient`.
- **`Parser` protocol:** `formats: frozenset[str]`; `can_parse(raw: RawFile) -> bool`; `parse(raw: RawFile) -> ParsedFile` where `ParsedFile` carries `as_of_date`, optional `published_at`, the `HoldingsFrame` (Polars) and parser notes. Weight unit is declared by the parser, not guessed.
- **`HoldingsFrame` schema** (columns, all in the normalized `holdings` table): `fund_ticker`, `as_of_date`, `known_at`, `file_version`, `security_id` (nullable), `security_name`, `security_ticker`, `exchange` (MIC), `isin`, `cusip`, `asset_class`, `sector`, `country`, `currency`, `weight` (fraction), `shares`, `market_value`, `source_id`, `raw_file_sha256`.
- **`known_at`:** fetch time for daily files; the stated publication date for disclosures when one is stated, otherwise fetch time.

## 5. Point-in-time model

- A **file version** is identified by (`source_id`, `fund_ticker`, `as_of_date`, `raw_file_sha256`). Its `known_at` is the earliest fetch that produced that hash. A later fetch with the same hash adds nothing. A later fetch with a different hash for the same fund and as-of date is a new `file_version` (1, 2, …) with its own `known_at`.
- **As-of query for date *t*:** for each fund, consider versions with `known_at <= t`; take the latest `as_of_date`, and within it the highest `file_version`. Nothing known after *t* can affect the result.
- **Change detection** compares consecutive `as_of_date`s per fund (latest version of each), emitting `add`, `drop`, and `weight_change` rows above a configurable threshold.

## 6. Security master

`etfh.secmaster` resolves every holding row to a natural-key `security_id` using
time-invariant evidence only (D41): the row's own provided identifiers, its
exchange-qualified ticker, the human-reviewed overrides file and, optionally, cached
OpenFIGI mappings. Links are never learned from other holdings files, so a file known later
cannot change how an earlier file's rows resolve. Overrides always win; the rest of the
order is:

1. Reviewed override in `config/security_overrides.csv`, most specific first (`isin`,
   `cusip`, `ticker` as `MIC:TICKER`, `name` as `<fund_ticker>|<exact name>` or
   `*|<normalized name>` for a global name override). Every override carries the reviewer
   and the review date.
2. Cash → `CASH:<CCY>` (`CASH:XXX`, ISO 4217 "no currency", when none is stated); fx and
   derivative rows → `DERIV:<fund>:<normalized ticker or full name>`, with stable `#2`,
   `#3` suffixes when two rows in one file still collide (D33). Non-security ids count in
   weight sums but never in overlap.
3. Provided valid ISIN → `ISIN:<isin>`.
4. Provided valid CUSIP → derived `ISIN:` for CA/US issuers (structural CUSIP→ISIN), else
   `CUSIP:<cusip>`.
5. MIC + ticker → `TKR:<MIC>:<TICKER>`. Issuers that publish only a Bloomberg exchange code
   keep it in `security_ticker` with a null `exchange` (`"AAPL US"`, D57); `assign` splits a
   trailing single-venue code into its MIC, while a country-composite code (`US`, `CN`) is
   never a venue and is used only for FIGI lookups. The same ticker on two exchanges never
   merges.
6. Otherwise the row stays unmatched (`security_id` null) and appears in
   `reports/unmatched.csv`.

Name-only rows are never merged automatically (D8, D54). The build writes
`reports/name_proposals.csv` for each unmatched normalized name: candidates are drawn from
identifier-bearing rows anywhere in the raw store and carry `confidence=exact` (normalized
names equal, countries agree when both are known, exactly one candidate id) or
`confidence=fuzzy` (score at or above the threshold). `etfh review list` shows them, and
`etfh review approve --confidence exact` (or one or more `--name`s) appends global name
overrides with the reviewer and date to `config/security_overrides.csv`; nothing is applied
by the build itself.

With `--openfigi` (D47, D55) every mappable `ISIN:` or `TKR:` id — provided, derived, or
from an override — is expressed as `FIGI:<compositeFIGI>`, so ticker-only and ISIN issuers
share one id space. Cash, derivative and unmapped rows keep their ids; lookups are cached
on disk and the mode is off by default.

## 7. Validation

Per file, before it enters derived tables:
- Pandera schema: types, required columns, `weight` finite and within `[-1, 1]`.
- Weight sums. A daily file must sum to 1 within an asset-class tolerance; a quarterly
  disclosure (a partial top-holdings list) is instead checked against the total printed in
  the document, never against 1 (D38). Any file whose weights sum to more than
  `1 + tolerance` is an error (`weight_sum_over_one`), daily or quarterly, partial or not
  (D56). The tolerance accounts for rounding:
  `min(0.02, max(asset-class tolerance, n_rows × half of the smallest unit printed in the
  file))`, using the parser's `weight_decimals` note.
- Duplicate lines (D51). The same non-null `security_id` on two rows is an error
  (`duplicate_security`, file excluded) only when the rows reach that id through different
  identifier forms (a double listing) or are identical in every parsed field; the same
  identifier repeated with different quantities is an `info` finding (`duplicate_lines`)
  and the rows stay separate.
- Partial daily files (D52). When a parser records `partial_list=true` with evidence from
  the source, the sum-to-1 check becomes a warning and the file is analysed as partial.
- `as_of_date` not after the fetch date in America/Toronto; numeric fields parse (no stray
  `-`, `%` or thousands separators); a unit contradiction is reported only when the other
  unit actually fits.

Failures write `qa_events` (file, check, severity, detail) and exclude the file from
derived tables. `etfh qa` exits 0 when every failing file is excluded and reported, 1 when
the tables are missing or an errored file is not excluded, and 3 when
`--max-unmatched-share X` is exceeded by an included daily file.

## 8. Analytics

All take an `asof` date and use the as-of query in §5:
- Pairwise overlap `Σ min(w_i, w_j)` over common `security_id`s (unmatched and non-security rows excluded), plus the list of common holdings.
- Top-10 weight, Herfindahl index `Σ w²`, number of holdings, sector and country breakdowns.
- Changes between two dates for a fund.
- A fund with several sources uses its daily holdings when a daily version is known; a
  quarterly top-holdings list is used only when no daily file exists and is flagged
  `partial` (D42, D53). The choice reads the build's `sources` table, never a weight-sum
  heuristic.

## 9. Collection

- `PoliteClient` wraps `httpx.Client`: per-host minimum interval `max(1s, robots Crawl-delay,
  source condition)`; robots.txt fetched once per host per run and obeyed per RFC 9309 —
  2xx obeys the file, 3xx follows up to five redirects, 4xx (including 401/403) means no
  restrictions, 5xx or unreachable disallows all for the run (D46); tenacity retries with
  exponential backoff and jitter on 429/5xx/timeouts; `Retry-After` honoured up to 120 s;
  conditional GET; an identifiable User-Agent from `ETFH_USER_AGENT`.
- The terms gate runs twice (D3): `etfh collect` refuses any source whose record is not
  `allowed`/`allowed_with_conditions` before a collector is looked up, and `PoliteClient.get`
  re-checks the record for the exact `Source` bound to the collector. `collector_for` returns
  a copy of the registered collector with its `source` set, so `etfh collect --config DIR`
  gates every request with DIR's terms record.
- Collectors prefer direct download links (CSV/XLSX/JSON/PDF). A source that needs
  JavaScript with no underlying link, or that blocks the identifiable User-Agent, is marked
  unsupported rather than worked around.
- Scheduling: `ops/install_launchd.sh` installs a `launchd` job that runs
  `ops/run_collect.sh` daily. The script adds the usual uv locations to PATH, loads `.env`,
  runs collect → build → qa and exits with QA's status, so a failed QA is a failed job.

## 10. Storage

- Raw store: files outside git (`./raw` by default), laid out `raw/<issuer>/<ticker>/<YYYY-MM-DD>/<fetched_at>.<ext>` with a `.meta.json` sidecar (URL, source, SHA-256, HTTP status, ETag/Last-Modified).
- Build output: `<out>/etfh.duckdb`, `parquet/<table>.parquet` and `reports/` (`unmatched.csv`,
  `fuzzy_proposals.csv`, `name_proposals.csv`). The tables are the frozen `file_versions`,
  `holdings`, `qa_events` and `holdings_changes` plus the additive
  `sources(source_id, issuer, kind, format)` table written from the build's config (D53).
  Every table is rebuilt from scratch and sorted on a documented key before writing, so the
  same raw store produces byte-identical Parquet.
- A raw file the build cannot parse, or that names an unknown source or fund, still gets a
  `file_versions` row with `status='excluded'` and a `qa_events` error, so the QA report can
  account for every file it saw.

## 11. Testing

- Offline by default; live tests are marked `@pytest.mark.network` and excluded from CI.
- CI runs the same `lint`, `types` and `tests` jobs on one self-hosted macOS ARM64 runner
  at low priority with single-threaded tool settings (D49: `ETFH_DUCKDB_THREADS=1`,
  `POLARS_MAX_THREADS=1`, `UV_CONCURRENT_*`), so the suite must stay fast and offline. The
  required checks use the non-strict policy (D48): a branch need not be up to date with
  `main` to merge.
- Synthetic fixtures generated deterministically by `etfh.synthetic` in issuer-like formats, including two injected bad files, a republished file, a percent-vs-fraction pair, a same-ticker-different-exchange pair, and a late-known file that would change overlap if lookahead leaked.
- Classic-bug tests: collector refuses non-allowed source; percent vs fraction weights normalized; weight-sum tolerance by asset class; same ticker on different exchanges not merged; overlap uses only files known by as-of; republished file creates a new version.

## 12. Key decisions and alternatives considered

| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| DataFrame engine | Polars 2.x | pandas | Faster, stricter types, native DuckDB interop, native Pandera support. Polars 2 streaming collect does not keep row order, so outputs are sorted explicitly. |
| Storage model | Rebuild DuckDB from the raw store each build | Incremental upserts into a long-lived DB | Data is small; full rebuild makes builds reproducible and versioning auditable. |
| PDF tables | pdfplumber + issuer rules, camelot 2.x fallback | camelot first | pdfplumber gives fine control of table settings without system dependencies; camelot 2 now uses pdfium (no Ghostscript). |
| Excel | Polars `read_excel` (calamine) | openpyxl only | Calamine is fast and now the Polars default; openpyxl kept for writing fixtures and as fallback. |
| Security ids | Natural-key ids (`ISIN:`, `CUSIP:`, `TKR:<MIC>:`, optional `FIGI:`) | Surrogate integer master table | Deterministic across rebuilds, self-describing, and makes the "never merge across exchanges" rule visible in the key. |
| Unidentified names | Reviewed global name overrides (`*\|<normalized name>`) | Automatic fuzzy merge | Name-only issuers can overlap with identifier issuers without ever guessing; every override names its reviewer and date. |
| JS-rendered sources | Mark unsupported | Headless browser | Keeps collection polite, fast and maintainable; matches the terms-first stance. |
| Config | pydantic v2 models over YAML/CSV | Untyped dicts | Bad terms records fail loudly at load, which the terms gate depends on. |

## 13. Redistribution

The public repository contains code, synthetic fixtures and derived aggregates (overlap and concentration statistics) only, unless a source's terms record permits more. Raw files and the normalized holdings of real funds stay outside git.
