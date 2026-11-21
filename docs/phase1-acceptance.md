# Phase 1 acceptance review — 2026-11-21

Phase 1 completion criterion (PRD §24): *reliable historical and daily data pipelines.*
Evidence is from the test suite (SQLite and PostgreSQL) and live runs on real data
(EODHD free plan; BCA publication reports).

Legend: ✅ met · 🟡 met with a stated limitation · ❌ not met

## PRD §22 — Data

| Criterion | Status | Evidence / limitation |
|---|---|---|
| All discovered eligible instruments are in master data | 🟡 | The master and importer work. Live: only 5 test stocks loaded. EODHD lists 923 active JK common stocks (`idx instruments eodhd-diff`), but EODHD gives no listing dates, so the full master needs the IDX company list (OI-23). |
| Ticker changes do not break instrument identity | ✅ | ADR 0002; `test_ticker_change_preserves_identity`, `test_ticker_change_mapped_through_vendor_symbols` |
| Delisted stocks are kept when data exists | 🟡 | Modelled and tested. Coverage: EODHD has only 97 delisted JK tickers (OI-02). |
| Raw and adjusted prices can be reconciled | ✅ | ADR 0009; live 5/5 vendor reconciliation, maximum difference 0.000002% |
| Fundamental publication timestamps are preserved | ✅ | ADR 0010; `published_at` and its basis, `available_at` = next day, revisions kept |
| Ingestion can be repeated without duplicates | ✅ | Content and record hashing; idempotency tests for prices, corporate actions and reports; live reruns all "unchanged" |
| Every data source is traceable | ✅ | Every row → run → artifact → fetch log; reviewed registry with evidence |

## Phase 1 deliverables (PRD §24)

| Deliverable | Status | Notes |
|---|---|---|
| Repository, environment, configuration | ✅ | `uv.lock`, CI (Node 24 actions), `.env` handling |
| Database setup / migrations | ✅ | Alembic 0001–0003; up, down and drift tested on both backends |
| Instrument master | 🟡 | See OI-23 (listing dates) |
| Data-source registry | ✅ | 12 sources reviewed; permission gate |
| EOD market ingestion | 🟡 | EODHD automated (Rp0: about 20 tickers/day); full market at Rp0 needs manual IDX files (OI-01) |
| Fundamental ingestion | 🟡 | Banks via the OJK publication report; non-banks open (OI-29); BCA automation awaiting owner (OI-30) |
| Corporate action processing | ✅ | Dividends, splits, rights, bonus shares; versioned factors; 1-year history on the free plan (OI-26) |
| Data validation | ✅ | Price, corporate-action, report-identity and cross-source rules |
| Raw archive | ✅ | Content-addressed, checksum-verified, replayable, copied to backup |
| Backups and restore (§18.2) | ✅ | ADR 0011; live restore test passed |

## Verdict
**Phase 1's infrastructure is complete and verified.** What limits it is *data coverage*, not
software: at Rp0 there is no legal, free, automated source for (a) all ~950 stocks' daily EOD,
(b) a full delisting history, or (c) non-bank financial statements. Those gaps are recorded
(OI-01, OI-02, OI-23, OI-29, OI-30) and each one needs a decision by the owner, data entered
by the owner, or budget.

**Recommendation:** start Phase 2 (features and baseline models) on the data that exists, and
widen coverage in parallel. Phase 2 must respect the coverage flags: no fundamental features
for issuers without reports, and no BUY signal where data is missing or blocked (PRD §8.3).
