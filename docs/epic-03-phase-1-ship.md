# Epic 03 — Phase 1 ship

**Status: repository preparation complete; the repository is public since 2026-10-28. Remaining ship steps: Streamlit Community Cloud deployment and the `v1.0.0` release.**

## Goal

Prepare the Phase 1 public release. The code is functionally complete
(Epics 0–2), so this epic is about what a stranger sees and what a maintainer
can prove: repository hygiene and an MIT licence, CI that can move to hosted
runners by flipping one variable plus a secret scan on every PR and over the
full history, a demo mode that can never show real issuer data, reproducible
README screenshots, the release README and this document, the human-approved
name overrides, and the last QA follow-ups from the Epic 2 review. Nothing was
deployed, made public or tagged until Epic 2 closed and the human approved
(D63); the public switch itself is recorded below and in D87/D88.

## How it works

- **Repository hygiene and licence (A).** `LICENSE` is MIT (D61), `pyproject.toml`
  carries the public description, keywords, classifiers and project URLs, and
  `.env.example` documents every environment variable the code reads
  (`ETFH_USER_AGENT`, `OPENFIGI_API_KEY`, `ETFH_OPENFIGI`, `ETFH_CONFIG_DIR`,
  `ETFH_DB`, `ETFH_DUCKDB_THREADS`, `ETFH_DEMO`). `.gitignore` covers raw data,
  builds, databases, `.env`, caches and the orchestration directories; the
  debug-leftover audit (stray prints, leftover markers, commented-out blocks) found
  nothing to remove.
- **CI ready for hosted runners (B, D60).** The required `lint`, `types` and `tests` jobs take their runner label set from the repository variable
  `CI_RUNS_ON` (default `["self-hosted","macOS","ARM64"]`). On hosted runners the
  jobs install uv and Python 3.12 themselves; on the self-hosted Mac they keep
  the preinstalled toolchain. The switch was used at the public ship: hosted CI
  was proven on a wave-08 PR while the repository was still private (D87), the
  self-hosted runner was removed once the switch stuck, and every job since
  2026-10-28 13:51 UTC has run green on `ubuntu-latest` (D88/D89). The workflow
  file itself is unchanged by the switch.
- **Secret scanning (B, D60).** A non-required `secrets` job runs gitleaks on
  every PR with `.gitleaks.toml` (default rules; no broad allowlists). The
  workflow uses the pinned gitleaks binary rather than the marketplace action,
  which left a temporary download behind on a persistent runner and failed the
  next run. At the public ship the full-history scan ran on a fresh mirror
  including PR refs: 311 commits, no secret, and a history path audit found no
  `.env`, `raw/`, `*.duckdb`, `*.parquet` or issuer file outside
  `tests/fixtures/`. Four issue/PR texts that quoted real holdings lines were
  redacted before the switch (originals backed up outside the repository;
  GitHub keeps the edit history) — D87 records the scan and D88 the switch.
- **Synthetic demo mode (C, D62).** `ETFH_DEMO=1` (or `--demo`) makes the
  dashboard build the committed synthetic store into a temporary directory and
  ignore `ETFH_DB`, so a demo server cannot open a real store. Every page shows
  "Demo data — synthetic funds only." The repository-root `streamlit_app.py` is
  the Community Cloud entry point and forces demo mode unconditionally. No
  `requirements.txt` is added: Community Cloud recognises `uv.lock` and runs uv
  from the repository root. The deployment is configured as repository
  `broiisapro/cad-etf-holdings`, branch `main`, main file `streamlit_app.py`,
  Python 3.12, no secrets and no environment variables.
- **Weekly view and demo readiness (wave 08, #190).** The dashboard gained a
  "This week" tab between **Fund** and **Overlap**: the digest for the Friday
  week covering the selected date, built from the same validated document as
  the published site. The hosted demo builds the public document (D79) and
  takes its config from the committed `config/` directory; the local dashboard
  builds an internal one. `etfh/dashboard/weekly.py` holds the pure table
  builders shared with the tab, and `streamlit_app.py` records the deploy
  settings above in its docstring.
- **Reproducible screenshots (C, D59).** `scripts/make_screenshots.py` starts the
  dashboard in demo mode on a free port, drives Chromium with Playwright (a
  one-off `uv run --with` tool, not a dependency) and writes
  `docs/img/{fund,overlap,heatmap}.png` at 1400×900 and under 400 KB. It pins
  the committed synthetic as-of date and fund pair from
  `tests/fixtures/expected_overlap.json`, and a test checks the images exist,
  have the right size and come from demo mode. The committed images predate the
  "This week" tab and still show a three-tab strip; they are regenerated in the
  next wave, so the README does not claim they show four tabs.
- **Release README and docs (D).** `README.md` is the ten-second version:
  opening paragraph, one screenshot, status, what it does, the weekly digest and
  the exploratory event study with their real commands, responsible collection,
  quick start, real-data commands, architecture, data quality, limitations and
  licence. This document records the epic, `docs/epic-02-real-data-integration.md`
  the approval-and-commit flow (D58) and the final live numbers, and
  `docs/epic-06-publication.md` the digest, site, Pages workflow, prices and
  event study.
- **Approved name overrides (D, D54/D58).** The human approved the 357 exact
  name proposals on 2026-10-22; the approval file was copied byte-for-byte to
  `config/security_overrides.csv` (reviewer and date on every row). The build
  applies them like any reviewed override; nothing was approved or edited by an
  agent.
- **QA follow-ups (E).** Parser notes (`partial_list`, `weight_capped_percent`,
  `commodity_quantity`, `exchange_codes`) are persisted as `info` rows in
  `qa_events`, so the QA report explains excluded or partial files without
  guessing. `etfh.qa.unmatched_shares(con, asof=None)` can select the exact
  version analytics would use for a date, and the dashboard's data-quality panel
  passes its selected as-of date, so the panel cannot show a republished file's
  share for an older view.
- **Hermetic ops tests (A).** `tests/test_ops.py` runs the scheduled scripts in
  an empty environment with fake `uv`/`etfh` executables, so the tests no longer
  depend on the host's PATH or user agent. `ops/run_collect.sh` gained
  `ETFH_UV_DIRS` to make the uv search list controllable; default behaviour is
  unchanged.

## Key decisions

- **D58 approval flow over ad-hoc edits.** The human's approval file is the
  artifact of record; an agent copies it verbatim through a reviewable PR.
  `config/security_overrides.csv` therefore contains only human-approved rows,
  each with a reviewer and a date.
- **D59 images are assets, not docs.** Screenshots live in `docs/img/` and are
  generated from synthetic data by a committed script, so the docs rule (D29)
  stays intact and no real issuer data appears in an image.
- **D60 one-variable CI switch.** Keeping the self-hosted defaults in the
  workflow and overriding through `CI_RUNS_ON` means the private phase and the
  public phase ran the same file; the fallback is evaluated by GitHub, not by a
  shell. The switch was flipped for the public ship and is now
  `["ubuntu-latest"]` (D87/D88).
- **D61 MIT.** Chosen by the human for a portfolio project: permissive and
  standard. The copyright line names the human, not a tool.
- **D62 synthetic-only demo.** A public demo that cannot reach real holdings is
  a terms decision, not only a technical one. `streamlit_app.py` forces demo
  mode so a misconfigured deployment cannot show live issuer data; publishing
  derived real aggregates would need a separate, explicit decision at ship time.
- **Keep the QA report's latest-version default.** As-of shares are opt-in
  (`unmatched_shares(con, asof)`), so existing reports and the
  `--max-unmatched-share` gate keep their meaning; only the dashboard asks for
  the selected date.

## Interfaces

- `LICENSE`, `pyproject.toml` metadata, `.env.example`, `.gitignore` — public
  packaging and documented environment.
- `.github/workflows/ci.yml`: required job ids `lint`, `types`, `tests` unchanged;
  `runs-on: ${{ fromJSON(vars.CI_RUNS_ON || '["self-hosted","macOS","ARM64"]') }}`;
  non-required `secrets` job; `.gitleaks.toml` default rules.
- Repository variable `CI_RUNS_ON` (JSON array; `["ubuntu-latest"]` since the
  public ship, D87).
- `etfh.dashboard.demo`: `is_demo()`, `demo_database()`, `DEMO_BANNER`,
  `DEMO_LATEST_ASOF`; environment variable `ETFH_DEMO`; entry point
  `streamlit_app.py` (repository, branch, main file, Python version and the
  absence of secrets recorded in its docstring).
- `etfh.dashboard.weekly`: `week_end(asof)`, `config_dir()`,
  `digest_for_week(db_file, end, *, public, config_dir, demo=False)`, `fund_table`,
  `changes_table`, `overlap_table`, `undisclosed_funds`, `quality_label`; the
  "This week" tab in `etfh/dashboard/app.py`.
- `scripts/make_screenshots.py`; `docs/img/fund.png`, `overlap.png`, `heatmap.png`.
- `config/security_overrides.csv` with 357 global name overrides
  (`match_type=name`, `match_value=*|<normalized name>`) approved 2026-10-22.
- `etfh.qa.unmatched_shares(con, asof: date | None = None)`; parser notes in
  `qa_events` as `info` rows with `check` equal to the note name.
- `ops/run_collect.sh` honours `ETFH_UV_DIRS` when set.
- `README.md`, this epic document, the updated Epic 2 document and
  `docs/epic-06-publication.md`.

## Deviations

- **One test outside this wave's file ownership changed.** Committing the
  approved overrides made `tests/test_secmaster.py::test_committed_overrides_file_is_header_only`
  fail by construction, and no wave-05 issue owned that test. It now asserts
  that the committed file loads, that no data row is dropped and that every
  override is reviewed. This is the same kind of cross-owner unblock recorded in
  the wave-04 review, and it is listed in the PR and handback.
- **The gitleaks marketplace action was replaced by the pinned binary** after
  the action's installer collided with its own leftover file on the persistent
  self-hosted runner. The fix is a CI-only change in the same wave.
- **Hosted CI and the visibility switch happened after this epic's wave.** The
  hosted path was wired in wave 05, proven on a wave-08 PR while the repository
  was private, and the self-hosted runner was removed after the public switch
  (D87–D89). The full-history scan (`311 commits, no secret`) and the redaction
  of four issue/PR texts are recorded in D87/D88. Nothing in this epic's code
  changed for the switch.
- **Epic 2 closed before the ship.** The five-day scheduled-collection check
  (D50) passed and Epic 2 was closed by the wave-08 start (D83), which is what
  unblocked the public ship.

## Known limitations

- **The repository is public but not released.** It went public on 2026-10-28
  13:48 UTC; the Streamlit demo is not deployed and no release or `v1.0.0` tag
  exists. Those two steps are the whole difference between this branch and a
  release.
- **The demo shows synthetic funds only.** Real derived aggregates in the public
  demo need an explicit human decision per source terms (D62).
- **The README images are static and show three tabs.** Regenerate them with
  `scripts/make_screenshots.py` after a dashboard UI change (the "This week" tab
  arrived after the images); the test guards their size, not their visual
  accuracy.
- **Coverage and data limits are unchanged from Epic 2.** Eight approved issuer
  sources, 52 real funds, quarterly disclosures as partial top-holdings lists,
  fuzzy name proposals still unapproved, OpenFIGI optional and off by default.

## Ship steps

The repository is already public (2026-10-28) and hosted CI is proven, so the
steps that remain are the two that need an external service or a release:

1. Deploy the demo on Streamlit Community Cloud: repository
   `broiisapro/cad-etf-holdings`, branch `main`, main file `streamlit_app.py`,
   Python 3.12, no secrets and no environment variables. Confirm the deployed
   page shows the demo banner and the "This week" tab.
2. Tag `v1.0.0`, publish a code-only release, and confirm the GitHub description
   and topics still match the README.

Already done, in the agreed order: hosted CI proven on the private repository by
wave-08 PRs; full-history secret scan and history path audit on a fresh mirror
(clean, four texts redacted); self-hosted runner removed; visibility changed to
public with secret scanning, push protection and fork-PR approval enabled
(D87–D89). The `v1.0.0` tag stays the last step, after the docs and the demo
deployment.
