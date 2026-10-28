# Epic 06 — Publication

## Goal

Epic 6 is Phase 3 of the PRD: turn the point-in-time dataset into things a
reader can use without running the code.

- **FR-3.1 — weekly digest.** Compare each fund's portfolio with the one a week
  earlier and publish the result — adds, drops, weight changes, turnover,
  concentration and overlap shifts — as a validated JSON document, a static
  site and an Atom feed, plus a "This week" tab in the dashboard. Real funds
  publish as aggregates only (D79).
- **FR-3.2 — event study (exploratory).** Find large additions in the
  point-in-time holdings, measure abnormal returns around them with a market
  benchmark and publish the aggregate statistics, keeping security-level rows
  internal.

Both ship with their schedules and publication switches **off** (D76). The code
was built in wave 08 while Epic 3 finished the ship preparation; enabling the
weekly Pages run and publishing real aggregates are wave-09 steps that need the
maintainer's go-ahead (D79).

## How it works

### Weekly digest (FR-3.1)

`etfh.digest.build_digest(con, week_end, *, public, sources, funds, top_n=10,
overlap_threshold=0.01)` returns a JSON-ready dict with `schema_version: 1`.

- **Window.** `week_start = week_end - 7 days` and the default week end is the
  most recent Friday (`default_week_end`). `known_by` records
  `etfh.asof.cutoff_utc(week_end)`, the America/Toronto start of the following
  day, so a document is reproducible from the database and the date alone.
- **Snapshots.** The start snapshot is the latest portfolio known by the
  week-start cutoff; if the fund published nothing by then, the earliest
  comparable portfolio inside the window (`new_in_window`). The end snapshot is
  the latest known by the week-end cutoff. Start and end being the same version
  is `no_change_data`. Quarterly top-holdings versions are removed before the
  choice, so a partial list never mixes into a comparison. Snapshots come from
  `holdings` and `file_versions`; `holdings_changes` is never read.
- **Per fund.** Ticker, issuer, name, status, portfolio dates, counts of adds,
  drops and weight changes (`WEIGHT_CHANGE_THRESHOLD = 0.0005`), turnover
  `0.5 · Σ|Δw|`, top-10 weight at both ends, the sum of daily flow estimates
  inside the week (null when there are none) and the number of flow days. Cash,
  fx and derivative rows are excluded by asset class and by `CASH:`/`DERIV:` id
  prefix. Unmatched rows still count in the counts and turnover through the
  build's name/ticker/currency fallback key, but a change line needs a real
  identifier, so they never appear in `changes`.
- **Overlap shifts.** Every pair of funds with a comparable portfolio at both
  cutoffs is measured with `analytics.overlap` at each end; pairs whose shift is
  at least `overlap_threshold` (default 0.01) are listed, sorted by absolute
  shift.
- **Quality.** Files included and excluded from `file_versions`, and
  `funds_without_data`: configured funds with files but no comparable portfolio
  this week (quarterly-only funds, funds whose files failed validation). A
  configured fund with no file at all is a collection-coverage question for the
  QA report, not a digest entry.
- **Validation.** `validate_digest` checks exact key sets, types, enums, list
  ordering, 6-dp rounding, `YYYY-MM-DD` dates, the D79 rules, the
  `no_change_data` invariants and `flow_days` consistency, and raises
  `ValueError` naming the first failing path. Floats are rounded to 6 dp and
  `json.dumps(doc, sort_keys=True)` is byte-stable for the same database.

### Static site

`etfh.site.render_site(digests, out_dir, *, site_title, base_url=None)` checks
the fields it uses, converts them to frozen dataclasses, sorts by week and
writes:

| File | Content |
|---|---|
| `index.html` | the latest week, with `table#changes` (one row per fund) |
| `weeks/<week_end>.html` | one page per digest, with older/newer navigation |
| `archive.html` | every published week, newest first |
| `data/<week_end>.json` | the validated document, `sort_keys=True`, indent 2 |
| `feed.xml` | Atom 1.0, one entry per week, ids from the documents |
| `.nojekyll` | keeps GitHub Pages from running Jekyll |

- No JavaScript and no external request: inline CSS with light/dark tokens,
  semantic headings and tables (`caption`, `scope`, right-aligned numerics),
  skip link and focus outlines.
- Every page carries "Derived data · exploratory · not investment advice"; a
  demo week additionally shows "Demo data — synthetic funds only".
- Security change lines are dropped at parse time when
  `securities_disclosed: false` and the published JSON is sanitised the same
  way, so a bad caller cannot publish a closed line (D79).
- Week and data files left over from an earlier run are pruned when they match
  `YYYY-MM-DD.(html|json)`, so publishing fewer weeks cannot leave an old page
  live. `base_url` adds canonical links and Atom ids and is validated as
  `http(s)`.
- Reruns are byte-identical for the same documents: ids and timestamps come
  from the documents, never from the clock.

### Pages workflow and switches

`.github/workflows/pages.yml` runs Saturdays at 13:23 UTC, after the week's
disclosures have settled, and on demand. It is skipped entirely unless
`ETFH_PAGES == 'true'`.

1. **build** (`ubuntu-latest`, 30 min): checkout, install uv and Python 3.12,
   `uv sync --frozen`. With `ETFH_PAGES_REAL == 'true'` it also checks out the
   private data repository with `ETFH_DATA_DEPLOY_KEY` and runs
   `ops/build_digest.sh real data site`; otherwise `ops/build_digest.sh demo -
   site`. The script runs `etfh weekly --demo --weeks 1` for the demo or
   `etfh build` plus `etfh weekly --weeks 8 --public` for real data, fails when
   `index.html` is missing, and in real mode fails when any published file
   contains the absolute data-checkout path. The site is uploaded with
   `actions/upload-pages-artifact`, including hidden files (`.nojekyll`).
2. **deploy** (`needs: build`, `pages: write` and `id-token: write`,
   `environment: github-pages`): `actions/deploy-pages` publishes the artifact
   and reports the page URL.

The repository variables and secrets are listed under Interfaces (all unset
today). Only this workflow deploys the site; the dashboard and the CLI never
publish.

### Large-addition events

`etfh.events.large_additions(con, *, min_weight=0.01, min_increase=0.005,
start=None, end=None, include_quarterly=False)` returns one row per event with
the fund, security id/name/ticker/exchange, `event_type` (`add` or `increase`),
portfolio date, `known_date`, weights before and after, delta and source kind.

- The chain is built from included `file_versions` and `holdings`, walked per
  fund in `known_at` order; `known_date` is the America/Toronto date of the
  version's `known_at` — the day the market could first know.
- An **add** is a security absent from the previous known portfolio with weight
  ≥ `min_weight`; an **increase** is `delta ≥ min_increase` and weight after ≥
  `min_weight`. A fund's first portfolio produces no events.
- A republished version compares with the version it replaces, so a correction
  does not repeat the original add; the next new portfolio date compares with
  the newest version of the latest earlier date.
- Cash, fx, derivative, balancing-line (D80c), physical-commodity (D82) and
  unmatched rows never become events. Duplicate lines for one security are
  summed first.
- Quarterly mode is off by default: with `include_quarterly=True` each fund's
  quarterly lists form their own chain, only additions count, and missing
  securities are never treated as drops — a partial list cannot show a drop.

### Prices (Tiingo, D84)

`etfh.prices` implements the `PriceSource` protocol — `daily_closes(symbols,
start, end)` returns `symbol, date, close`, sorted, with split- and
dividend-adjusted closes, and omits unknown symbols instead of raising.

- **`symbol_for(security_id, security_ticker, exchange)`** maps only US-listed
  venues (`XNYS`, `XNAS`, `XASE`, `ARCX`, `BATS`) to a ticker; class shares use
  `-` (`BRK.B` → `BRK-B`). ISIN-only, name-only, TSX-listed and
  country-composite rows resolve to `None`.
- **`cached_figi_resolver(cache_dir)`** (D90a) adds the offline fallback for the
  issuers that identify holdings by ISIN or composite FIGI (BMO, Purpose). The
  returned callable tries `symbol_for` first and, only on a miss, reads the JSON
  cache `etfh build --openfigi` already wrote (read-only, never a request):
  the security id's cached composite FIGI, then a cached `TICKER` entry for that
  composite whose venue is US (`exchCode` `US`, or a venue that maps to a US
  MIC). It returns the ticker in Tiingo form (`BRK/B`, `BRK.B` → `BRK-B`), and
  `None` when no US listing is cached or a composite resolves to more than one
  US ticker. A missing or unreadable cache leaves the answer identical to
  `symbol_for`. The index and the answers are kept in memory per resolver, so a
  run reads the cache once.
- **`TiingoPrices.from_env(raw_root, *, sources, budget_path)`** reads
  `TIINGO_API_KEY` and sends it only as an `Authorization` header. It refuses
  unless `tiingo_prices` passes the terms gate (`allowed_with_conditions`, D84),
  goes through `PoliteClient` (limiter, robots, retries, identifiable
  User-Agent), stores each response in the raw store with its sidecar and reuses
  a stored response when its recorded request window covers the asked range.
  A 404 or empty array means the symbol is absent, not an error.
- **`PriceBudget`** enforces the free-plan limits — 50 requests per hour, 1,000
  per day and 500 unique symbols per calendar month — before each request and
  raises `BudgetExceededError` without consuming budget. The state file is
  shared by every command and lives at `<raw>/../.etfh/price_budget.json`
  (`--budget` overrides), outside the tree synced to the data repository.
- **`etfh prices fetch --symbols A,B --from D --to D --raw DIR`** checks the
  terms gate before anything else (a refusal prints
  `skipped: terms source=tiingo_prices decision=…`, exits 2 and writes
  nothing), fails with a clear message when the key is unset, then prints one
  line per symbol and a summary. Exit codes: 0 success, 1 fetch error, 2 terms
  refusal, missing key or budget refusal.
- **`SyntheticPrices(seed=7, shocks=...)`** is the deterministic offline source:
  a weekday geometric random walk, the same value for a date regardless of the
  requested window, `SPY` included, and an optional per-(symbol, day) shock used
  by the tests and the demo.

### Event study (FR-3.2, exploratory)

`etfh.eventstudy.run_event_study(events, prices, *, symbol_for,
benchmark="SPY", window=(-5, 5), estimation=(-120, -11),
model="market_adjusted", bootstrap=2000, seed=7)`.

- **Calendar.** Benchmark dates define the trading-day grid. Day 0 is the first
  benchmark trading day on or after `known_date`; a day 0 more than ten calendar
  days later is a counted `window_incomplete` rather than a silently mis-dated
  event.
- **Returns.** Simple daily returns of adjusted closes. `market_adjusted` uses
  `AR = r − r_benchmark`; `market_model` estimates OLS α/β over `[-120, -11]`
  (at least 60 usable days) and uses `AR = r − (α + β r_benchmark)`; a
  zero-variance benchmark over that window is a counted `short_estimation`. The
  estimation window must end before the event window starts, so the two never
  overlap.
- **CARs.** `[-1,1]`, `[0,1]`, `[0,5]` and `[-5,5]`, reported only when each
  fits inside the requested window (never clipped), with the cross-sectional
  mean, a t-statistic from the cross-sectional standard error and a percentile
  bootstrap 95 % interval re-seeded per sub-window.
- **Accounting.** Every event is used or counted: `no_symbol`, `no_prices`,
  `short_estimation`, `window_incomplete`, `duplicate`, with
  `n_events = n_used + Σ skipped`. Events that resolve to the same symbol and
  day 0 across funds are one observation (the first in
  `known_date, fund_ticker, security_id` order), and the count is reported.
- **Outputs.** `write_outputs` writes `summary.json` (aggregates, label
  `exploratory`, model, benchmark, windows, skips, method, disclaimer),
  `car_by_day.csv` (aggregates) and `per_event.internal.csv` (security-level:
  identifiers, dates, symbol, α/β, CARs). Only the public half may be
  published.
- **CLI.** `etfh event-study --db PATH --raw DIR --out DIR [--source
  tiingo|synthetic] [--benchmark SPY] [--min-weight 0.01] [--window -5,5]
  [--model market_adjusted|market_model] [--budget PATH] [--config DIR]
  [--figi-cache DIR]`. It prints the public summary, how many events resolved
  by ticker, by cached OpenFIGI mapping and not at all, the three output paths
  and "results are exploratory". Missing prices or identifiers never raise:
  they are counted skips. `--figi-cache` defaults to the build's
  `~/.cache/etfh/openfigi`.
- **Demo resolution.** `--source synthetic` runs `SyntheticPrices` offline and
  resolves a security's own ticker on any venue, because the committed fixtures
  are TSX-listed; the command says so. The real source resolves a row by its US
  venue first and then through `cached_figi_resolver` — the build's cached
  OpenFIGI mappings, D90a — and is gated and budgeted like `prices fetch`.

### Raw-store tools

`etfh verify-raw --raw DIR` recomputes every data file's SHA-256 against its
sidecar and reports missing/orphan sidecars, malformed metadata, hash
mismatches and unexpected paths; exit 0 when clean, 1 for problems, 2 for a
missing directory. `etfh sync-raw --from SRC --to DST [--dry-run]` copies
verified data-file/sidecar pairs both-or-neither, skips identical pairs and
reports a conflict instead of overwriting different bytes; it refuses a source
that does not verify. `ops/seed_data_repo.sh` uses the same machinery to seed
the private data repository.

## Key decisions and reasoning

- **D76 — everything published or scheduled ships disabled.** A merged
  workflow that cannot run until a variable is set lets the code be reviewed
  without collecting, deploying or publishing anything before the release.
  Enabling each switch is a deliberate, auditable step.
- **D77 — raw data lives in a private repository.** The Actions collection
  writes files and sidecars to `broiisapro/cad-etf-holdings-data`; the public
  repository, its logs, summaries and artifacts carry derived aggregates and QA
  counts only. The deploy key is the only credential involved.
- **D79 — public outputs are aggregates for real funds.** `securities_disclosed`
  is true for an internal document, for a synthetic fund, or when every source
  of the fund records `terms.redistribution_allowed`; otherwise change lines
  are empty. No real source permits redistribution today, so a real-data Pages,
  demo or artifact shows counts, turnover, overlap and flow estimates only. The
  policy is enforced in the producer, again in the site parser and again in the
  published JSON, and fails closed on an unknown source id.
- **D84 — Tiingo on the free plan.** The human approved Tiingo for the event
  study with internal-use conditions; `PriceBudget` encodes the plan limits and
  the key never leaves the request header. Raw price responses stay in the raw
  store and are never committed or published; only aggregate statistics may be
  shown.
- **The digest is the contract.** Building one validated document per week and
  rendering it everywhere (site, JSON, feed, dashboard tab) means the published
  pages and the published JSON cannot disagree, and a malformed document is
  rejected before anything is written.
- **Friday-to-Friday weeks.** A fixed weekday makes the window stable and
  comparable; the cutoff is the local start of the next day, so a file fetched
  late on Friday still belongs to that week.
- **Empty weeks are dropped, not published.** A page for a week with no
  comparable portfolio is noise. The CLI prints every skipped week, publishes
  the rest, and exits 1 only when no requested week has any holding.
- **Quarterly-only funds are not in `funds`.** A weekly comparison or top-10
  concentration from a partial top-holdings list would be misleading, so those
  funds appear in `quality.funds_without_data` instead. This is the narrower
  reading of the field accepted by the orchestrator (#183).
- **Day 0 is the first trading day after `known_date`, not the portfolio date.**
  The portfolio date can precede publication by days; using `known_at` keeps
  the study free of lookahead.
- **Market-adjusted is the default.** It needs no estimation window and is the
  most transparent baseline for an exploratory study; the market model is
  available for comparison.
- **Budget state outside the raw store.** The store is synced to the data
  repository file-for-file; command state must not travel with the data, and
  one shared file is what makes the hourly/daily/monthly limits hold across
  separate invocations.

## Interfaces

All names below match the merged code.

- `etfh.digest.build_digest(...) -> dict[str, Any]`;
  `etfh.digest.validate_digest(doc) -> None`;
  `etfh.digest.default_week_end(today: date) -> date`;
  `etfh.digest.securities_disclosed(fund, sources, public=...)`.
- Digest document `schema_version: 1` (partial content):
  `week_start`, `week_end`, `known_by`, `public`, `demo`,
  `funds[]` (`ticker`, `issuer`, `name`, `securities_disclosed`, `status`,
  `portfolio_date_start`, `portfolio_date_end`, `counts`, `turnover`,
  `top10_weight_start`, `top10_weight_end`, `flow_estimate_sum`, `flow_days`,
  `flow_method`, `changes[]`), `overlap_shifts[]`,
  `quality` (`files_included`, `files_excluded`, `funds_without_data`).
- `etfh.site.render_site(digests, out_dir, *, site_title="cad-etf-holdings —
  weekly changes", base_url=None) -> list[Path]`; modules `model.py`,
  `pages.py`, `feed.py`, `format.py`, `style.py`.
- `etfh.events.large_additions(con, *, min_weight=0.01, min_increase=0.005,
  start=None, end=None, include_quarterly=False) -> pl.DataFrame` with
  `fund_ticker, security_id, security_name, security_ticker, exchange,
  event_type, portfolio_date, known_date, weight_before, weight_after, delta,
  source_kind`.
- `etfh.eventstudy`: `PriceSource` protocol, `run_event_study`,
  `EventStudyResult(per_event, car_by_day, summary)` with `to_public_json()`,
  `write_outputs(result, out_dir) -> list[Path]`.
- `etfh.prices`: `symbol_for`, `TiingoPrices`, `SyntheticPrices`,
  `PriceBudget`, `BudgetExceededError`, `CLOSE_SCHEMA`, `closes_frame`.
- `etfh.rawcheck`: `verify_raw_store`, `sync_raw_store`, `RawCheckReport`,
  `SyncReport`, `python -m etfh.rawcheck verify|sync`.
- CLI: `etfh weekly`, `etfh event-study`, `etfh prices fetch`,
  `etfh verify-raw`, `etfh sync-raw`.
- Dashboard: `etfh/dashboard/weekly.py` (`digest_for_week`, `fund_table`,
  `changes_table`, `overlap_table`, `undisclosed_funds`, `quality_label`) and
  the "This week" tab in `etfh/dashboard/app.py`.
- Workflows: `.github/workflows/pages.yml` (Saturdays 13:23 UTC, `ETFH_PAGES`
  switch) and `.github/workflows/collect.yml` (weekdays 23:17 UTC,
  `ETFH_ACTIONS_COLLECT` switch). Scripts: `ops/build_digest.sh MODE
  DATA_CHECKOUT OUT_DIR` and `ops/actions_collect.sh DATA_CHECKOUT [DATE]`.
- Repository variables: `ETFH_PAGES`, `ETFH_PAGES_REAL`, `ETFH_DATA_REPO`,
  `ETFH_ACTIONS_COLLECT`, `ETFH_USER_AGENT`, `ETFH_GIT_NAME`,
  `ETFH_GIT_EMAIL`. Secrets: `ETFH_DATA_DEPLOY_KEY`, `OPENFIGI_API_KEY`,
  `TIINGO_API_KEY` (local `.env` only).
- Environment: `TIINGO_API_KEY` documented in `.env.example`; `config/sources.yaml`
  carries `tiingo_prices` with the D84 terms record.

## Deviations

- **`archive.html` is an extra site file.** The contract lists index, weeks,
  data, feed and `.nojekyll`; the archive is required by the deliverable and is
  linked from every page. Pages serves extra files without complaint.
- **The site has its own narrow document checker.** `etfh/site/model.py` checks
  the fields it renders instead of importing `validate_digest` (the digest
  package was still in flight when it was written). It is deliberately a subset,
  and the CLI validates with `validate_digest` before rendering, so a document
  cannot reach the pages unvalidated.
- **The `.gitignore` entry is anchored (`/site/`).** An unanchored `site/`
  would also ignore `etfh/site/` and the site fixtures.
- **`--config` and `--budget` are additive CLI options.** The interface names
  `--db/--raw/--out/--source/--benchmark/--min-weight/--window/--model`; the
  two extras make the terms gate testable and the budget location explicit. No
  contracted behaviour changed.
- **The demo symbol resolution lives in the CLI.** For `--source synthetic`
  only, `_demo_symbol_for` accepts the security's own ticker on any venue
  because the fixtures are TSX-listed. The real path keeps
  `etfh.prices.symbol_for`; the command's help and its printed line say the
  numbers are simulated.
- **Digest performance is quadratic in funds.** `build_digest` calls
  `analytics.overlap` once per pair (561 pairs × 2 cutoffs on the live store,
  ~5 s); `overlap_matrix` could do it in one pass. Correct but not optimised;
  the Pages job builds eight weeks in one run.
- **No live Tiingo request was made in wave 08.** There was no
  `TIINGO_API_KEY` in the worktree; the gated path, the budget and the printing
  are covered with a fake source, and a `@pytest.mark.network` test exists for
  the real endpoint.
- **The README images still show three tabs.** The "This week" tab was added
  after the screenshots; regenerating them with `scripts/make_screenshots.py`
  is the next wave's task (D90), and the docs say so instead of implying a
  fourth tab is visible.

## Known limitations

- **A real event study still resolves nothing on today's live events.** The
  cached-mapping fallback (D90a) resolves 633 of the live store's 4,701 distinct
  non-cash security rows — the US-listed portion — but every one of the 12 large
  additions the 2026-10-27 store reports is a Canadian bond or a non-US equity,
  so a real run on that store still counts all of them as `no_symbol`. The
  fallback only knows what the build cached: a security whose US listing was
  never looked up, and a security with no US listing at all, stay unresolved,
  and the method remains demonstrable end to end only with `--source synthetic`.
- **The study is exploratory and small.** It has no size, sector or
  event-clustering controls, relies on adjusted closes from one vendor and
  reports a handful of events at best. Every output says "exploratory", and
  none of it is investment advice.
- **Tiingo's free plan is narrow.** 500 unique symbols per month, 1,000
  requests per day, US listings only, internal use only (D84). Backfilling a
  long history for many symbols needs days or a paid plan; the budget refuses
  before a limit is crossed rather than silently truncating.
- **There is no historical daily holdings panel.** Events exist only for
  portfolio dates the collection has seen: a few weeks of daily files
  (September–October 2026) plus the quarterly disclosures. Daily backfill is not
  implemented, so an event study over years is not possible yet.
- **Real publication is not enabled.** `ETFH_PAGES` is unset, so no Pages build
  runs; `ETFH_PAGES_REAL` is unset and would additionally need the private data
  checkout, the deploy-key secret and the maintainer's go-ahead. The first
  Pages deployment will render the synthetic demo.
- **The site was verified from markup and CSS, not in a browser.** Layout was
  checked at the 360 px and desktop breakpoints by reading the generated HTML/CSS
  and against a committed snapshot; no browser screenshot of the site is
  committed, so visual regressions would need one.
- **No real aggregates have been published.** D79 currently empties every
  real fund's change lines; a real-data digest therefore shows counts, turnover,
  concentration and overlap shifts only, and its usefulness depends on the
  maintainer enabling it.
- **Quarterly-only funds never appear in a digest.** They stay in
  `quality.funds_without_data`; their concentration and overlap remain a
  dashboard concern.
- **The digest recomputes from scratch.** Each run rebuilds the DuckDB file from
  the raw store before rendering; on the live store that is ~20 s for the build
  plus ~5 s per week. Acceptable for a scheduled weekly job, not instant.
