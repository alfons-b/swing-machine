# IDX AI Trading Intelligence

A personal quantitative research platform for the Indonesia Stock Exchange (BEI/IDX).

**Current state: Phase 1 complete (M1.1–M1.6), see `docs/phase1-acceptance.md`. Data foundation, automated EOD source, corporate actions and adjusted prices, point-in-time bank fundamentals, cross-source checks, verified backups.** There are no models, signals or
dashboard yet. By design (PRD §28, §30), these come only after the data infrastructure passes
its acceptance tests.

> This software gives no investment advice, does not execute trades, and never connects to a
> broker.

## What exists today

| Area | Capability |
|---|---|
| Schema | PostgreSQL schema built with Alembic migrations: reference data, source registry, ingestion log, data-quality issues, bitemporal raw prices, and corporate-action and fundamentals tables (schema only for now) |
| Source registry | `config/sources.yaml` is reviewed in git. Permissions are given per purpose and need evidence. Every adapter must pass a **permission gate** before it runs |
| Instrument master | Permanent `instrument_id`; ticker history stored as dated intervals (ticker changes keep the same instrument); listing, suspension and delisting events; base universe eligibility |
| Calendar | XIDX sessions seeded from `exchange_calendars`, with manual corrections and an EOD-readiness check |
| EOD ingestion | Adapter → raw archive (content-addressed) → schema parse → validation rules → versioned, idempotent write. Every row can be traced to its run and raw file |
| Monitoring | Per-run counters, per-record outcomes, data-quality issues, freshness and missing-session report |

**Data sources** (review in `docs/adr/0008-eod-source-selection.md`):
- **EODHD** is the automated EOD source. Its terms allow private storage and analysis. The free
  plan gives 20 calls/day and 1 year of history, which keeps about 20 tickers current; the paid
  plan (from USD 19.99/mo) adds whole-exchange bulk downloads and 30+ years of history.
- **User-supplied CSV files**, for example the IDX daily stock summary downloaded by hand, give
  full-market coverage at Rp0.
- IDX automated access and Yahoo Finance are **not allowed** (bot challenge / terms).

## Setup

Requirements: Python 3.11–3.13, [uv](https://docs.astral.sh/uv/), PostgreSQL 16 (optional for
tests, which also run on SQLite).

```bash
uv sync
```

```bash
cp .env.example .env
```

On Windows PowerShell, use `copy .env.example .env` instead. Then edit `IDX_DATABASE_URL`.

Create the database (example for a local PostgreSQL):

```bash
psql -U postgres -c "CREATE USER idx WITH PASSWORD 'idx'" -c "CREATE DATABASE idx_trading OWNER idx"
```

## First run

```bash
uv run idx db upgrade
```

```bash
uv run idx sources sync
```

```bash
uv run idx calendar seed --start 2000-01-01 --end 2027-12-31
```

```bash
uv run idx instruments import path/to/instruments.csv --ticker-changes path/to/ticker_changes.csv
```

```bash
uv run idx ingest csv path/to/eod.csv --provider "where the file came from"
```

```bash
uv run idx ingest runs
```

```bash
uv run idx ingest freshness
```

Automated EOD from EODHD (set `IDX_EODHD_API_TOKEN` in `.env` first; get a free key at eodhd.com):

```bash
uv run idx ingest eodhd --tickers BBCA,TLKM,BBRI --dry-run
```

```bash
uv run idx ingest eodhd --universe
```

```bash
uv run idx instruments eodhd-diff --delisted
```

The first command shows the plan without sending anything. The second spends at most
`IDX_EODHD_DAILY_CALL_BUDGET` calls, stalest tickers first; running it daily after 17:30 WIB
keeps them current. The third compares EODHD's ticker lists with your instrument master and
writes nothing. On a paid plan, use `uv run idx ingest eodhd-bulk --date 2026-11-20` for the
whole exchange in one call.

Rebuild prices from the raw archive (no API calls; also the restore path after losing the
database):

```bash
uv run idx ingest replay --source eodhd
```

Run the EODHD refresh every weekday after 17:30 WIB. On Windows, use Task Scheduler (run
`uv run idx ingest eodhd --universe` in the project folder); on Linux or macOS, use cron.

Corporate actions and adjusted prices (ADR 0009):

```bash
uv run idx ca fetch-eodhd --universe
```

```bash
uv run idx ca import path/to/corporate_actions.csv
```

```bash
uv run idx ca factors
```

```bash
uv run idx ca reconcile --universe
```

```bash
uv run idx prices show BBCA --adjust total_return --start 2026-01-01 --csv bbca.csv
```

- `ca fetch-eodhd` fetches dividends and splits (2 calls per ticker, least recently checked
  first, within the daily budget).
- `ca import` loads actions you enter from IDX/KSEI announcements. Columns: `ticker,
  action_type, ex_date, ratio_old, ratio_new, cash_amount, exercise_price, currency,
  announcement_date, record_date, payment_date`. Manual entries override EODHD for the same event.
- `ca factors` recomputes the versioned adjustment factors; `ca reconcile` checks them against
  EODHD's adjusted close and checks splits against prices.
- `prices show` prints or exports `raw`, `split` or `total_return` series; add
  `--as-of DATE` for point-in-time adjustment.

Point-in-time fundamentals (ADR 0010; banks, OJK publication-report format):

```bash
uv run idx fin import-pdf laporan-keuangan-maret-2026.pdf --ticker BBCA --published-on 2026-04-23
```

```bash
uv run idx fin show BBCA --as-of 2026-04-24
```

```bash
uv run idx fin ratios BBCA --as-of 2026-04-24
```

A report becomes visible the day after its publication date. `idx fin fetch-bca` (automatic
download from BCA's IR page) is on hold until the owner decides OI-30.

### Daily routine and backups (ADR 0011)

```bash
uv run idx daily
```

It runs prices, corporate actions, factors, checks, backup and the archive copy, plus a restore
test every 30 days. Exit code 0 = healthy, 1 = degraded, 2 = failed. Set `IDX_BACKUP_DIR` to a
different disk or a synced folder.

On Windows, schedule it once per trading day after 17:30 WIB:

```bash
schtasks /Create /TN "idx-daily" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 17:45 /TR "cmd /c cd /d C:\path\to\swing-machine && uv run idx daily >> logs\daily.log 2>&1"
```

Run the backup commands separately when needed:

```bash
uv run idx backup create
```

```bash
uv run idx backup verify
```

```bash
uv run idx backup status
```

Other useful commands: `idx sources list`, `idx sources check <id> automated_ingestion`,
`idx instruments universe --on 2026-11-20 --eligible-only`. Add `--help` to any command for
details.

### CSV formats

- **Instruments:** `ticker, company_name, instrument_type, listing_date, delisting_date, board, isin`
- **Ticker changes:** `old_ticker, new_ticker, effective_date, reason`
- **EOD, `standard` profile:** `ticker, date, open, high, low, close[, prev_close, volume, value, frequency, foreign_buy, foreign_sell]`
- **EOD, `idx_stock_summary` profile:** the IDX daily stock-summary layout. Use `--trade-date` if
  the file has no date column. Check the headers against a real export first (OI-01).

Re-importing a file is safe: unchanged rows are skipped. If a source later reports different
values, they are **flagged** and the stored row is kept. Use `--supersede` to store them as a
new version instead.

## Development

```bash
uv run pytest
```

```bash
IDX_TEST_DATABASE_URL=postgresql+psycopg://idx:idx@localhost:5432/idx_trading_test uv run pytest
```

```bash
uv run ruff check src tests migrations && uv run ruff format --check src tests migrations && uv run mypy
```

The first `pytest` command runs on SQLite only; the second also runs every database test on
PostgreSQL.

Schema changes are made in the models and then added as a migration:

```bash
uv run alembic revision --autogenerate -m "describe change"
```

A test fails if the models and migrations drift apart.

## Layout

```
config/sources.yaml          reviewed data-source registry
migrations/                  Alembic migrations
src/idx_trading/
  db/                        models, enums, sessions
  registry/                  registry loader + permission gate
  instruments/               instrument master + CSV importers
  calendar/                  XIDX trading calendar
  ingestion/                 adapters, archive, validation, writer, pipeline, coverage
  cli/                       `idx` command
tests/                       unit + integration (SQLite always, PostgreSQL when configured)
docs/adr/                    architecture decision records
docs/traceability.md         PRD requirement → code → test
docs/open-issues.md          PRD §27 issues and new findings
docs/implementation-log.md   milestone plan and log
```
