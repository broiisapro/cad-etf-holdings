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
uv run etfh build --raw tests/fixtures/raw_synthetic --out /tmp/etfh2 \
  && cmp /tmp/etfh/parquet/holdings.parquet /tmp/etfh2/parquet/holdings.parquet && echo DETERMINISTIC
ETFH_DB=/tmp/etfh/etfh.duckdb uv run streamlit run etfh/dashboard/app.py --server.headless true
```

For live collection, copy `.env.example` to `.env`, set `ETFH_USER_AGENT` to a
contact-bearing identifier, then run `uv run etfh collect --date today --raw ./raw`
followed by `etfh build` and `etfh qa`. Raw downloads stay outside git.
