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
