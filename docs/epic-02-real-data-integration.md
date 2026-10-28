# Epic 02 — Real-data integration

**Status: complete (2026-10-28).** Epic 1 closed with a live `collect → build
→ qa` that exits 0, but most real holdings could not be identified, so
cross-issuer overlap was close to zero. This epic made real data usable. It
closes on the five-day scheduled-collection criterion (D50): the daily issuers
collected on five calendar days and the fresh-clone DoD passes (D83). Tag
`epic-02-complete` at `6fcb898`.

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
- **Approval and commit flow (D58).** The human approves against the live
  build; `scripts/approve-names.sh` runs `etfh review approve` with the
  reviewer's name and today's date and writes the result to
  `.orchestration/approvals/security_overrides.csv`, outside git. An agent then
  commits that file byte-for-byte to `config/security_overrides.csv` through a
  normal PR that cites the approval file, the reviewer and the date. Agents
  never approve matches or edit a row themselves; the committed CSV is the
  approval artifact, and the build reads it like any other reviewed override.
  The first approval (2026-10-22, reviewer Moksh Siruvani) added 357 global
  exact-name overrides.
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

The tables above are a 2026-10-21 dry run against a temporary config copy. The
357 exact-name overrides were approved on 2026-10-22 and are now committed;
the next live build applies them for real.

## Final numbers (2026-10-28)

Fresh build of the live raw store from the main checkout (read-only; output
under `/tmp`, nothing committed), with the committed overrides and the cache
described below. These are counts and shares only.

**Collection.** Five local collection days: 2026-10-21, 10-23, 10-24, 10-26 and
10-27 (America/Toronto). `bmo_daily` and `purpose_daily` have files from all
five; `evolve_daily` from three (10-21, 10-23, 10-27). `purpose_daily` also
served a 2026-09-30 file, and the quarterly disclosures in the store are all
2026-03-31 (Purpose one earlier quarter-end).

**Raw store and versions.** 202 data files with 202 sidecars:
`bmo` 93, `purpose` 61, `evolve` 30, `vanguard_ca` 10, `globalx_ca` 8.
`etfh verify-raw` checks all 202 pairs with no problem. The build turns them into
1,672 `file_versions` rows — 202 distinct paths, because each of the 49 Purpose
NAV files yields ~30 fund/date series for `purpose_metrics` — split as
`bmo_daily` 78, `purpose_daily` 49, `evolve_daily` 18, `purpose_qpd` 12,
`vanguard_ca_qpd` 10, `globalx_ca_qpd` 8, `evolve_qpd` 6, `bmo_metrics` 15,
`evolve_metrics` 6 and `purpose_metrics` 1,470.

**Build.** 1,672 versions, **all 1,672 included, 0 excluded**; 32,120 holdings
rows; 1,491 `fund_metrics` rows (33 funds with metrics); 2,351 change rows;
179 QA events; 3,113 unmatched rows and 584 name proposals in the default build.
The same store built with `--openfigi` (cached lookups only) resolves 17,309 rows
to composite FIGI ids and leaves 764 unmatched rows and 61 name proposals.

**QA.** `etfh qa` exits 0: 181 holdings files, 181 included, 0 excluded, 51
warnings; 70 metrics inputs, 70 included, 33 funds, no findings. The 51
warnings are weight-sum findings (37) and quarterly stated-total deltas (14);
the parser notes are separate `info` rows. The daily file that previously
failed the sum-to-1 check (a partial list summing to 1.0064) is now admitted as
`partial` under D80a with its published weights unchanged.

**Unmatched weight share per issuer** (sum of unmatched over sum of security
weight across each issuer's latest included versions; per-file median in
brackets):

| issuer | kind | default build | `--openfigi` |
|---|---|---|---|
| bmo | daily | 0.01% (0.00%) | 0.01% (0.00%) |
| evolve | daily | 85.86% (99.47%) | 0.08% (0.00%) |
| purpose | daily | 22.88% (9.26%) | 22.88% (9.26%) |
| evolve | quarterly | 34.67% (38.47%) | 34.67% (38.47%) |
| globalx_ca | quarterly | 55.25% (6.80%) | 55.25% (6.80%) |
| purpose | quarterly | 52.97% (55.47%) | 52.97% (55.47%) |
| vanguard_ca | quarterly | 36.77% (8.54%) | 36.77% (8.54%) |

The Evolve daily difference is the documented mode difference: its files carry a
Bloomberg country-composite code, which is not an exchange MIC, so without
`--openfigi` those rows resolve only through reviewed names (D57). The remaining
daily gap is Purpose name-only bond, income and bullion funds; the quarterly
gap is top-holdings lists whose names carry coupons, maturities and currencies
that no approved name override covers.

**Representative cross-issuer overlaps**, as of 2026-10-27 with `--openfigi`:

| pair | issuers | overlap | common holdings |
|---|---|---|---|
| PHR / ZRE | Purpose daily / BMO daily | 0.1922 | 7 |
| ESPX / ZSP | Evolve daily / BMO daily | 0.9309 | 496 |
| VFV / ZSP | Vanguard QPD / BMO daily | 0.4468 | 24 |

Both quarterly pairs are flagged "top holdings only" by the CLI. These are
derived aggregates; no security-level line is recorded here.

## Known limitations

- **Fuzzy proposals are not approved.** The names with several candidate ids
  (share classes, ADRs, similar issuers) need a human decision; the committed
  overrides cover the `exact` class only.
- **Name-only issuers stay partly unmatched.** Purpose bond, income and bullion
  funds, and the quarterly top-holdings lists, keep the shares above by design
  (D8/D41: no learned links, no automatic name matching).
- **Quarterly disclosures are top holdings only.** Their unmatched shares stay
  higher than the daily issuers' even after review, and analytics flag them
  `partial`.
- **OpenFIGI is optional and off by default.** Evolve's daily rows resolve to
  FIGI ids only with `--openfigi`; a cold cache needs network access.
- **BMO quarterly disclosures could not be exercised from this machine**
  (network), so the quarterly issuer list has no BMO rows.
- **Only exact name overrides are committed.** The 357 reviewed names carry a
  reviewer and date in `config/security_overrides.csv`. A reviewed name is
  evidence for every fund and every later build; there is no expiry.
  Re-reviewing a name that maps to a different security requires editing the
  reviewed CSV deliberately.
