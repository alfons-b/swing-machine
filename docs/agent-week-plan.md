# Agent plan for 2026-11-22 → 2026-11-29 (owner returns 2026-11-30)

Owner instructions (2026-11-21): collect data every day for a week; download financial
statements through the owner's browser; keep building to the end and launch only when
complete; do not stop at each stage for the owner to test (automated tests stay: PRD §25
Definition of Done).

## Every daily run (scheduled task, 17:47 WIB)
1. `scripts/daily_collect.sh`: PostgreSQL up → `idx daily` (16 EODHD calls for prices, never-
   fetched first in `config/priority_tickers.txt` order; 4 for corporate actions) →
   `idx fin fetch-bca`. Log: `logs/daily-YYYYMMDD.log` (git-ignored).
2. Append one row to `docs/collection-log.md` (`scripts/collection_metrics.py`, counts only)
   and push it to `main`.
3. If the Claude in Chrome tools are connected: download the next items in
   `docs/fin-download-queue.md` from the issuers' own sites or IDX in the owner's browser, at
   human pace. If a CAPTCHA or bot challenge appears, stop and leave it for the owner; the agent
   never solves challenges. Then run `idx fin import-pdf` with the stated publication date.
4. With the remaining effort, the next unfinished milestone below. Each one: code + tests +
   docs; push to `main` only with green CI; note it in `docs/implementation-log.md`.
5. If `.env` or the database is gone (container reset), report it; do not invent data.

## Phase 2 milestones (data-independent first)
| ID | Milestone | Done when |
|---|---|---|
| M2.1 | Feature store: versioned definitions; technical features (returns, momentum, RSI, MACD, ATR, ADX, Bollinger, MAs, relative strength, volume/turnover); point-in-time fundamental features; Parquet export | Features reproducible from raw data, no look-ahead (tests) |
| M2.2 | Path-aware labels for H1–H4 (3/5/10/20/40 days): forward return net of costs, MFE, MAE; entry at the next session's open | Label tests on synthetic paths |
| M2.3 | Model registry + walk-forward evaluation; baselines (momentum rank, logistic/ridge) and LightGBM; calibration (Brier, reliability) | Reproducible comparison report |
| M2.4 | Ranking + recommendation engine: BUY/WATCH/AVOID, entry/target/stop/expiry, risk and data-quality gates; immutable signal registry | No BUY on blocked or ineligible data (tests) |
| M3.1 | Backtester: long-only, next-open execution, costs, slippage, lots of 100, multi-benchmark | Matches hand-computed cases |

Phase 5 (dashboard, Supabase, Google OAuth, Telegram, Cloudflare) needs the owner's accounts
and is prepared but not deployed before 2026-11-30.

## Last run (2026-11-29)
Write `docs/evaluation-2026-11-30.md` (coverage reached, quality issues, milestones done,
decisions needed), send the owner a fresh restore-tested data package, and disable the
scheduled task.
