# ADR 0016 — Recommendation engine and the immutable signal registry

- Status: Accepted
- Date: 2026-11-25
- Implements: PRD FR-030 (signal generation), FR-031 (full ranking), FR-032 (top picks),
  FR-033 (entry, target, stop), FR-034 (position size, basic), FR-042 (immutable signals); M2.4

## Decision

### Which model, which mode
- **Production** signals need all of the following:
  - a model the owner approved (`idx models approve`, ADR 0015);
  - a model that predicts excess return;
  - matching feature definitions;
  - an out-of-sample calibration (below);
  - the latest session in the feature store as the signal date.
- Anything else is **shadow**: a candidate model or a past date. Shadow runs are stored for
  research and are never shown as a recommendation.
- No model is approved yet, so every run so far is shadow.

### Expected return is calibrated, never the raw score
- Over-confidence: on the walk-forward predictions, realised excess return grew by only about
  0.3 × the predicted excess (slope 0.28–0.32). Raw regression scores are over-confident in
  magnitude.
- The fix:
  - `idx models evaluate` now stores, per model, the least-squares line
    `realised excess ~ intercept + slope × score` from pooled out-of-sample predictions.
  - Signals use `expected excess = intercept + slope × score`.
  - A model without this line, or with slope ≤ 0, cannot produce signals.
- **Expected net return** = expected excess + market drift.
  - The **drift** is the median net return over the last 20 days whose H-day labels had already
    closed on the signal date. Only information available that day is used.
  - In a falling market the drift is negative, so the best-ranked stocks can still stay WATCH.

### Rules (`signals/engine.py`, pure function, unit-tested)
- **AVOID** when the first failing gate is any of these:
  - not eligible (listing status, special-monitoring board, instrument type);
  - no price bar on the signal date;
  - open data-quality errors in the last 30 days;
  - below the floor (Rp 200, Rp 1 bn/day);
  - no ATR;
  - the model could not score the stock;
  - rank outside the top 20%.
- **WATCH** (top 20%, but no BUY) when any of these holds:
  - fewer than 100 stocks ranked that day;
  - no drift estimate;
  - expected net return below +0.5%;
  - the position rounds to zero lots.
- **BUY** when every gate passes.
- Every decision stores its reasons.

### Levels, in raw prices on IDX ticks
- Max entry = close + 0.5 ATR, rounded down: orders go in at the next open and the price is not
  chased.
- Stop = close − 1.5 ATR, rounded down.
- Target = close + 2 × (close − stop), rounded up.
- Expiry = the H-th session after the signal date.
- Size: the smallest of three caps, in lots of 100:
  - 1% capital risk at the stop;
  - 20% of capital;
  - 2% of 20-day traded value.
- All numbers are in `config/trading.yaml` → `signals` and were chosen by the agent. The owner
  reviews them on 30 Nov.

### Immutable registry (migration 0006)
- `signal_runs` holds one row per (date, horizon, model, mode), with config, inputs, drift and
  counts. `signals` holds one row per stock per run.
- Updates and deletes are refused twice:
  - by an ORM hook (`ImmutableRecordError`);
  - by PostgreSQL triggers (`refuse_signal_change`), which also stop hand-written SQL.
- A second run for the same key is refused with a clear message.

## First runs (shadow, candidate models, 2026-11-25)
- **Signal date 25 Nov:** 14 stocks had a bar. The cross-section gate blocks all BUYs, which is
  correct. The daily collector backfills new stocks instead of refreshing old ones (see the
  collection log), so today's cross-section is too thin to rank.
- **Signal date 20 Nov:** 444 stocks had a bar and 139 were ranked above the floor.
  - With the raw score, ridge H5 would have issued 7 BUYs at "+0.5% to +1.1% expected".
  - With the calibrated line, all 27 top names had expected net returns of −1.1% to −1.5%, so
    the result was 0 BUY and 27 WATCH. LightGBM H10 gave the same: 0 BUY.
  - This matches ADR 0015: even the top quintile lost money after costs this year.

## Consequences
- A production BUY requires:
  1. a daily refresh of the whole market (a data decision for 30 Nov);
  2. an approved model;
  3. a market where the best names are expected to beat costs.
- Until then the engine correctly says WATCH or AVOID.
- M3.1 (backtester) will test these rules, including stops and targets, over history.
