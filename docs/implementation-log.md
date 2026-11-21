# Implementation Log

## Milestone plan (Phase 1 — Foundation & Data Infrastructure)

| Milestone | Scope | Status |
|---|---|---|
| M1.1 | Repository, environment, configuration, schema and migrations, source registry and permission gate, instrument master, trading calendar, EOD ingestion framework, CSV adapter, CI | **Done** (2026-11-21) |
| M1.2 | Source review and selection (ADR 0008); EODHD adapter with call budget, refresh planner, bulk mode; ticker-list comparison; live JK test; archive replay | **Done** (2026-11-21), apart from the scheduled workflow (OI-20) |
| M1.3 | Corporate action ingestion; split and total-return adjustment factors; adjusted series; raw↔adjusted reconciliation | Planned |
| M1.4 | Fundamental ingestion (point in time), financial facts normalisation | Planned |
| M1.5 | Cross-source reconciliation job, `reconciliation_results` | Planned |
| M1.6 | Backups (daily, 90-day retention) and restore test; Phase 1 acceptance review | Planned |

## 2026-11-21 — M1.1

**Built**
- `uv` project (Python 3.11–3.13), locked dependencies, ruff and mypy settings, GitHub Actions CI
  with a PostgreSQL 16 service.
- Schema (Alembic `0001`): reference data, source registry, ingestion bookkeeping and data
  quality, bitemporal `daily_prices`, corporate action and fundamentals tables (schema only).
- Source registry (`config/sources.yaml`) with explicit schema, idempotent sync, and a
  per-purpose permission gate with evidence and expiry.
- Instrument master: permanent identity, half-open ticker intervals with overlap protection,
  ticker changes, listing events replayed into status, base universe eligibility, CSV importers.
- XIDX trading calendar seeded from `exchange_calendars`, manual overrides kept, EOD readiness
  check.
- Ingestion framework: adapter contract, content-addressed raw archive, CSV adapter with column
  profiles, validation rules, versioned writer with `flag`/`supersede` policies, atomic runs,
  per-record outcomes, freshness and missing-session report.
- `idx` CLI: `db`, `sources`, `calendar`, `instruments`, `ingest`.

**Verified**
- 99 tests pass on SQLite and on PostgreSQL 16. Line coverage is 97%.
- CLI smoke run against a fresh PostgreSQL database: migrate → sync → seed → import → ingest
  (rerun = all unchanged) → universe → freshness. The permission gate refuses `idx_official`.
- The migration builds exactly the ORM schema and downgrades cleanly on both backends.

**Bugs found by tests and fixed**
- SQLite returns naive datetimes, so a calendar reseed rewrote every row. Comparisons now
  normalise to UTC.
- The calendar seeder only covered the library's default 20-year window and dropped non-session
  edge dates. It now builds the exact window, capped at the library's forward horizon.
- The CLI leaked database connections. Engines are now disposed per command.
- A delisted instrument kept its ticker open forever (found in the smoke run). Delisting now ends
  the ticker interval.

**Deliberately not done**
- No third-party source adapter, because no source has passed a terms review yet (ADR 0004).
- No feature engineering or models (PRD §30: data infrastructure must pass acceptance first).

## 2026-11-21 — M1.2

**Source review (ADR 0008).** Reviewed IDX, Yahoo, Stooq, Twelve Data, Invezgo, Sectors.app and
EODHD from their live terms and pricing pages.
- IDX: blocks all automation with a Cloudflare challenge → denied.
- Yahoo: its terms forbid automated collection and building a database → denied.
- Twelve Data: IDX needs the paid Pro plan. Invezgo: paid.
- **EODHD chosen**: its terms allow private storage and analysis. Free plan = 20 calls/day, past
  year only.
- A probe that used a browser user-agent against IDX after its 403 was (rightly) blocked by
  the session's safety controls. It was dropped, and the HTTP client never imitates a browser.

**Built**
- `ingestion/http.py`: standard-library client that identifies itself honestly, throttles,
  retries only transient errors and redacts secrets.
- `ingestion/adapters/eodhd.py`: explicit response schemas, `CallBudget` hard cap, per-ticker
  and bulk modes, symbol lists; 401/402/403 are fatal.
- `coverage.plan_refresh`: under a call budget, never-fetched tickers first, then the stalest.
- CLI: `idx ingest eodhd`, `idx ingest eodhd-bulk`, `idx instruments eodhd-diff`.
- Registry: EODHD approved; Yahoo `blocked`; IDX automated access denied; Twelve Data and
  Invezgo registered as paid alternatives.

**Verified**
- 127 tests pass on SQLite and PostgreSQL 16. Line coverage is 96%.
- Live call to the real EODHD API with the public demo key (US ticker): schema parsed, token
  redacted, a 403 is fatal and not retried. JK data needs the owner's key (OI-19).

**Bugs found and fixed**
- A run's request options (dataclasses) could not be stored as JSON. They are now converted.
- A registry value longer than its database column passed on SQLite but failed on PostgreSQL.
  The registry schema now enforces the column lengths, and a test keeps them in sync.

## 2026-11-21 — M1.2 live verification (owner's free EODHD key)

- Key stored only in the git-ignored `.env` (mode 600). Tests now ignore `.env` entirely.
- 7 API calls used (EODHD's counter agrees): JK active and delisted lists, then 5 tickers ×
  1 year. Result: 1,195 bars, 0 rejected.
- **Bug found by the live data:** instruments without a known listing date were treated as
  "not listed", so every bar got a warning and the universe silently excluded them. Added the
  `unknown` listing status: an INFO note on price rows, and excluded from the universe with an
  explicit reason (OI-23).
- **New: `idx ingest replay`** rebuilds prices from the raw archive (checksum-verified, original
  parser, gated by `raw_archive`). It was used to re-validate the live data after the fix with
  zero API calls. A replay into an empty database reproduces identical rows.
- **Independent checks on the real data:** calendar = trading record for all 239 sessions; 0 of
  4,780 OHLC values off the IDX tick grid; adjusted/raw close ratio shows dividends only.
- **Coverage findings:** see OI-02, OI-21, OI-22.
- **Test-isolation bug fixed:** a CLI test read the real `.env` token and attempted a network
  call; an autouse fixture now removes all `IDX_*` settings and `.env` loading from tests.
- 137 tests pass (SQLite + PostgreSQL), 95% line coverage.
