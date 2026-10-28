# Architecture — cad-etf-holdings

cad-etf-holdings collects the portfolio holdings that Canadian ETF issuers publish, normalizes them into one point-in-time dataset, detects changes, and computes overlap and concentration across funds. It also publishes a weekly digest of those changes and measures abnormal returns around large additions. This document is the cross-cutting design. Per-epic detail lives in `docs/epic-NN-<slug>.md`.

## 1. Goals and constraints

- **Responsible collection.** Every source has a recorded terms-of-use decision in `config/sources.yaml`. Nothing is fetched without `allowed` or `allowed_with_conditions`. At most one request per second per host, slower when robots.txt `Crawl-delay` or recorded conditions require it. Identifiable User-Agent with a contact address. No logins, no paywalls, no headless browsers.
- **Point-in-time correctness.** Analytics as of date *t* use only files whose `known_at <= t`. A republished file is a new version, never an overwrite.
- **No silent merges.** A holding that cannot be mapped to a security through a provided identifier, an exchange-qualified ticker or a human-reviewed name override stays unmatched and is reported.
- **Reproducible.** `uv.lock` pins the environment; `etfh build` from the same raw store produces identical tables, the digest validates before it is rendered and the site is byte-identical for the same documents.
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
                              │                         │
                              │                         └──> etfh verify-raw / sync-raw (SHA-256 audit)
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
                      build: file_versions, sources, holdings, holdings_changes, fund_metrics
                              │                         (DuckDB + Parquet)
                              ├──> analytics (as-of aware) ──> CLI · Streamlit dashboard · derived exports
                              │
                              ├──> etfh.events.large_additions ──> etfh.eventstudy ──> public CAR tables
                              │                                         ^
                              │                                         │ adjusted closes
                              │                              etfh.prices (Tiingo, cached + budgeted)
                              │
                              └──> etfh.digest.build_digest (week over week, D79 policy)
                                          │ validated document(s)
                                          v
                                   etfh.site.render_site ──> index.html · weeks/ · archive.html
                                                              data/ · feed.xml · .nojekyll
                                          │
                                          v
                                   GitHub Pages workflow (disabled until ETFH_PAGES=true)
```

Two scheduled workflows sit outside the laptop:

```
private data repo (broiisapro/cad-etf-holdings-data)
        ^  raw/ pushed by the collect job (deploy key)
        │
  collect.yml ──> etfh collect · collect-aum · verify-raw · build · qa (markdown) · export
        │           artifacts and job summaries carry derived aggregates only (D77)
        v
  derived aggregates (runner temp, 30-day artifact)

  pages.yml ──> ops/build_digest.sh demo|real ──> site/ ──> actions/deploy-pages
```

## 3. Package layout

```
etfh/
  __init__.py
  types.py          RawFile, ParsedFile, Collector & Parser protocols, SourceKind enums
  registry.py       load sources.yaml / funds.csv (pydantic v2), terms gate
  http.py           PoliteClient: per-host limiter, robots cache, tenacity retries,
                    conditional GET, optional request headers, gate re-check
  storage.py        raw-store layout, write/iter raw files, sidecar metadata, SHA-256
  rawcheck.py       verify_raw_store (SHA-256 against sidecars, layout, orphans) and
                    sync_raw_store (both-or-neither pairs, conflicts never overwritten);
                    also runs as `python -m etfh.rawcheck verify|sync`
  schema.py         pandera.polars models: Holdings, QaEvent, HoldingsChange,
                    ParsedFundMetrics, FundMetrics; DuckDB DDL
  units.py          weight-unit normalization (percent ↔ fraction), numeric parsing helpers
  synthetic.py      deterministic synthetic issuer-format generator (CSV / XLSX / JSON / PDF)
  collect/          one module per issuer (e.g. bmo.py, evolve.py, purpose.py,
                    bmo_metrics.py, evolve_metrics.py); *_qpd.py for disclosures
  parse/            one module per issuer format; qpd_pdf.py for disclosure tables;
                    purpose_metrics.py reads NAV/AUM out of stored purpose_daily files
  secmaster/        identifiers.py (ISIN/CUSIP check digits, MIC map, composite exchange
                    codes), resolve.py (reviewed overrides, CASH/DERIV ids, optional FIGI
                    ids), overrides.py, names.py (normalization and proposals), review.py
                    (approve proposals → overrides file), openfigi.py (cached, opt-in)
  validate.py       file-level checks → qa_events; balancing-line and commodity predicates
  build.py          rebuild DuckDB from raw store; versions; holdings_changes; sources;
                    fund_metrics; review reports; diverts prices and metrics files
  analytics.py      overlap, top-10, Herfindahl, counts, breakdowns, metrics_asof, flows
  events.py         large_additions: point-in-time add/increase events from file_versions
                    + holdings (never from holdings_changes)
  eventstudy.py     run_event_study: market-adjusted / market-model CARs, t-stats,
                    bootstrap intervals, public/internal split, write_outputs
  prices/           PriceSource implementations and helpers:
                    symbols.py (US-listed MIC → ticker), tiingo.py (cached, gated,
                    budgeted), synthetic.py (deterministic offline), budget.py
                    (PriceBudget: 50/h, 1,000/day, 500 symbols/month), frames.py
  digest/           weekly digest document (schema_version 1): window.py (Friday-to-Friday,
                    cutoff), policy.py (D79), compare.py (snapshot selection, changes,
                    turnover, overlap shifts), document.py (build_digest), schema.py
                    (validate_digest)
  site/             static site: model.py (validated view of a digest), pages.py
                    (index/week/archive HTML), feed.py (Atom), format.py, style.py,
                    render.py (render_site: writes the file set, prunes stale weeks)
  cli.py            Typer app: collect, collect-aum, parse, build, qa, overlap, changes,
                    flows, backfill-qpd, spotcheck, export, weekly, event-study,
                    prices fetch, verify-raw, sync-raw, review
  dashboard/        app.py (Streamlit + Altair), demo.py (synthetic demo store),
                    weekly.py (digest tables for the "This week" tab),
                    quality.py (data-quality panel), format.py (number formatting)
  spotcheck.py      sampled raw values next to normalized weights
  export.py         derived aggregates only (overlap matrix, concentration, sector breakdown)
config/
  sources.yaml      source registry + terms-of-use records
  funds.csv         fund universe
  security_overrides.csv   human-reviewed identifier and name overrides
  validation.yaml   weight-sum and disclosure tolerances
ops/
  launchd/com.etfh.collect.plist   local daily schedule
  run_collect.sh                   collect → collect-aum → build → qa, exit with QA's status
  collect_guard.sh                 sleep-proof once-a-day guard for launchd
  install_launchd.sh               installs the guard
  actions_collect.sh               the same pass for GitHub Actions, pushing raw to the data repo
  build_digest.sh                  demo|real → etfh weekly → site/
  seed_data_repo.sh                verify + sync a local raw store into a data-repo checkout
.github/workflows/
  ci.yml            lint, types, tests (required) + secrets (gitleaks, not required)
  collect.yml       scheduled collection (disabled until ETFH_ACTIONS_COLLECT=true)
  pages.yml         weekly digest build + Pages deploy (disabled until ETFH_PAGES=true)
tests/
  fixtures/raw_synthetic/  synthetic raw store (committed; generated by etfh.synthetic)
  fixtures/digest*/        digest documents, golden weeks and a rendered-site snapshot
  fixtures/expected_overlap.json
```

## 4. Core interfaces (frozen in Epic 0)

- **`RawFile`** (frozen dataclass): `source_id`, `issuer`, `fund_ticker`, `as_of_hint: date | None`, `fetched_at: datetime (UTC)`, `url`, `path`, `sha256`, `content_type`, `ext`.
- **`Collector` protocol:** `source_id: str`; `fetch(fund: Fund, date: date, client: PoliteClient, store: RawStore) -> list[RawFile]`. A collector never bypasses `PoliteClient`.
- **`Parser` protocol:** `formats: frozenset[str]`; `can_parse(raw: RawFile) -> bool`; `parse(raw: RawFile) -> ParsedFile` where `ParsedFile` carries `as_of_date`, optional `published_at`, the `HoldingsFrame` (Polars) and parser notes. Weight unit is declared by the parser, not guessed. Metrics parsers return `ParsedFundMetrics` instead.
- **`HoldingsFrame` schema** (columns, all in the normalized `holdings` table): `fund_ticker`, `as_of_date`, `known_at`, `file_version`, `security_id` (nullable), `security_name`, `security_ticker`, `exchange` (MIC), `isin`, `cusip`, `asset_class`, `sector`, `country`, `currency`, `weight` (fraction), `shares`, `market_value`, `source_id`, `raw_file_sha256`.
- **`known_at`:** fetch time for daily files; the stated publication date for disclosures when one is stated, otherwise fetch time.

## 5. Point-in-time model

- A **file version** is identified by (`source_id`, `fund_ticker`, `as_of_date`, `raw_file_sha256`). Its `known_at` is the earliest fetch that produced that hash. A later fetch with the same hash adds nothing. A later fetch with a different hash for the same fund and as-of date is a new `file_version` (1, 2, …) with its own `known_at`.
- **As-of query for date *t*:** for each fund, consider versions with `known_at <= t`; take the latest `as_of_date`, and within it the highest `file_version`. Nothing known after *t* can affect the result.
- **Change detection** compares consecutive `as_of_date`s per fund (latest version of each), emitting `add`, `drop`, and `weight_change` rows above a configurable threshold.
- **Events** (`etfh.events`) walk the same versions in `known_at` order and emit an add/increase only when the version that first shows it was known on its `known_date`; a republished version never duplicates an event.
- **The weekly digest** selects two snapshots the same way, at the week-start and week-end cutoffs, and never reads `holdings_changes`.

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
  the source, the sum-to-1 check becomes a warning and the file is analysed as partial. A
  partial daily file whose weights sum to at most 1.10 is admitted with its published
  weights unchanged (D80a).
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
- `metrics_asof` and `flows` for fund-level NAV, shares and AUM, with the D67 estimate and its mandatory method label.
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
  conditional GET; an identifiable User-Agent from `ETFH_USER_AGENT`. Extra request headers
  are allowed for APIs, but the User-Agent cannot be overridden.
- The terms gate runs twice (D3): `etfh collect` refuses any source whose record is not
  `allowed`/`allowed_with_conditions` before a collector is looked up, and `PoliteClient.get`
  re-checks the record for the exact `Source` bound to the collector. `collector_for` returns
  a copy of the registered collector with its `source` set, so `etfh collect --config DIR`
  gates every request with DIR's terms record. Price fetches use the same gate.
- Collectors prefer direct download links (CSV/XLSX/JSON/PDF). A source that needs
  JavaScript with no underlying link, or that blocks the identifiable User-Agent, is marked
  unsupported rather than worked around.
- **Local schedule.** `ops/install_launchd.sh` installs a `launchd` job behind
  `ops/collect_guard.sh`, which runs `ops/run_collect.sh` at most once per local day after
  18:00 (or immediately after a missed day) and stamps the day only when something was
  fetched or confirmed unchanged (D64).
- **Scheduled collection (FR-2.3).** `.github/workflows/collect.yml` runs weekdays at
  23:17 UTC when `vars.ETFH_ACTIONS_COLLECT == 'true'` (ships disabled, D76). It checks out
  the private data repository with a deploy key, then `ops/actions_collect.sh` runs collect
  → collect-aum → `verify-raw` → commits and pushes `raw/` to the data repository's branch
  (`git -c` identity from `ETFH_GIT_NAME`/`ETFH_GIT_EMAIL`, D81) → builds into the runner's
  temporary directory → appends `etfh qa --format markdown` to the job summary → exports
  derived aggregates. Pushing before the build means a QA failure never loses a day's
  files; QA's exit status is the job's. Raw files never enter the public repository, its
  logs, summaries or artifacts (D77).
- **Seeding.** `ops/seed_data_repo.sh RAW_DIR DATA_CHECKOUT` verifies the local store, syncs
  it into `DATA_CHECKOUT/raw` and commits there; it never pushes.

## 10. Storage

- Raw store: files outside git (`./raw` by default), laid out `raw/<issuer>/<ticker>/<YYYY-MM-DD>/<fetched_at>.<ext>` with a `.meta.json` sidecar (URL, source, SHA-256, HTTP status, ETag/Last-Modified). The weekly collection pushes the same layout to the private data repository.
- Build output: `<out>/etfh.duckdb`, `parquet/<table>.parquet` and `reports/` (`unmatched.csv`,
  `fuzzy_proposals.csv`, `name_proposals.csv`). The tables are the frozen `file_versions`,
  `holdings`, `qa_events` and `holdings_changes` plus the additive
  `sources(source_id, issuer, kind, format)` table written from the build's config (D53)
  and `fund_metrics`. Every table is rebuilt from scratch and sorted on a documented key
  before writing, so the same raw store produces byte-identical Parquet.
- A raw file the build cannot parse, or that names an unknown source or fund, still gets a
  `file_versions` row with `status='excluded'` and a `qa_events` error, so the QA report can
  account for every file it saw. Metrics inputs are versioned and validated as metrics
  files, never as holdings. Price responses (`SourceKind.prices`) are diverted before the
  holdings path and get no `file_versions` row at all — they are a price cache, not a
  holdings source.
- The price request budget lives at `<raw>/../.etfh/price_budget.json`, outside the store
  that is synced file-for-file to the data repository, so command state never travels with
  the data.

## 11. Weekly digest and the static site

- **Window.** `week_start = week_end - 7 days`; the default week end is the most recent
  Friday (`etfh.digest.default_week_end`). The cutoff for each end is
  `etfh.asof.cutoff_utc(date)`, the America/Toronto start of the next day.
- **Comparison.** Start snapshot = latest portfolio known by the week-start cutoff; if none,
  the earliest comparable portfolio inside the window (`new_in_window`); end snapshot = the
  latest known by the week-end cutoff. The same version at both ends is `no_change_data`.
  Partial quarterly disclosures are removed before the choice, so they never mix into a
  comparison.
- **Document.** `build_digest` returns a JSON-ready dict (`schema_version: 1`) with one entry
  per fund: status, portfolio dates, counts of adds/drops/weight changes, turnover
  (`0.5 · Σ|Δw|`), top-10 weight at both ends, flow-estimate sum, and the change lines when
  D79 permits security-level detail. It also carries overlap shifts for pairs present at both
  cutoffs and the file/fund accounting under `quality`. Floats are rounded to 6 dp and
  `json.dumps(doc, sort_keys=True)` is byte-stable for the same database.
  `validate_digest` rejects a malformed document before anything is rendered.
- **Policy (D79).** `securities_disclosed` is true for an internal document, for a synthetic
  fund, or when every source of the fund records `terms.redistribution_allowed`; a source id
  missing from the registry fails closed. The site drops change lines again at parse time
  and sanitises the JSON it publishes.
- **Site.** `etfh.site.render_site(digests, out_dir, *, site_title, base_url=None)` writes
  `index.html` (latest week, with a `table#changes`), `weeks/<week_end>.html`,
  `archive.html`, `data/<week_end>.json`, `feed.xml` (Atom) and `.nojekyll`. No JavaScript,
  no external requests, inline CSS with light/dark tokens, semantic tables, a demo banner on
  demo weeks and "Derived data · exploratory · not investment advice" on every page. Stale
  dated week files from an earlier run are pruned.
- **Command.** `etfh weekly` builds one document per requested week end, validates each,
  renders the site and prints the index path. Empty weeks are reported and left out;
  `--public` (default) applies D79, `--internal` includes security lines for every fund;
  `--demo` builds the committed synthetic store.
- **Pages.** `.github/workflows/pages.yml` runs Saturdays at 13:23 UTC and is skipped unless
  `ETFH_PAGES == 'true'`. It runs `ops/build_digest.sh demo - site` (synthetic store, one
  week) or, with `ETFH_PAGES_REAL == 'true'`, checks out the private data repository and runs
  `ops/build_digest.sh real data site` (rebuild + eight public weeks). The script fails if
  `index.html` is missing and, in real mode, if any published file contains the absolute data
  checkout path.

## 12. Prices and the event study

- **Events.** `etfh.events.large_additions(con, *, min_weight=0.01, min_increase=0.005,
  start=None, end=None, include_quarterly=False)` returns one row per add/increase with the
  fund, security, portfolio date, `known_date` (the Toronto date of the version's `known_at`),
  weights, delta and source kind. Cash, fx, derivative, balancing-line and unmatched rows
  never become events; duplicate lines for one security are summed first. Quarterly mode is
  off by default and forms its own per-fund chain.
- **Prices.** `etfh.prices` implements the `PriceSource` protocol: `daily_closes(symbols,
  start, end)` returns `symbol, date, close` with split- and dividend-adjusted closes, sorted,
  and omits unknown symbols instead of failing. `symbol_for` maps only US-listed MICs
  (`XNYS`, `XNAS`, `XASE`, `ARCX`, `BATS`) to a ticker (`BRK.B` → `BRK-B`); ISIN-only,
  name-only, TSX and country-composite rows resolve to `None`. `TiingoPrices.from_env`
  reads `TIINGO_API_KEY` (sent only as a request header, never logged or stored), goes
  through `PoliteClient`, refuses unless `tiingo_prices` passes the terms gate (D84), caches
  raw responses in the raw store and checks `PriceBudget` before every request.
  `SyntheticPrices` is the deterministic offline source used by demos and tests.
- **Method.** Day 0 is the first benchmark trading day on or after `known_date` (within ten
  calendar days). Simple returns are computed over a benchmark-defined trading calendar;
  `market_adjusted` uses `AR = r − r_benchmark`, `market_model` estimates OLS α/β over
  `[-120, -11]` (at least 60 usable days). CARs are reported for `[-1,1]`, `[0,1]`, `[0,5]`
  and `[-5,5]` when they fit the requested window, with a cross-sectional mean, t-statistic
  and a percentile bootstrap 95 % interval. Every skipped event is counted under
  `no_symbol`, `no_prices`, `short_estimation`, `window_incomplete` or `duplicate`, and
  `n_events = n_used + Σ skipped`.
- **Outputs.** `write_outputs` writes `summary.json` (aggregates, label `exploratory`),
  `car_by_day.csv` (aggregates) and `per_event.internal.csv` (security-level, internal
  only). `etfh event-study` prints only the public summary.

## 13. Testing

- Offline by default; live tests are marked `@pytest.mark.network` and excluded from CI.
  Price fetches and collection are faked or served from local fixtures, so the suite needs
  no key and no network.
- CI runs the same `lint`, `types` and `tests` jobs on GitHub-hosted `ubuntu-latest`
  runners (D81/D87/D88); the `runs-on` label set still comes from the repository variable
  `CI_RUNS_ON` (D60). A non-required `secrets` job runs gitleaks on every PR. The required
  checks use the non-strict policy (D48): a branch need not be up to date with `main` to
  merge.
- Synthetic fixtures generated deterministically by `etfh.synthetic` in issuer-like formats, including two injected bad files, a republished file, a percent-vs-fraction pair, a same-ticker-different-exchange pair, and a late-known file that would change overlap if lookahead leaked. Digest fixtures add golden weeks and a rendered-site snapshot; the event study has an injected-return acceptance test.
- Classic-bug tests: collector refuses non-allowed source; percent vs fraction weights normalized; weight-sum tolerance by asset class; same ticker on different exchanges not merged; overlap uses only files known by as-of; republished file creates a new version; prices never become holdings rows; the public digest never carries a closed security line.

## 14. Key decisions and alternatives considered

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
| Digest transport | One validated JSON document, one renderer | Query the database in each page | The document is the publishable contract; validation happens once, the site and the JSON cannot disagree. |
| Public detail | Aggregates for real funds (D79) | Publish real holdings lines | Source terms forbid redistribution; the policy fails closed and is enforced in the producer, the site parser and the JSON. |
| Prices | Tiingo behind a `PriceSource` protocol | Vendor SDK | The free plan fits an exploratory study, the protocol keeps the provider replaceable, and the budget file enforces the plan limits. |

## 15. Redistribution and publication

The public repository contains code, synthetic fixtures and derived aggregates (overlap,
concentration, counts, flow estimates) only, unless a source's terms record permits more.
Raw files and the normalized holdings of real funds stay outside git, and scheduled
collection writes them to the private data repository only. Public outputs follow D79: a
security-level line may be shown only for a synthetic fund or a source whose terms record
`redistribution_allowed: true`; everything else is published as aggregates. Publishing real
aggregates on Pages still requires the maintainer's explicit go-ahead.
