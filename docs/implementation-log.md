# Implementation Log

## Milestone plan (Phase 1 — Foundation & Data Infrastructure)

| Milestone | Scope | Status |
|---|---|---|
| M1.1 | Repository, environment, configuration, schema and migrations, source registry and permission gate, instrument master, trading calendar, EOD ingestion framework, CSV adapter, CI | **Done** (2026-11-21) |
| M1.2 | Source review and selection (ADR 0008); EODHD adapter with call budget, refresh planner, bulk mode; ticker-list comparison; live JK test; archive replay | **Done** (2026-11-21), apart from the scheduled workflow (OI-20) |
| M1.3 | Corporate action ingestion; split and total-return adjustment factors; adjusted series; raw↔adjusted reconciliation | **Done** (2026-11-21) |
| M1.4 | Fundamental ingestion (point in time), financial facts normalisation | **Done for banks** (2026-11-21); non-banks open (OI-29), BCA automation awaiting owner (OI-30) |
| M1.5 | Cross-source reconciliation job, `reconciliation_results` | **Done** (2026-11-21); live data has one price source so far |
| M1.6 | Backups (daily, 90-day retention) and restore test; Phase 1 acceptance review | **Done** (2026-11-21), see `docs/phase1-acceptance.md` |

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

## 2026-11-21 — M1.3 corporate actions (branch `m1.3-corporate-actions`)

Owner decisions: budget stays Rp0; the agent sources financial statements (M1.4); start M1.3.

**Built (ADR 0009)**
- Migration `0002`: versioned `corporate_actions`, `reconciliation_results`, `artifact_fetches`
  (with backfill), `daily_prices.volume_basis` (with backfill for EODHD rows), factor
  versioning per action with recorded inputs. Up/down/drift checked on PostgreSQL and SQLite.
- `corporate_actions/`: explicit schemas, input rules, versioned writer, pipeline (reuses the EOD
  pipeline's gate, archive and atomic runs), EODHD and CSV adapters, factor formulas and service,
  point-in-time adjusted series with EODHD volume conversion, two reconciliation checks.
- CLI: `idx ca fetch-eodhd | import | factors | reconcile`, `idx prices show`,
  `idx ingest replay --dataset corporate_actions`.
- CI actions moved to `checkout@v7` / `setup-uv@v7` (Node 24; removes the deprecation warning).

**Verified live** (10 API calls): 12 dividends; 5/5 vendor reconciliation pass (max diff
0.000002%); factor recompute idempotent; full database rebuild from the archive alone gives
identical results with no API calls.

**Bugs found and fixed**
- *Lost fetch provenance:* identical response bodies (e.g. empty split lists) were stored once,
  so the system could not prove which tickers had been checked. Added the `artifact_fetches` log.
- *Mixed archive folder:* replay fed corporate-action files to the price parser. Replay now
  filters by payload kind.
- Existing EODHD bars had no volume basis. Migration 0002 backfills it.
- A field named `date` shadowed the `date` type in the vendor schema (caught by mypy).

**Tests:** 166 pass (SQLite + PostgreSQL).

## 2026-11-21 — M1.4 point-in-time fundamentals (branch `m1.4-fundamentals`)

**Source review (ADR 0010).**
- IDX `idx.co.id`: denied (bot challenge). `idx.id`: only a front end for the same protected
  API, so not used.
- EODHD fundamentals: paid.
- BCA IR: acceptable, but automation is on hold because its firewall blocks `python-urllib`
  (owner decision, OI-30).

**Built.**
- Migration `0003`: fact basis, period and scale; report document type, title, publication-date
  basis, run link; new unique keys.
- OJK bank publication parser with identity checks; BCA IR adapter (index discovery,
  filename publication dates, re-upload = revision); manual PDF import; point-in-time queries
  (next-day availability, revision-aware); TTM; ratios (FR-007).
- CLI: `idx fin fetch-bca | import-pdf | show | ratios`.

**Verified live** on BCA FY2025 and Q1 2026: identities hold; Q1 profit matches the press;
the point-in-time switch happens exactly on the day after publication.

**Tests:** 191 pass (SQLite + PostgreSQL).

## 2026-11-21 — M1.5 cross-source reconciliation (branch `m1.5-reconciliation`)

- `ingestion/reconcile_sources.py` + `idx ingest reconcile`: for every (instrument, session)
  with bars from two or more sources, OHLC must be identical (IDX tick grid). Volume is
  compared after converting EODHD volume back to raw; a difference above 5% is a warning.
- Price mismatches open `cross_source_price_mismatch` issues; `blocked_bars()` lets
  downstream stages refuse those sessions (PRD §8.3). Only exceptions and a summary are
  stored, not every passing pair.
- Live: only EODHD bars so far, so the command reports "nothing to compare" instead of a
  meaningless pass.

## 2026-11-21 — M1.6 backups, daily routine, Phase 1 acceptance (branch `m1.6-backup`)

- `ops/backup.py` + `idx backup create | verify | prune | archive | status` (ADR 0011):
  snapshot-consistent `pg_dump`, a real restore test into a scratch database, 90-day
  retention, checksum-verified incremental archive copy.
- `idx daily`: the whole EOD routine with isolated steps and an exit code for
  healthy/degraded/failed.
- **Bugs fixed:**
  - SQLite connections were left open (`with sqlite3.connect()` does not close);
  - price and corporate-action collection used BUY eligibility, so unknown-status and
    suspended stocks got no data. Now every listed common stock is collected; warrants are
    excluded.
- **Live:** backup and restore of 3,062 rows matched; archive copy 26 files; `idx daily`
  works without API calls.
- Phase 1 acceptance review: `docs/phase1-acceptance.md`.
- **Tests:** 205 pass (SQLite + PostgreSQL).

## 2026-11-21 — Owner decisions, non-bank statements, data expansion (branch `data-expansion` → `main`)

Owner: (1) honest client identity for BCA, yes; (2) supplied ANTAM H1-2025 statements;
(3) merge directly; (4) expand the data.

- **Merged** M1.3–M1.6 into `main`. CI failed on it: the runner (Ubuntu 26.04) has
  PostgreSQL 18 client tools against a 16 server, and a newer `pg_dump` writes
  `transaction_timeout`, which a 16 server rejects. Backups now require equal majors, and CI
  starts a server matching the client. CI is green.
- **BCA:** all 5 quarterly publication reports ingested automatically.
  - Bug: identical bytes arriving through another channel became a fake revision. Fixed
    (document identity by SHA-256).
  - Bug: interim reports say "periode berjalan". Fixed.
  - Bug: "nothing new" runs were counted as failed; they are now `skipped`.
  - Q3-2026 profit growth 4.19% matches the press (4.2%).
- **Non-bank parser** (ADR 0012), verified on ANTAM: 19 concepts, all identities, issued
  shares from the caption, EPS cross-check, healed split numbers. Press figures match. Bugs
  found on the real file: notes pages counted as statements; section headings broke wrapped
  labels.
- **Data expansion:** master 923 stocks (from EODHD, no invented dates); 100,048 new daily bars
  for 420 stocks via the one-off bonus calls; corporate actions for 43 stocks.
- **Vendor data problems found and handled:**
  - holiday filler bars, sometimes 999999.9999 (OI-33);
  - fractional "raw" prices: 320 bars marked invalid by the new revalidation (OI-34);
  - UNTR's superseded dividend applied twice by EODHD: cancelled by an evidenced manual
    override; the vendor mismatch is acknowledged in `reconciliation_exceptions.yaml`.
  - Splits confirmed for DSSA (1:25) and CYBR (1:2).
- **Vendor reconciliation:** 40 pass, 1 acknowledged, 0 fail. Tickers without our own
  corporate-action lists are skipped, not failed.
- **Data package:** 219,535 rows, restore-tested, delivered to the owner outside git.
- **Tests:** 228 pass (SQLite + PostgreSQL).

## 2026-11-22 — IDX stock list, trading assumptions

- `idx instruments import-idx-list` (migration 0004 `instrument_share_counts`): 944 stocks with
  official listing dates, boards and listed shares. 27 new instruments; 917 dated. Placeholder
  ticker starts were replaced by the listing date. Special-monitoring board stocks (60) are not
  BUY-eligible. Six collected tickers missing from the list are reported, not delisted.
- Bug: the Indonesian month abbreviation "Agt" (Agustus) was not recognised; 66 rows failed
  until it was added.
- `config/trading.yaml`: Stockbit costs (conservative 0.1513% / 0.2513%), capital Rp 100 million,
  lot 100, 1 tick slippage per side.
- **Tests:** 235 pass.

## 2026-11-22 — Daily run 1, M1.7 review, M2.1 feature store

- **Daily run:** healthy. +2,943 bars from 16 new stocks, corporate actions for 2 more;
  vendor reconciliation 42 pass, 1 acknowledged, 0 fail; backup restore-tested.
- **M1.7 issuer IR review:** only BCA can be automated legitimately.
  - Astra: the main site allows Claude bots, but its report API host's robots.txt is
    `Disallow: /`.
  - Telkom: the report list comes from an undisclosed script endpoint.
  - BRI, BNI, Unilever: HTTP 403.
  - ANTAM: robots.txt disallows ClaudeBot.
  - Reports for these issuers come from owner uploads.
- **M2.1** `tech_v1` feature store (ADR 0013): 19 features, look-ahead test, versioned by
  definition hash, Parquet output. First build: 440 stocks, 103,866 rows.
