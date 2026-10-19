# Epic 01 — Pipeline core

## Goal

Epic 1 turns the Epic 0 contracts into a working pipeline: terms-gated collectors for
the approved issuer sources, parsers for daily holdings files and quarterly portfolio
disclosure PDFs, a security master with reviewed overrides, per-file validation and a QA
report, a deterministic rebuild of the DuckDB/Parquet dataset with file versions and
change detection, an as-of-aware analytics layer, a CLI covering the whole flow, the
real fund universe, and a local Streamlit dashboard. Everything is exercised end to end
on the committed synthetic fixtures; live collection runs only for sources whose
recorded terms decision allows it (D40).

## How it works

- **Registry and terms gate.** `config/sources.yaml` carries one record per source with
  the human's decision and conditions (D40: eight sources `allowed_with_conditions`,
  every other real candidate `not_allowed`, synthetic sources `allowed`). A fund may
  list several sources (`Fund.source_ids`, `;`-separated in `config/funds.csv`) with
  optional `key=value` params. `is_collectable`/`require_collectable` gate collectors
  and every `PoliteClient.get`.
- **Collection.** A: Purpose (JSON), Evolve (CSV) and BMO (XLSX, with a business-day
  fallback) daily collectors; B: Vanguard, Global X, BMO and Purpose/Evolve quarterly
  disclosure collectors that read the disclosure link from the product page. All
  requests go through `PoliteClient` (per-host limiter, robots.txt, retry/backoff,
  `Retry-After` capped at 120 s, conditional GET, gate re-check) and write raw bytes
  through `RawStore` with a SHA-256 sidecar. Synthetic sources never make requests.
- **Parsing.** `etfh/parse/synthetic_daily.py` covers the three synthetic daily shapes;
  `etfh/parse/{purpose,evolve,bmo}.py` the approved daily formats; `etfh/parse/qpd_pdf.py`
  is a generic disclosure-table parser (pdfplumber with issuer rules, camelot 2.x
  fallback) used by all quarterly sources. Every parser declares its weight unit,
  emits the `declared_unit`/`unit_check`/`stated_total_fraction`/`partial_list` notes and
  normalizes to fractions through `etfh.units`.
- **Security master.** `etfh/secmaster` validates ISIN/CUSIP check digits, normalizes
  exchange labels to ISO 10383 MICs, resolves rows to natural-key ids (provided
  identifier → CA/US CUSIP-to-ISIN derivation → MIC+ticker → reviewed override →
  null), never learns links from other files (D41), and reports unmatched rows plus
  fuzzy proposals for human review. The optional OpenFIGI client is cached, rate-limited
  and off by default.
- **Validation and QA.** `etfh/validate.py` runs the Pandera schema check and the
  business checks (numeric parsing, weight-sum tolerance per asset class or the
  document's stated total for disclosures, duplicate security, future as-of, unit
  contradiction, negative-weight reporting). Any error excludes the file from derived
  tables; `etfh/qa.py` summarizes and renders the report and returns exit code 0 only
  when every errored file is excluded and reported.
- **Build.** `etfh/build.py` performs a full rebuild from the raw store: parse, security
  master, validate, version by SHA with the earliest `known_at` (publication date when
  stated), then write `file_versions`, `holdings` (included only), `qa_events` and
  `holdings_changes`, the DuckDB file, sorted Parquet exports and the review reports.
  The same raw store always produces byte-identical Parquet.
- **Analytics.** `etfh/analytics.py` answers as-of queries using only versions known by
  `cutoff_utc(asof)` (America/Toronto midnight, D30); complete daily files beat partial
  quarterly top-holdings lists (D42), overlap excludes cash/derivative and unmatched
  ids, and concentration/breakdowns cover the rest.
- **CLI.** `etfh collect|parse|build|qa|overlap|changes` implements FR-1.11, including
  the machine-readable `overlap --json` that matches the committed expected-overlap
  fixture.
- **Universe, dashboard, ops.** F contributed the real fund universe (59 funds across
  BMO, Evolve, Global X, Purpose and Vanguard), the Streamlit dashboard that reads the
  built DuckDB read-only through `etfh.analytics`, and the launchd schedule
  (`ops/run_collect.sh`, `ops/install_launchd.sh`).

## Key decisions and reasoning

- **Synthetic fixtures first.** Every classic bug has a committed fixture: percent vs
  fraction weights, a weight-sum failure, a duplicate security, the same ticker on two
  exchanges, a republished file and two late-known files whose leak moves the SYNA/SYNB
  overlap by 0.2474 (>= 0.05), so lookahead cannot slip through silently.
- **SHA-based versions.** A version is one distinct content hash per
  `(source, fund, as_of_date)`; repeated fetches collapse to the earliest `known_at`, a
  changed file becomes version 2. This is what makes corrections auditable and
  republishing safe.
- **Completeness as the daily/quarterly switch.** Analytics decides "partial top
  holdings" from the selected file's weight sum instead of reading the sources config,
  because the analytics functions receive only the DuckDB connection; validation
  already guarantees daily files sum to 1, so the two views agree in practice.
- **Natural-key security ids.** `ISIN:`, `CUSIP:`, `TKR:<MIC>:` ids are deterministic
  across rebuilds and make the "never merge across exchanges" rule visible; fuzzy
  matches are only proposals until a human copies them into
  `config/security_overrides.csv`.
- **Fail loudly, exclude visibly.** Config errors raise at load; validation errors
  exclude the file but leave a `file_versions` row, QA events and a report line, and
  `etfh qa` encodes the "every errored file is excluded" rule in its exit code.
- **One line per fund/source in `collect`.** Operators can diff daily logs; the terms
  decision, unsupported flag and synthetic flag are checked before any collector
  lookup, so a refused source can never be contacted.

## Interfaces for future epics

All names below match the merged code on `main`.

- `etfh.build.build(raw_root, out_dir, *, config_dir=Path("config"), security_master=None, validator=None) -> BuildResult`
  writes `etfh.duckdb`, `parquet/<table>.parquet` and `reports/{unmatched,fuzzy_proposals}.csv`;
  `BuildResult` reports row/file counts.
- `etfh.analytics`: `holdings_asof`, `overlap` (→ `OverlapResult` with `common`),
  `concentration` (→ `Concentration`), `breakdown`, `overlap_matrix`, `changes`,
  `funds_available`, `FundNotFoundError`; all take a DuckDB connection and an as-of date.
- `etfh.cli`: `collect`, `parse`, `build`, `qa`, `overlap` (with `--json`), `changes`.
- `etfh.secmaster`: `SecurityMaster.assign`, `unmatched_report`, `fuzzy_proposals`,
  `load_overrides`, `FigiLookup`.
- `etfh.validate`: `ValidationConfig`, `QaFinding`, `validate_file`, `is_excluded`;
  `etfh.qa`: `summarize`, `render`, `exit_code`.
- `etfh.types` records/protocols and `etfh.schema` models/DDL are unchanged apart from
  the D45 correction.
- Dashboard entry point: `etfh/dashboard/app.py` reading `ETFH_DB`.
  Ops: `ops/run_collect.sh` (collect → build → qa) with launchd installation.

## Deviations from the plan

- Six wave-02 agents instead of the PRD's maximum of four (D43), splitting C and D along
  module boundaries; each issuer's collector and parser share one owner (D32).
- The D45 schema correction (nullable signed `shares`/`market_value`, `weight` in
  `[-1, 1]`) was required because the Epic 0 schema could not represent real files.
- `etfh parse` gained an optional `--config` and writes `parsed.parquet`; the interface
  did not pin its output.
- `changes` treats a change as observed on its `to_as_of` date, so a pair that starts
  inside the requested window but completes after it is not reported.
- Build writes the review reports with agent C's `unmatched_report`/`fuzzy_proposals`
  helpers rather than a local copy.

## Known limitations and deferred items

- **Live collection is gated and lightly exercised.** Collectors exist for all approved
  sources but real files can change shape at any time; live network tests are marked
  `network` and excluded from CI. Global X daily and Vanguard daily remain `unsupported`
  (bot challenge / JavaScript page) and are never worked around.
- **OpenFIGI is off by default.** `FigiLookup` needs a cache directory and optionally
  `OPENFIGI_API_KEY`; nothing calls it unless a caller passes an instance.
- **Fuzzy proposals require review.** They are written to `reports/fuzzy_proposals.csv`
  and only take effect after a human copies a mapping into
  `config/security_overrides.csv`; unmatched rows stay unmatched.
- **Quarterly disclosures are top holdings only**, flagged `partial` in analytics and
  labelled "top holdings only" in the dashboard; they never mix into daily series.
- **Phase 2 and 3 are not started**: shares-outstanding flows, historical backfill,
  scheduled collection in GitHub Actions, the weekly digest and the event study.
- **Raw files are never committed** and the public tree keeps code, synthetic fixtures
  and derived aggregates only (D5, D9).

## Wave 03 changes (2026-10-19)

Wave 03 was Epic 1's carryover plus the real-data integration (Epic 2). It fixes what the
first live run exposed, adds the audit/export tooling, and closes Epic 1: a live
`etfh collect → build → qa` on a fresh clone now exits 0.

### New locked-in decisions

- **D46** (corrects D37): robots.txt follows RFC 9309 — 2xx obeys the file, 3xx follows up
  to five redirects, 4xx (including 403) means no restrictions, 5xx or unreachable
  disallows all for the run. Object stores that answer a missing robots.txt with
  `403 AccessDenied` no longer block approved sources.
- **D47**: optional `etfh build --openfigi` assigns `FIGI:<compositeFIGI>` to every
  non-cash, non-derivative row whose ISIN, MIC+ticker or Bloomberg composite code+ticker
  maps through the cached `FigiLookup`; unmapped and ambiguous rows keep their D23 id.
  Off by default; default builds and the synthetic expectations are unchanged.
- **D51/D52**: duplicate lines in one file are an `error` (`duplicate_security`, file
  excluded) only when they reach the same id through different identifier forms or are
  identical rows; the same identifier repeated with different quantities is an `info`
  finding (`duplicate_lines`) and the rows stay. A daily file whose parser records
  `partial_list=true` with source evidence is not held to the sum-to-1 rule: `weight_sum`
  becomes a warning and the file is analysed as `partial`.
- **D53**: analytics priority and the `partial` flag read the build's additive
  `sources(source_id, issuer, kind, format)` table instead of weight-sum heuristics, and
  the as-of cutoff is strictly `known_at < cutoff_utc(asof)`.
- **D48/D49**: required checks stay `lint`/`types`/`tests` but use the non-strict policy;
  CI runs on one self-hosted runner whose environment forces single-threaded tools, and
  package code that opens DuckDB honours `ETFH_DUCKDB_THREADS`.
- **D50**: Epic 1 closes when the live `etfh qa` exits 0 on a fresh clone; Epic 2 closes
  after five calendar days of scheduled collection.

### Collectors and parsers (live-data fixes)

- Purpose daily now parses every holdings section, including option/derivative lines and
  the funds that publish their holdings as a section dict
  (`portfolio.holdings_data["Holding Breakdown"]`, seen live on the fund-of-funds feed);
  the genuinely unweighted commodity feed (a gold-bar quantity in ounces) stays excluded
  and reported.
- Evolve daily classifies and keys derivative lines and reads the real CSV shape; BMO
  daily handles duplicate lines and both workbook headers; the quarterly PDF parsers
  learned the real Vanguard, Global X, BMO, Purpose and Evolve layouts.
- Derivative ids are unique per instrument per fund (`DERIV:<fund>:<ticker-or-name>` with
  ordinal suffixes for genuine collisions), cash without a currency is `CASH:XXX`, and
  Bloomberg exchange codes for single exchanges map to MICs while country-composite codes
  are used only for FIGI lookups.

### Build, analytics, CLI and ops

- Every raw file the build sees gets exactly one `file_versions` row, even when it fails
  to parse, has no parser, or names an unknown source/fund; those files are excluded with
  a `qa_events` error, so `etfh qa` can list every problem file and the accounting closes.
- The build writes the additive `sources` table and records parser `partial_list` notes
  as info events; `collector_for` binds the looked-up `Source` into a copy so
  `etfh collect --config DIR` gates every request with DIR's terms record.
- `etfh spotcheck` prints deterministically sampled raw values (CSV/XLSX cells, JSON
  fields, PDF line text, including PDFs whose extracted text drops spaces) next to the
  normalized weights and the file's weight sum/stated total; `etfh export` writes derived
  aggregates only (overlap matrix, concentration, sector breakdown) with no security-level
  data. Concentration now counts unmatched security rows (they are holdings with unknown
  ids) while overlap continues to require a shared id.
- `ops/run_collect.sh` loads `.env`, requires `ETFH_USER_AGENT`, runs collect, build
  (adding `--openfigi` when `ETFH_OPENFIGI=1`) and qa, and exits with QA's status, so the
  launchd job now works and its failure signal is the QA verdict.

### QA report

`etfh qa` lists every errored file (including raw files missing from `file_versions`,
flagged `inconsistent`), reports the unmatched weight share per included file with a
per-issuer median and the worst funds, and supports `--max-unmatched-share X` to exit 3
when an included daily file exceeds it.

### Live verification

The live run on 2026-10-19 collected 70 approved-source files (BMO daily 16, Purpose
daily 12 + QPD 12, Evolve daily 6 + QPD 6, Global X QPD 8, Vanguard QPD 10), built 7,688
holdings rows, and `etfh qa` exited 0 with one excluded file (the purpose commodity feed
whose only "holding" is a physical quantity). `etfh build --openfigi` assigned 2,611
composite FIGI ids; `spotcheck` and `export` ran against the live build. Real holdings
stay outside git (D5); only derived aggregates are shareable.
