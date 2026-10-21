# Epic 02 — Real-data integration

**Status: draft — pending the 5-day scheduled-collection check (D50).** Epic 1
closed with a live `collect → build → qa` that exits 0, but most real holdings
could not be identified, so cross-issuer overlap was close to zero. This epic
makes real data usable before the scheduled run proves itself.

## Goal

Turn the live files into identified holdings: keep the exchange code that
Bloomberg-style issuers publish, give every mappable security one FIGI id space,
let a human review and approve name matches for the issuers that publish names
only, and stop rejecting good files over rounding. The pipeline, the QA report
and the dashboard must show the result, and `etfh qa` must pass on a live build.

## How it works

- **Composite exchange codes (A).** Daily parsers keep a Bloomberg code in
  `security_ticker` as `"<TICKER> <CODE>"` with a null `exchange` when the code
  is not a single venue (D57). The resolver splits the suffix: a single-venue
  code becomes a MIC (`TKR:<MIC>:<TICKER>`); a country-composite code (`US`,
  `CN`, ...) is never a venue and is used only as OpenFIGI's `exchCode`. Evolve
  files, which previously lost the code, are now mappable in FIGI mode.
- **One FIGI id space (C).** With `--openfigi` (D47, D55), every mappable
  `ISIN:` or `TKR:` id — provided, CUSIP-derived, or from a reviewed override —
  is expressed as `FIGI:<compositeFIGI>`, so ISIN issuers (BMO, Purpose) and
  ticker issuers (Evolve) share one key. Cash and derivative rows keep their
  `CASH:`/`DERIV:` ids; unmapped and ambiguous rows keep the D23 id. Lookups are
  cached on disk; default builds are byte-identical to before.
- **Reviewed name matching (C library, D CLI), D54.** The build writes
  `reports/name_proposals.csv`: one row per (unmatched normalized name,
  candidate security id) with `confidence=exact` (normalized names equal,
  countries agree when both are known, exactly one candidate id) or `fuzzy`
  (score at or above the threshold). Candidates come only from identifier-backed
  rows in the build's own raw store, never from earlier name overrides.
  `etfh review list` shows them; `etfh review approve --confidence exact` or
  `--name ...` appends global name overrides (`match_type=name`,
  `match_value=*|<normalized name>`) with the reviewer and date to
  `config/security_overrides.csv`. Nothing is applied by the build itself, and
  `--dry-run` never writes.
- **Name overrides identify name-only rows.** A global name override applies
  only when the row has no identifier evidence at all — no valid ISIN/CUSIP, no
  exchange-qualified ticker, no Bloomberg composite code. Identifier-bearing
  rows keep their own id, so approving a name can never merge a share class, an
  ADR with its ordinary line, or two bonds of one issuer. (Fund-specific name
  overrides keep their precedence: they are deliberate corrections for one
  fund's row.)
- **Rounding-aware validation (E), D56.** The weight-sum tolerance is
  `min(0.02, max(asset-class tolerance, n_rows × half of the smallest printed
  unit))`, from the parser's `weight_decimals` note. Any file over
  `1 + tolerance` is an error (`weight_sum_over_one`), daily or quarterly,
  partial or not. Duplicate lines are split from double listings (D51) and a
  source-declared partial daily list becomes a warning (D52).
- **QA (E).** The report separates daily and quarterly unmatched weight, shows
  per-issuer medians and the worst funds (only those with a positive share).
  `etfh qa --max-unmatched-share X` exits 3 when an included daily file exceeds
  the threshold; quarterly top-holdings lists are not held to it.
- **Dashboard and ops (F, D).** The dashboard adds a data-quality panel
  (as-of date, source kind, partial flag, unmatched share, excluded files) and
  honours `ETFH_DUCKDB_THREADS`. `ops/run_collect.sh` finds uv in the usual
  install locations, fails with a clear message when it cannot, loads `.env`,
  runs collect → build → qa and exits with QA's status, so the launchd job's
  failure signal is the QA verdict.

## Key decisions

- **D56 over D17/D38 for weight sums:** rounding is a property of the printed
  file, not of the issuer, so the tolerance is computed from the row count and
  the printed unit; the old fixed 0.005/0.01/0.02 tolerances remain the floor.
  This is what lets BNC's 1.0051 daily file in and still rejects BNC's 3.07
  quarterly parse.
- **D57 keeps the frozen schema:** the exchange code travels inside
  `security_ticker`; no column was added.
- **D55 expresses every mappable id as FIGI in FIGI mode,** including ids that
  came from overrides, so ticker-only and ISIN issuers can overlap. Without
  `--openfigi` the D23 ids are unchanged.
- **D54 approvals are global and carry a reviewer and date.** The file is
  append-only in effect: re-approving the same name is a no-op, and an approval
  that would change an existing reviewed id to a different security is refused.
- **Global name overrides do not rewrite identified rows** (see
  "How it works"). This is a clarification of D8/D23/D54, recorded as an
  integration fix in the wave-04 handback.

## Interfaces

- `etfh.build.build(...)` now also writes `reports/name_proposals.csv`
  (`normalized_name, example_name, country, n_funds, total_weight,
  candidate_security_id, candidate_name, score, confidence`) and reports
  `BuildResult.n_name_proposals`.
- `etfh.secmaster.names`: `normalize_name`, `infer_kind`, `normalize_country`,
  `reference_names`, `name_proposals`.
- `etfh.secmaster.review`: `approve(proposals, *, confidence, names, reviewer,
  reviewed_on) -> list[Override]`, `append_overrides(path, overrides) -> int`.
- CLI: `etfh build [--openfigi]`, `etfh qa [--max-unmatched-share X]`,
  `etfh review list|approve`, next to the existing commands.
- `etfh.qa.unmatched_shares(con)` and `QaSummary.unmatched_quality` per source
  kind; the `sources` table (D53) supplies issuer and kind.

## Deviations

- **Global name overrides are applied after identifiers,** not before (D8, D23,
  D54). Without this, the live demo changed 35 identified rows — including an
  equity share class rewritten as another class and two distinct bonds of one
  issuer — because a global override also matched rows that already had their
  own ISIN, ticker or composite code. Fund-specific name overrides still win
  over everything, as before.
- **Purpose's physical commodity feed (KILO)** is parsed as one commodity
  holding with weight 1.0 rather than excluded (#100).
- **BMO quarterly disclosures could not be exercised** in this environment:
  `bmogam.com` is unreachable from the Mac (connection error), so `bmo_qpd`
  fails closed and contributes no files. The collector and parser are unchanged
  and remain covered by the fixture look-alikes.
- The exact-class approval in this wave ran only against a temporary copy of
  `config/` (never committed); no real override was approved.

## Live verification (2026-10-21)

Fresh clone of this branch, `uv sync --frozen`, live `collect` of the approved
sources, then `build --openfigi` and `qa`:

- 70 raw files: BMO daily 16, Purpose daily 12 + QPD 12, Evolve daily 6 + QPD 6,
  Global X QPD 8, Vanguard QPD 10. 0 BMO QPD files (site unreachable).
- Build: 70 included, 0 excluded, 7,696 holdings, 3,384 FIGI ids, 1,205 unmatched
  rows, 389 name proposals. `etfh qa` exits 0 with 22 warnings (partial lists and
  stated-total deltas) and no errors.
- Proposals: 357 exact names (one candidate each) and 32 fuzzy rows for 19 names.
- Dry run in a temporary config copy: `etfh review approve --confidence exact
  --reviewer "dry run"` added 357 name overrides; the rebuild resolved 848
  unmatched rows (1,205 → 357), left the 32 fuzzy proposals in place and
  reported 4,228 FIGI rows.

Unmatched weight share, summed over each issuer's included latest versions
(median per file in brackets):

| issuer | kind | before | after exact approvals |
|---|---|---|---|
| bmo | daily | 0.01% (0.00%) | 0.01% (0.00%) |
| evolve | daily | 0.16% (0.00%) | 0.08% (0.00%) |
| purpose | daily | 100.00% (100.00%) | 31.75% (20.53%) |
| evolve | quarterly | 100.00% (100.00%) | 34.67% (38.47%) |
| globalx_ca | quarterly | 100.00% (100.00%) | 55.25% (6.80%) |
| purpose | quarterly | 100.00% (100.00%) | 52.97% (55.47%) |
| vanguard_ca | quarterly | 100.00% (100.00%) | 36.77% (8.54%) |

Representative cross-issuer overlaps (as of 2026-10-21):

| pair | issuers | before | after |
|---|---|---|---|
| PHR / ZRE | Purpose daily / BMO daily | 0.0000 | 0.1917 (7 common) |
| ESPX / ZSP | Evolve daily / BMO daily | 0.9380 | 0.9380 (496 common) |
| VFV / ZSP | Vanguard QPD / BMO daily | 0.0000 | 0.4484 (24 common) |

The remaining unmatched share is concentrated in funds whose holdings no
identifier-bearing fund holds (for example bond funds whose names carry coupons
and maturities the fuzzy matcher does not approve, and international names that
only a name-only issuer publishes). Those rows stay unmatched by design until a
human reviews them.

## Known limitations

- **The 5-day clock is pending.** Epic 2 closes only after five calendar days of
  successful scheduled collection (D50); this document stays a draft until then.
- **Fuzzy proposals are not approved.** The 19 names with several candidate ids
  (share classes, ADRs, similar issuers) need a human decision; the demo
  approves only `exact`.
- **Quarterly disclosures are top holdings only.** Their unmatched shares stay
  higher than the daily issuers' even after review, and analytics flag them
  `partial`.
- **BMO quarterly disclosures cannot be verified from this machine** (network),
  and the OpenFIGI client needs network on a cold cache (optional API key raises
  the rate limit).
- **Reviewed overrides are per name, not per security id.** A reviewed name
  approved today is evidence for every fund and every later build; there is no
  expiry. Re-reviewing a name that maps to a different security requires editing
  the reviewed CSV deliberately.
- **No real overrides are committed.** The repository keeps only the code and
  the empty reviewed-overrides header; a human approving the live proposals is
  the next step.
