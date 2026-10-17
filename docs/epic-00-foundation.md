# Epic 00 — Foundation

## Goal

Epic 0 freezes the contracts that every later workstream builds on: the uv-managed
project and quality gates, the terms-gated source registry, the polite HTTP client,
the core records and protocols, the Pandera schemas and DuckDB DDL, the raw store,
the weight-unit helpers, and a deterministic synthetic fixture set. After this epic
the four Phase 1 workstreams (collectors, parsers, security master/validation, and
build/analytics/dashboard) can work in parallel against fixed interfaces without
touching each other's files.

## How it works

- **Project and gates.** Python 3.12 is pinned with `.python-version` and a committed
  `uv.lock`. `uv sync --frozen` reproduces the environment. `ruff check`/`ruff format`,
  `mypy --strict` for `etfh`, and `pytest -q -m "not network"` are the gates. CI runs
  jobs exactly named `lint`, `types` and `tests` on the self-hosted macOS ARM64
  runners and uses the runner's own `uv` (no network installers).
- **Registry and terms gate.** `config/sources.yaml` is loaded into pydantic v2 models
  that fail loudly on invalid records. `is_collectable` is true only for a source whose
  decision is `allowed` or `allowed_with_conditions` and whose `supported` flag is true.
  `require_collectable` raises `TermsNotAllowedError` (message starts with
  `skipped: terms`) and is called both by collectors and inside every `PoliteClient.get`.
- **Collection politeness.** `PoliteClient` wraps one `httpx.Client`: identifiable
  User-Agent from `ETFH_USER_AGENT`, per-host minimum interval of
  `max(1s, robots Crawl-delay, source min_interval_seconds)`, robots.txt fetched once
  per host through the same limiter (404 allows, 5xx/unreachable disallows), conditional
  requests, and tenacity retries with exponential backoff and jitter that honour
  `Retry-After` (4 attempts maximum). Clock and sleep are injectable, so the tests
  never sleep and never touch the network.
- **Raw store.** `RawStore(root)` stores files unmodified under
  `<root>/<issuer>/<ticker>/<YYYY-MM-DD>/<fetched_at>.<ext>` (UTC `YYYYMMDDTHHMMSSZ`)
  with a `.meta.json` sidecar. Re-fetching identical bytes for the same
  issuer/ticker/date writes nothing and returns the existing `RawFile`.
- **Schemas.** `pandera.polars` models define `ParsedHoldings`, `Holdings`,
  `FileVersion`, `QaEvent` and `HoldingsChange`; `DDL_*` constants create the same
  tables in DuckDB, and a test compares column names and order programmatically.
- **Units.** Parsers declare a `WeightUnit`; `normalize_weights` converts to fractions,
  and `check_unit` reports a declared-unit contradiction instead of guessing.
- **Fixtures.** `etfh.synthetic` deterministically generates a raw store plus
  `manifest.json` and `expected_overlap.json` for the seven synthetic funds, covering
  every Phase 1 classic bug offline.

## Key decisions and reasoning

- **Polars 2.x, explicit sorts.** Polars 2 lazy `collect()` is streaming and does not
  keep row order after joins/group-bys, so every table `build` writes must be sorted
  explicitly (D15). `RawStore.iter_raw()` already returns a deterministically ordered
  list for the same reason.
- **Strict schemas, explicit dtypes.** Validation does not use model-level `coerce`
  because that would also hide genuine type errors. Parsers must give nullable columns
  their real dtype (for example `pl.String` for an all-null `cusip`). Weight is
  constrained to `[0, 1]`, which rejects NaN and infinities under Polars comparison
  semantics; sum checks belong to `validate.py`.
- **Basis-point fixture weights.** Synthetic weights are integer basis points, so every
  file sums exactly (percent and fraction renderings are both exact) and the expected
  overlap is computed with `Decimal` from the generator's own ground truth. The overlap
  JSON stores decimal strings, so later float comparisons cannot silently drift.
- **Deterministic binary fixtures.** openpyxl stamps the current time into the XLSX
  archive and overwrites `modified` at save time, so the generator rewrites the archive
  with fixed entry timestamps and a fixed `docProps/core.xml`; the PDF uses reportlab
  `invariant=1` with fixed metadata. The generator never reads the wall clock.
- **Structural `RawStore` protocol.** `etfh/types.py` declares the structural `RawStore`
  protocol (only `write`) so `Collector` annotations type-check before the concrete
  store exists; `etfh/storage.py` implements the same signature.
- **Terms first.** All 16 real candidate sources ship as `pending_review` with null
  reviewer/date, so nothing real can be fetched until a human records a decision
  (D3). The synthetic sources are the only collectable entries.
- **CA/US identifier linkage in fixtures.** A CUSIP-only row whose ISIN is observed
  elsewhere in the fixture set resolves to `ISIN:<isin>` per D23; ticker-only rows
  resolve to `TKR:<MIC>:<ticker>`, so the same ticker on two exchanges stays distinct.

## Interfaces for future epics

All names and signatures below match the merged code on `main`.

### `etfh.types`

- Enums: `Decision` (`allowed`, `allowed_with_conditions`, `not_allowed`,
  `pending_review`), `SourceKind` (`daily_holdings`, `quarterly_disclosure`,
  `synthetic`), `FileFormat` (`csv`, `xlsx`, `json`, `pdf`), `AssetClass`
  (`equity`, `fixed_income`, `balanced`, `commodity`, `other`).
- `RawFile` (frozen dataclass): `source_id`, `issuer`, `fund_ticker`,
  `as_of_hint: date | None`, `fetched_at: datetime` (UTC-aware), `url`,
  `path: Path`, `sha256`, `content_type`, `ext`.
- `ParsedFile` (dataclass): `raw: RawFile`, `as_of_date: date`,
  `published_at: datetime | None`, `frame: polars.DataFrame`,
  `notes: list[str]`. Weights in `frame` are fractions; the parser declares its
  source unit.
- `Collector` protocol: `source_id: str`;
  `fetch(fund: Fund, day: date, client: PoliteClient, store: RawStore) -> list[RawFile]`.
  A collector never bypasses `PoliteClient`.
- `Parser` protocol: `formats: frozenset[FileFormat]`;
  `can_parse(raw: RawFile) -> bool`; `parse(raw: RawFile) -> ParsedFile`.
- `RawStore` protocol: `write(content, *, source_id, issuer, fund_ticker, url,
  fetched_at, ext, content_type="application/octet-stream", as_of_hint=None,
  http_status=None, etag=None, last_modified=None) -> RawFile`.

### `etfh.registry`

- Models: `TermsRecord` (`url`, `reviewed_on`, `reviewer`, `decision`, `conditions`,
  `min_interval_seconds`, `redistribution_allowed`), `Source` (`id`, `issuer`, `kind`,
  `format`, `url_patterns`, `update_frequency`, `supported`, `notes`, `terms`),
  `Fund` (`ticker`, `issuer`, `name`, `asset_class`, `source_id`, `exchange`
  defaulting to `XTSE`). Models forbid unknown fields.
- `load_sources(path: str | Path) -> dict[str, Source]` and
  `load_funds(path: str | Path, sources: Mapping[str, Source]) -> list[Fund]`.
  Both fail loudly on duplicates, unknown source ids, unknown fields and invalid
  decision records.
- `is_collectable(source: Source) -> bool`;
  `require_collectable(source: Source) -> None` raising `TermsNotAllowedError`
  (a `PermissionError`) whose message starts with `skipped: terms` and names the
  source and decision.
- Committed config: `config/sources.yaml` (16 candidate sources `pending_review`,
  4 allowed synthetic sources, `syn_blocked` `not_allowed`), `config/funds.csv`
  (`SYNA`–`SYNX`, all synthetic).

### `etfh.http`

- `PoliteClient(user_agent: str | None = None, *, client: httpx.Client | None = None,
  clock: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None)`.
  The User-Agent falls back to `ETFH_USER_AGENT`; construction fails unless it contains
  `@` or `http`. `close()` closes only a client the instance created; the class is a
  context manager.
- `get(url, *, source: Source, etag: str | None = None, last_modified: str | None = None)
  -> httpx.Response`: re-checks the terms gate first, obeys robots.txt, throttles per
  host, sends conditional headers, retries 429/5xx/timeouts (4 attempts) and raises
  `HTTPRetryError(url, status_code)` when retryable responses run out;
  `RobotsDisallowedError` when robots.txt forbids the URL.
- Tests use `httpx.MockTransport` plus the injectable clock/sleep; nothing in the
  suite sleeps or reaches the network.

### `etfh.schema`

- `ParsedHoldings`: `fund_ticker`, `as_of_date` (Date), `security_name`,
  `security_ticker` (nullable), `exchange` (nullable MIC), `isin` (nullable, 12 chars),
  `cusip` (nullable, 9 chars), `asset_class`, `sector`, `country`, `currency`
  (nullable), `weight` (Float64 in `[0, 1]`), `shares`, `market_value` (nullable),
  `source_id`, `raw_file_sha256` (64 lowercase hex).
- `Holdings` = the same columns plus `known_at` (UTC Datetime), `file_version`
  (Int32 >= 1) and nullable `security_id`, in the architecture's column order.
- `FileVersion` (`source_id`, `fund_ticker`, `as_of_date`, `file_version`, `known_at`,
  `raw_file_sha256`, `path`, `status` in `included`/`excluded`).
- `QaEvent` (`raw_file_sha256`, `source_id`, `fund_ticker`, `as_of_date` nullable,
  `check`, `severity` in `error`/`warning`/`info`, `detail`, `excluded`).
- `HoldingsChange` (`fund_ticker`, `from_as_of`, `to_as_of`, `security_id` nullable,
  `change_type` in `add`/`drop`/`weight_change`, `weight_before`, `weight_after`,
  `delta`).
- `DDL_FILE_VERSIONS`, `DDL_HOLDINGS`, `DDL_QA_EVENTS`, `DDL_HOLDINGS_CHANGES` match
  the models' column names and order.

### `etfh.storage`

- `RawStore(root: str | Path)`, layout
  `<root>/<issuer>/<ticker>/<YYYY-MM-DD>/<fetched_at>.<ext>` with the sidecar
  `<fetched_at>.meta.json` (`url`, `source_id`, `issuer`, `fund_ticker`, `as_of_hint`,
  `fetched_at` ISO-8601 UTC, `sha256`, `content_type`, `http_status`, `etag`,
  `last_modified`, `bytes`). The directory date is the as-of hint when known, else the
  fetch date. `fetched_at` must be timezone-aware.
- `write(...) -> RawFile` (same signature as the protocol) writes bytes unmodified via
  a temp file and atomic replace; identical SHA-256 for the same issuer/ticker/date is
  deduplicated and returns the existing `RawFile`.
- `iter_raw() -> list[RawFile]` ordered by issuer, ticker, as-of date, fetched_at.
- `read_meta(path) -> dict[str, Any]` (also available as a method).

### `etfh.units`

- `WeightUnit` (`percent`, `fraction`).
- `parse_number(text: str | None) -> float | None`: thousands separators, trailing
  percent (not applied), parentheses as negative, unicode minus, `-`/empty/`N/A`
  null tokens, unparseable and non-finite values as `None`.
- `normalize_weights(values, declared: WeightUnit) -> pl.Series` returns fractions.
- `check_unit(values, declared: WeightUnit, tolerance: float = 0.01) -> UnitCheck`
  with `declared`, `total`, `expected`, `tolerance`, `consistent`, `suggested`; never
  changes the declared unit. `UnitCheck.detail()` is the QA-event text.

### `etfh.synthetic`

- `generate(out: Path) -> Manifest`: writes the raw store under `out` through
  `RawStore` and `manifest.json` plus `expected_overlap.json` next to it.
  `python -m etfh.synthetic --out DIR` does the same.
- `ground_truth_holdings(ticker: str, as_of: date, known_before: datetime | None = None)
  -> dict[str, Decimal]`: resolved `security_id` to fraction weights for the version
  used as of `as_of`; `known_before` defaults to the start of the next calendar day in
  America/Toronto (D30).
- `Manifest` fields: `funds`, `bad_files`, `republished_file`, `lookahead_files`,
  `mapl_pair`, `percent_fraction_pair`, `pdf_stated_total`, `expected_overlap`.
  Committed fixtures live in `tests/fixtures/raw_synthetic/**`,
  `tests/fixtures/manifest.json` and `tests/fixtures/expected_overlap.json`.

### `etfh.cli`

- Typer app with commands `collect`, `parse`, `build`, `qa`, `overlap`, `changes`;
  each currently prints `not implemented yet` to stderr and exits 2. Workstream D
  replaces the bodies without renaming the commands.

## Deviations from the plan

- The `RawStore` protocol in `etfh/types.py` covers only `write`, which is all the
  `Collector` protocol needs; `iter_raw` and `read_meta` exist on the concrete class
  only.
- `iter_raw()` returns a list rather than a generator so the deterministic order is a
  property of the return value itself.
- The mypy per-module `ignore_missing_imports` list (from issue #1) was extended with
  `openpyxl` and `reportlab.*`, which ship no type information.
- The `known_at` columns in `Holdings` and `FileVersion` are annotated as
  `pl.Datetime("us", "UTC")` with a `# type: ignore[valid-type]`; Polars accepts the
  instance at runtime (it is the documented way to require a UTC-typed column) but mypy
  wants a type expression.
- SYNC is declared as a fraction CSV so that "sums to 0.85" reads directly; the issue
  did not fix its unit. SYNF is a genuine partial list, so the weight-sum-to-1 rule is
  replaced by a stated-total-equals-table-sum check for that file, per its issue text.
- Expected overlap weights and the overlap value are decimal strings in the JSON, not
  floats, to keep exact-decimal arithmetic auditable.
- `.env.example` and the registry require a contact-bearing `ETFH_USER_AGENT`; the CI
  workflow uses `actions/checkout@v7` (current major) and the runner's Homebrew `uv`.

## Known limitations and deferred items

- **CLI is a stub.** All six commands exit 2 until workstream D implements them.
- **No real collection.** Every real candidate source is `pending_review`, so
  `require_collectable` refuses them. Collection starts only after the human records
  terms decisions and `config/sources.yaml` is updated by an issue citing them (D28).
- **No collectors, parsers, security master, validation, build, analytics or
  dashboard.** Epic 0 delivers the contracts only.
- **No live network tests.** The `network` marker is registered and deselected by
  default but unused; live checks are deferred to the integration epic.
- **OpenFIGI client not implemented** (`OPENFIGI_API_KEY` documented as optional in
  `.env.example`).
- **`ops/launchd/com.etfh.collect.plist` not created**; scheduling is an Epic 1 item.
- **Real fund universe not configured.** `config/funds.csv` contains only the seven
  synthetic funds.
- **Required status checks are not yet enabled on `main`** (D13); the orchestrator adds
  them once this CI workflow has reported on a real PR.
- **QPD parsing is not implemented**, so the SYNF cross-check exists only in the
  fixture tests; real parsers must implement the stated-total check (D17).
- **The raw store protocol does not constrain read APIs**, so collectors should import
  `RawStore` from `etfh.storage` for the concrete methods.
