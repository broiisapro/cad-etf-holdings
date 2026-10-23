# Epic 03 — Phase 1 ship

**Status: draft — pending ship steps: secret scan, hosted CI switch, visibility change, demo deploy, v1.0.0.**

## Goal

Prepare the Phase 1 public release without making anything public. The code is
functionally complete (Epics 0–2), so this epic is about what a stranger sees
and what a maintainer can prove: repository hygiene and an MIT licence, CI that
can move to hosted runners by flipping one variable plus a secret scan on every
PR and over the full history, a demo mode that can never show real issuer data,
reproducible README screenshots, the release README and this document, the
human-approved name overrides, and the last QA follow-ups from the Epic 2
review. Nothing is deployed, made public or tagged until Epic 2 closes and the
human approves (D63).

## How it works

- **Repository hygiene and licence (A).** `LICENSE` is MIT (D61), `pyproject.toml`
  carries the public description, keywords, classifiers and project URLs, and
  `.env.example` documents every environment variable the code reads
  (`ETFH_USER_AGENT`, `OPENFIGI_API_KEY`, `ETFH_OPENFIGI`, `ETFH_CONFIG_DIR`,
  `ETFH_DB`, `ETFH_DUCKDB_THREADS`, `ETFH_DEMO`). `.gitignore` covers raw data,
  builds, databases, `.env`, caches and the orchestration directories; the
  debug-leftover audit (prints, TODO/FIXME markers, commented-out blocks) found
  nothing to remove.
- **CI ready for hosted runners (B, D60).** The required `lint`, `types` and
  `tests` jobs take their runner label set from the repository variable
  `CI_RUNS_ON` (default `["self-hosted","macOS","ARM64"]`). On hosted runners the
  jobs install uv and Python 3.12 themselves; on the self-hosted Mac they keep
  the preinstalled toolchain. Ship is therefore one variable change, not a
  workflow rewrite.
- **Secret scanning (B, D60).** A non-required `secrets` job runs gitleaks on
  every PR with `.gitleaks.toml` (default rules; no broad allowlists). The
  workflow uses the pinned gitleaks binary rather than the marketplace action,
  which left a temporary download behind on a persistent runner and failed the
  next run. A separate full-history scan on a fresh clone of every branch found
  no secret and no real issuer file (gitleaks 8.30.1, 226 non-merge commits,
  0 findings, 2026-10-23); the history path audit found no `.env`, `raw/`,
  `*.duckdb`, `*.parquet` or issuer files outside `tests/fixtures/`.
- **Synthetic demo mode (C, D62).** `ETFH_DEMO=1` (or `--demo`) makes the
  dashboard build the committed synthetic store into a temporary directory and
  ignore `ETFH_DB`, so a demo server cannot open a real store. Every page shows
  "Demo data — synthetic funds only." The repository-root `streamlit_app.py` is
  the Community Cloud entry point and forces demo mode unconditionally. No
  `requirements.txt` is added: Community Cloud recognises `uv.lock` and runs uv
  from the repository root.
- **Reproducible screenshots (C, D59).** `scripts/make_screenshots.py` starts the
  dashboard in demo mode on a free port, drives Chromium with Playwright (a
  one-off `uv run --with` tool, not a dependency) and writes
  `docs/img/{fund,overlap,heatmap}.png` at 1400×900 and under 400 KB. It pins
  the committed synthetic as-of date and fund pair from
  `tests/fixtures/expected_overlap.json`, and a test checks the images exist,
  have the right size and come from demo mode.
- **Release README and docs (D).** `README.md` is the ten-second version:
  opening paragraph, one screenshot, what it does, responsible collection,
  quick start, real-data commands, architecture, data quality, limitations and
  licence. This document records the epic. `docs/epic-02-real-data-integration.md`
  gains the approval-and-commit flow (D58) and a final-numbers placeholder.
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
  public phase run the same file; the fallback is evaluated by GitHub, not by a
  shell.
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
- Repository variable `CI_RUNS_ON` (JSON array; the default keeps the private
  self-hosted runner).
- `etfh.dashboard.demo`: `is_demo()`, `demo_database()`, `DEMO_BANNER`,
  `DEMO_LATEST_ASOF`; environment variable `ETFH_DEMO`; entry point
  `streamlit_app.py`.
- `scripts/make_screenshots.py`; `docs/img/fund.png`, `overlap.png`, `heatmap.png`.
- `config/security_overrides.csv` with 357 global name overrides
  (`match_type=name`, `match_value=*|<normalized name>`) approved 2026-10-22.
- `etfh.qa.unmatched_shares(con, asof: date | None = None)`; parser notes in
  `qa_events` as `info` rows with `check` equal to the note name.
- `ops/run_collect.sh` honours `ETFH_UV_DIRS` when set.
- `README.md`, this epic document and the updated Epic 2 document.

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
- **No hosted CI run was spent.** The hosted path is wired and reasoned through
  in the PR, but the first real run happens after the variable change at ship
  time; spending hosted minutes now would not test anything the self-hosted
  runner does not.
- **Epic 2 stays open.** This epic prepares the release; it does not close the
  five-day scheduled-collection check (D50) or change any visibility, variable,
  deployment or tag (D63).

## Known limitations

- **Nothing is public yet.** The repository is private, the demo is not
  deployed, no release or tag exists and `CI_RUNS_ON` is unset. The pending ship
  steps below are the whole difference between this branch and a release.
- **The demo shows synthetic funds only.** Real derived aggregates in the public
  demo need an explicit human decision per source terms (D62).
- **The README images are static.** Regenerate them with
  `scripts/make_screenshots.py` after a dashboard UI change; the test guards
  their size, not their visual accuracy.
- **Coverage and data limits are unchanged from Epic 2.** Eight approved issuer
  sources, 52 real funds, quarterly disclosures as partial top-holdings lists,
  fuzzy name proposals still unapproved, OpenFIGI optional and off by default,
  no historical backfill before the first collection.
- **The hosted CI path is unproven until ship.** The workflow is written for it,
  but the first hosted run is part of the ship checklist, not of this wave's
  verification.

## Ship steps

1. Close Epic 2: five calendar days of scheduled collection and
   `wave-done.sh <wave> 02` passing its DoD.
2. Merge the remaining wave-05 PRs and run `wave-done.sh 05 03`; review the
   handbacks and the attribution audit.
3. Record the full-history secret scan result on the release (it is clean as of
   2026-10-23) and confirm `.gitleaks.toml` still has no broad allowlist.
4. Set the repository variable `CI_RUNS_ON=["ubuntu-latest"]`, remove the
   self-hosted runner, then change repository visibility (PLAYBOOK §7 order:
   runner and variable before visibility).
5. Deploy the demo on Streamlit Community Cloud: repository
   `broiisapro/cad-etf-holdings`, branch `main`, main file `streamlit_app.py`,
   Python 3.12, no secrets.
6. Tag `v1.0.0`, publish a code-only release, and set the GitHub description and
   topics from the README.
