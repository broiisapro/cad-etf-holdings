# cad-etf-holdings

cad-etf-holdings collects the portfolio holdings Canadian ETF issuers publish, normalizes them into one point-in-time dataset, and answers questions like "how much do these two funds overlap?" and "which ETFs added this stock last quarter?". Collection is deliberately polite: every source has a recorded terms-of-use decision, and nothing is fetched or redistributed without one.

Status: Epic 1 (pipeline core) is complete. Collectors, parsers, security master, validation/QA, deterministic build, as-of analytics, CLI and a local dashboard run end to end on the committed synthetic fixtures; live collection is enabled only for the sources whose terms decisions allow it (`config/sources.yaml`).

## Quick start (synthetic data)

Everything below runs offline against the committed fixtures in `tests/fixtures/`.

```bash
uv sync --frozen
uv run pytest -q                                                    # offline suite
uv run etfh build --raw tests/fixtures/raw_synthetic --out /tmp/etfh
uv run etfh qa --db /tmp/etfh/etfh.duckdb                           # exit 0; 2 bad files excluded
uv run etfh overlap SYNA SYNB --asof 2026-11-02 --db /tmp/etfh/etfh.duckdb --json
uv run etfh changes --fund SYNA --from 2026-10-29 --to 2026-11-02 --db /tmp/etfh/etfh.duckdb
uv run etfh spotcheck --raw tests/fixtures/raw_synthetic --db /tmp/etfh/etfh.duckdb --n 5
uv run etfh export --db /tmp/etfh/etfh.duckdb --out /tmp/etfh-export --asof 2026-11-02
uv run etfh build --raw tests/fixtures/raw_synthetic --out /tmp/etfh2 \
  && cmp /tmp/etfh/parquet/holdings.parquet /tmp/etfh2/parquet/holdings.parquet && echo DETERMINISTIC
ETFH_DB=/tmp/etfh/etfh.duckdb uv run streamlit run etfh/dashboard/app.py --server.headless true
```

`spotcheck` re-reads the latest included file per issuer and prints sampled raw
values next to the normalized weights (deterministic for a given `--seed`), so a
parsing bug is visible against the file the numbers came from. `export` writes
derived aggregates only (`overlap_matrix.csv`, `concentration.csv`,
`sector_breakdown.csv`) — never holdings rows, names or individual weights.

For live collection, copy `.env.example` to `.env`, set `ETFH_USER_AGENT` to a
contact-bearing identifier (quote values that contain spaces or parentheses),
then run `uv run etfh collect --date today --raw ./raw` followed by
`uv run etfh build --raw ./raw --out ./build` and
`uv run etfh qa --db ./build/etfh.duckdb`. Add `--openfigi` to the build (with an
optional `OPENFIGI_API_KEY`) to assign cached composite FIGI ids to the rows
whose identifiers map. Raw downloads stay outside git. `ops/run_collect.sh` runs
this pass daily and loads `.env` itself; `ops/install_launchd.sh` installs it as
a launchd job.
