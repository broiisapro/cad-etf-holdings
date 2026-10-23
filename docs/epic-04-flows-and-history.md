# Epic 04 — Flows and disclosure history

## Goal

Epic 4 is Phase 2 of the PRD: turn the point-in-time holdings dataset into a flow
and history dataset.

- **FR-2.1 — estimated creation/redemption flows.** Collect daily fund metrics
  (NAV per unit, shares outstanding, AUM) from the issuers the human approved on
  2026-10-23 (D65) and estimate daily flows as `(Δshares) × NAV`, with the
  method stated in every output (D67).
- **FR-2.2 — historical backfill.** Walk each approved issuer's quarterly
  portfolio disclosures backwards for the last N calendar quarters, store what
  the issuer still publishes, and report every requested quarter as fetched,
  already present, or missing with a reason (D68).

FR-2.3 — moving the schedule to GitHub Actions — is deliberately **not** part of
this epic. D69 splits Epic 4 by time: wave 06 delivers FR-2.1 and FR-2.2 while
Epic 2 waits for its collection days; FR-2.3 waits for the public ship, and the
`epic-04-complete` tag follows only after it.

## How it works

### Metrics sources and collection

`SourceKind.fund_metrics` (D66) marks a source that publishes fund-level numbers
rather than holdings. Four sources exist, all under the D65 terms decision
(internal research use, no raw redistribution, polite rate, identifiable
User-Agent):

| Source | What it publishes | How it is collected |
|---|---|---|
| `bmo_metrics` | ETF factsheet PDF: month-end net assets and shares outstanding; NAV per unit derived | One PDF per fund through `PoliteClient` |
| `evolve_metrics` | Product page HTML: NAV per unit and total net assets as of the NAV date; units derived (`aum / nav`) and noted | Conditional GET of the product page |
| `purpose_metrics` | NAV table and `details.aum` inside the `purpose_daily` JSON | **No collector, no extra fetch**: the build parses the already-stored `purpose_daily` files |
| `vanguard_ca_metrics`, `globalx_ca_metrics` | No static, non-JavaScript source exists | `supported: false`; never fetched |

`etfh collect-aum` mirrors `collect`: it iterates the fund universe, keeps only
`fund_metrics` sources, applies the terms/unsupported/synthetic gate **before**
looking a collector up, and uses the collector bound to the `Source` from the
active config (so `--config DIR` gates every request with DIR's terms record).
For `purpose_metrics` it prints one `derived: purpose_metrics from purpose_daily`
line instead of fetching. Every other fund/source pair is logged exactly like
`collect` (`fetched …`, `unchanged …`, `error …`).

### Data model and build

The metrics contracts are additive; the frozen holdings schema is untouched.

- `etfh.schema.ParsedFundMetrics` — parser output: `fund_ticker`, `as_of_date`,
  nullable `nav_per_unit` (> 0), `shares_outstanding` (≥ 0), `aum` (≥ 0),
  `currency`, `source_id`, `raw_file_sha256`.
- `etfh.schema.FundMetrics` — the same plus `known_at` (UTC) and
  `file_version` (≥ 1); `DDL_FUND_METRICS` creates the `fund_metrics` table in
  the model's column order.
- One row is one fund's numbers on one as-of date. A source may publish a whole
  history in one file (a Purpose NAV table), so one file can yield many rows.
  Every figure is nullable and is **never** forward-filled or defaulted to zero.

The build processes every raw file twice: through the holdings path and through
`find_metrics_parsers`, which returns every metrics parser whose `can_parse`
matches — including a parser for a **different** source than the file's
(`purpose_metrics` reads `purpose_daily` files). Metrics sources are routed out
of the holdings path, so a metrics file never becomes a holdings file with "no
parser", and a metrics failure never excludes a holdings file or vice versa.
Versioning follows the D20 rules keyed on the metrics source id: the same SHA
adds nothing, a republished file for the same fund and as-of date increments
`file_version`, and `known_at` is the earliest fetch that produced the hash
(publication date when a parser states one).

`etfh.qa` gains a **fund metrics** section: metrics inputs included/excluded,
funds with metrics, the latest metrics date per issuer, and findings. Metrics
errors are reported like holdings errors and keep the same exit-code rules.
`etfh qa --asof D` replaces the unmatched-weight section with the versions a
point-in-time query at `D` would use (`unmatched_shares(con, asof)`).

### Flows (FR-2.1, D67)

`etfh.analytics.metrics_asof(con, fund, asof)` returns one row per as-of date —
the latest version known by `asof`; a republished file becomes visible only at
its own `known_at`. `etfh.analytics.flows(con, fund, start, end, asof=…)`
returns one row per metrics date in `[start, end]`:

```
flow_estimate = (shares_outstanding − prev_shares) × nav_per_unit
```

- `shares_outstanding` and `nav_per_unit` come from the **same** row; no
  cross-date combination.
- `prev_shares` is the shares figure of the previous as-of date that has one.
  A date without a share count is skipped when looking back, never filled from
  an older date and never carried forward.
- `gap_days` counts weekdays between the two dates, so a normal weekend is 1
  and a missing business day is more than 1; it is null when there is no
  previous date (the first row).
- `asof` defaults to `end`: the whole series uses only versions known by then.
- Every row carries `method = "Δshares × NAV (estimate; ignores distributions
  and in-kind effects)"`; the CLI prints it as a footer and the JSON as a
  top-level field even when there are no rows.

`etfh flows --fund F --from D1 --to D2 --db PATH [--asof D] [--json]` prints the
table or the JSON payload (`fund`, `start`, `end`, `asof`, `method`, `rows`),
with 4-decimal strings for shares/NAV and 2-decimal strings for the flow.

### Backfill (FR-2.2, D68)

`etfh.backfill.backfill_qpd(issuer, quarters, raw_root, *, config_dir, client,
today, funds)`:

1. Resolves the issuer to an approved, supported `quarterly_disclosure` source;
   an unknown issuer or one without an approved source raises `BackfillError`
   (the CLI exits 2).
2. Computes the last `quarters` calendar quarter-ends strictly before today in
   America/Toronto.
3. For each fund configured for that source, treats a stored PDF whose stated
   portfolio date is within seven days of the quarter-end as `present` (no
   request).
4. Otherwise calls the collector's `fetch_historical(fund, quarter_end, client,
   store)`; a returned file is `fetched`, no file or a `CollectError` is
   `missing` with the reason.

Every request goes through `PoliteClient`, and discovery uses only links the
issuer itself publishes (product-page document lists or a documented stable URL
pattern verified on the current quarter). The command prints one line per
fund × quarter (`fetched|present|missing`) and exits 0 even when quarters are
missing.

### CLI and operations

| Command | Purpose |
|---|---|
| `etfh collect-aum --date … --raw DIR [--config DIR]` | Collect `fund_metrics` sources only |
| `etfh flows --fund F --from D1 --to D2 --db PATH [--asof D] [--json]` | Flow estimates plus the D67 method line |
| `etfh backfill-qpd --issuer I --quarters N --raw DIR [--config DIR]` | Quarterly disclosure backfill with per-quarter status |
| `etfh qa --db PATH [--asof D] [--max-unmatched-share X]` | QA report, now including fund metrics and an as-of weight view |

`ops/run_collect.sh` now runs `collect → collect-aum → build → qa`, and
`ops/collect_guard.sh` is the sleep-proof daily trigger (D64): launchd polls it
every 30 minutes and at login, it collects at most once per local day after
18:00 (or immediately after a missed day), and it stamps the day only when at
least one source/fund pair was fetched or confirmed unchanged. State lives in
`<repo>/.cache/etfh/`; the guard log is
`~/Library/Logs/etfh/guard.log`.

## Key decisions and reasoning

- **D65 — approved metrics sources.** The human approved daily fund metrics for
  BMO, Evolve and Purpose on 2026-10-23 under the same conditions as the
  holdings sources. Vanguard and Global X metrics exist in the config only as
  `supported: false`, because both product pages are JavaScript/bot-protected
  and no static file exists. The source notes record the evidence.
- **D66 — additive contracts.** `SourceKind.fund_metrics`, `ParsedFundMetrics`,
  `FundMetrics` and the `fund_metrics` table are new; the frozen holdings schema
  is unchanged. This keeps existing builds, Parquet exports and synthetic
  expectations byte-identical.
- **D67 — the flow formula.** `(S_t − S_prev) × NAV_t` is the simplest defensible
  implied-flow estimate from public data. Mixing dates or forward-filling would
  invent flows on days without a share count, so a missing previous share count
  yields no flow and a longer gap is flagged. The method label is printed on
  every row, footer and JSON payload because the estimate ignores distributions
  and in-kind effects.
- **D68 — conservative backfill.** Only approved PDF sources, only links the
  issuer publishes, and every quarter accounted for. A quarter that is no longer
  online is `missing`, not guessed from a URL pattern.
- **D69 — time-split epic.** FR-2.1/FR-2.2 ship now; FR-2.3 (GitHub Actions
  scheduling) waits for the public ship, so the epic is tagged only after it.

## Interfaces

All names match the merged code.

- `etfh.types.SourceKind.fund_metrics` (`"fund_metrics"`).
- `config/sources.yaml`: `bmo_metrics`, `evolve_metrics`, `purpose_metrics`,
  `vanguard_ca_metrics` and `globalx_ca_metrics` (unsupported), each with a D65
  terms record. `config/funds.csv` lists the matching metrics source in each
  fund's `source_ids`.
- `etfh.schema.ParsedFundMetrics`, `etfh.schema.FundMetrics`,
  `etfh.schema.DDL_FUND_METRICS`.
- `etfh.parse.registry.find_metrics_parsers(raw: RawFile, source: Source) ->
  list[Parser]` — every metrics parser that claims the file, regardless of the
  file's source.
- `etfh.build.build(..., metrics_validator=None)`; `BuildResult.n_fund_metrics`.
- `etfh.analytics.metrics_asof(con, fund, asof) -> pl.DataFrame`;
  `etfh.analytics.flows(con, fund, start, end, *, asof: date | None = None) ->
  pl.DataFrame`; `etfh.analytics.FLOW_METHOD`.
- `etfh.validate.validate_metrics(parsed, *, source, config) -> list[QaFinding]`
  with `schema`, `nav_positive`, `shares_nonnegative`, `aum_consistency`
  (warning), `future_as_of`, `duplicate_date`.
- `etfh.qa`: fund-metrics section in `summarize`/`render`; `unmatched_shares(con,
  asof=None)`.
- `etfh.backfill.backfill_qpd(...) -> list[BackfillResult]` and
  `BackfillResult(fund_ticker, quarter_end, status, detail, raw)`.
- `etfh.cli`: `collect-aum`, `flows`, `backfill-qpd`, `qa --asof`.
- `ops/collect_guard.sh`, `ops/run_collect.sh`, `ops/launchd/com.etfh.collect.plist`
  (`StartInterval 1800`, `RunAtLoad`), `ops/install_launchd.sh`.

## Deviations from the plan

- **FR-2.3 is deferred, not delivered.** GitHub Actions scheduling has to wait
  for the repository to be public (D69); the local launchd guard remains the
  production schedule until then.
- **BMO metrics are PDF factsheets, not the profile JSON endpoint.** A live check
  on 2026-10-23 found the profile endpoint returns a JavaScript shell with no
  fund data, so the collector uses the factsheet PDFs the issuer links; the
  source note records that. The factsheet publishes net assets and shares;
  NAV per unit is derived as `net assets / shares` and noted.
- **Evolve shares are derived.** The product page publishes NAV and total net
  assets but not units outstanding; the parser sets
  `shares_outstanding = aum / nav` and records `shares_derived=aum/nav`.
- **`etfh qa --asof` groups in the CLI.** `qa.summarize` has no `asof`
  parameter (E's API), so the CLI rebuilds only the unmatched-weight section
  from the public `unmatched_shares(con, asof)` frame and the public
  `QaSummary`/`UnmatchedShare`/`UnmatchedQuality` types, then renders the usual
  report. Exclusions and warnings remain store-wide facts.
- **The build reports metrics rows.** `etfh build` prints `fund metrics N rows`
  next to the holdings count, an additive output change needed to audit Phase 2
  runs.
- **`config/funds.csv` kept its holdings order.** Metrics sources were added to
  existing `source_ids`; no new fund rows were invented.

## Live verification (2026-10-23)

The Phase 2 pipeline was exercised against the live approved sources from this
checkout (real files stay in the gitignored `./raw`; only aggregate lines are
quoted here):

```
collect-aum: 21 files fetched (15 bmo_metrics factsheet PDFs, 6 evolve_metrics
             product pages); 1 derived purpose_metrics line;
             1 error: bmo_metrics ZQQ has no factsheet at the derived URL
collect:     76 files fetched (bmo_daily 16, evolve_daily 6, evolve_qpd 6,
             globalx_ca_qpd 8, purpose_daily 12, purpose_qpd 12,
             vanguard_ca_qpd 10); 15 unchanged; 6 synthetic and 1 terms-refused
             source skipped; bmo_qpd unavailable because robots.txt for
             bmogam.com was unreachable during the run (D46: disallow for the
             run), reported per fund
             a second run after the metrics-skip fix: 20 fetched, 50 unchanged,
             0 purpose_metrics lines, only the bmo_qpd robots errors remain
build:       457 file versions (456 included, 1 excluded), 7,689 holdings rows,
             387 fund_metrics rows, 1,910 change rows, 87 QA events,
             1,144 unmatched rows
qa:          exit 0; 70 files (69 included, 1 excluded: purpose_daily BNC, whose
             weights sum to 1.0064 and exceed 1 + tolerance); fund metrics:
             39 inputs included, 0 excluded, 33 funds with metrics, 0 findings;
             latest metrics date bmo 2026-09-30, evolve 2026-10-22,
             purpose 2026-10-22
flows PDF:   15 dated rows for 2026-10-01..2026-10-22; only the latest row
             carries a share count because Purpose publishes a NAV history but
             a share count only for the current date, so no flow estimate
             exists yet; the method line is printed
flows ZSP:   1 row at the factsheet's stated date with a share count and a
             derived NAV; no previous shares date, so no flow estimate yet; the
             method line is printed (individual fund figures are not repeated
             in committed docs)
qa --asof:   exit 0; --asof 2026-10-22 reports nothing known yet (all files were
             fetched on 2026-10-23), --asof 2026-10-23 restores the full section
backfill vanguard_ca: 0 fetched, 10 present, 70 missing (80 fund-quarters):
             Vanguard links only its current disclosure, so older quarters are
             reported missing with that reason
backfill globalx_ca:  16 fetched, 8 present, 40 missing (64 fund-quarters): the
             product pages list 2024 Q1, 2025 Q1, 2025 Q3 and 2026 Q1 only, so
             the linked older quarters were downloaded and the rest reported
             missing with the listed quarters in the reason
build after backfill: 473 file versions (470 included, 3 excluded), 7,846
             holdings rows; qa exit 0 with the 3 excluded files reported
final build (after the second collect): 535 file versions (531 included,
             4 excluded: two BNC versions of one file, CASH and HMMJ), 7,856
             holdings rows, 447 fund_metrics rows, 41 metrics inputs, 33 funds;
             qa exit 0
```

Two findings are source-side and are recorded rather than worked around:

- **One BMO factsheet is missing** (`bmo_metrics` ZQQ): the URL derived from the
  fund name returns no file; the collector reports the exact URL.
- **BMO quarterly disclosures were unreachable** because `bmogam.com` served no
  usable robots.txt during the run; per D46 the run disallowed that host and
  reported one line per fund. BMO daily holdings come from `df.bmogam.com`,
  which was unaffected.

Only one metrics date per fund existed on the first day of collection, so the
live flow estimates are intentionally absent (no `prev_shares`) while every
output still shows the method. The synthetic end-to-end test
(`tests/test_e2e_phase2.py`) proves the computed values on a two-day history.

## Known limitations and deferred items

- **One BMO factsheet is missing.** `bmo_metrics` could not find a factsheet for
  ZQQ at the URL derived from its `funds.csv` name on 2026-10-23; the collector
  reports the exact URL and the fund is simply absent from `fund_metrics`. The
  fund name or URL pattern needs an owner's follow-up.
- **BMO factsheets can lag.** The live run stored the factsheet BMO currently
  serves for each fund; two were noticeably old (ZWB `2024-12-31`, ZST
  `2025-06-30`). The pipeline records the date the document states rather than
  the fetch date; when a newer factsheet appears, the flow for that fund will
  span the gap and `gap_days` will show it.
- **BMO quarterly disclosures were unreachable on 2026-10-23.** `bmogam.com` did
  not return a usable robots.txt during the live run; D46 disallows the host for
  that run, and each BMO fund-quarter is reported as an error. Retrying later is
  the only remedy — the terms and robots rules are not bypassed.
- **Metrics history starts at the first collection** for BMO and Evolve (their
  files state one date each); Purpose's NAV table carries its own NAV history
  but a share count only for the current date, so a flow needs at least two
  collection days before `prev_shares` exists. Backfilled holdings history does
  not create metrics history.
- **Flows ignore distributions and in-kind activity.** The estimate is
  `Δshares × NAV`; a distribution that changes units outstanding, an in-kind
  creation or a fee rebate is not separated. This is why the method label is
  mandatory.
- **Backfill depends on what issuers still list.** Older quarters that have
  disappeared from a product page are reported `missing`; they are not
  reconstructed from guessed URLs.
- **Two backfilled Global X PDFs failed validation.** `CASH` and `HMMJ` for
  2025 Q3 parsed to 1.1485 and 1.0094 of the stated 1.0 total respectively, so
  they were excluded and reported by `etfh qa` rather than included with wrong
  weights. The disclosure parser (owned by the QPD workstream) needs the older
  table layout; the raw files stay in the store for a later retry.
- **No metrics for Vanguard or Global X.** Their product pages have no static
  source and were marked unsupported under D65 rather than worked around with a
  browser.
- **FR-2.3 (GitHub Actions scheduling) is deferred** until the public ship, as
  is the `epic-04-complete` tag.
