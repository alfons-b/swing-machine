# ADR 0017 — Long-only backtester on walk-forward signals

- Status: Accepted
- Date: 2026-11-26
- Implements: PRD FR-037 (historical backtesting), FR-039 (multiple benchmarks), FR-040
  (transaction-cost simulation); M3.1

## Decision

### Execution model (`backtest/engine.py`, hand-checked in `tests/unit/test_backtest.py`)
- **Entry:**
  - An order from the close of t fills at the next session's raw open + 1 tick.
  - It is skipped in four cases:
    - the open is above `max_entry` (no chasing);
    - the open is at or below the stop;
    - there is no trade that day;
    - cash is short. When cash is short, the order is cut to the lots cash can pay for, and
      skipped only if that is zero.
  - Only one position per stock at a time.
- **Each session, in this order** (the worst case first):
  1. gap through the stop → exit at the open;
  2. gap through the target → exit at the open;
  3. low ≤ stop → exit at the stop. If the stop and the target are touched on the same day,
     the stop wins;
  4. high ≥ target → exit at the target;
  5. expiry → exit at the close.
- Exits pay 1 tick and the Stockbit sell cost. Entries pay 1 tick and the buy cost.
- **Prices:**
  - Levels are raw prices.
  - P&L uses total-return-adjusted prices, so dividends and splits during a trade are counted.
    A test covers a dividend ex-date in the middle of a trade.
- **Equity** is marked to market at each close. Positions still open at the end are closed at
  the last close.
- **Benchmarks:**
  - cash (0%);
  - an equal-weight buy-and-hold of every stock above the floor on the first day, with no costs
    (a generous yardstick).
  - No index data is collected yet, so there is no IHSG benchmark (OI-46).

### Signals without look-ahead (`backtest/run.py`)
- Scores come from the walk-forward model of each fold (ADR 0015).
- The excess calibration line is fitted only on *earlier* folds' out-of-sample predictions, so
  fold 0 never trades. A test checks that rewriting a later fold's outcomes leaves the earlier
  orders unchanged.
- The market drift uses labels closed by t.
- The BUY/WATCH/AVOID rules are exactly those of ADR 0016.
- **Simplifications** (also listed in each report):
  - every stock above the floor is treated as eligible (no delisting dates yet);
  - candidates are the rows with complete labels.

## First results (2026-05-04 → 2026-11-25, Rp 100 m, 120 signal days)
| Variant | Model | H | Trades | Hit rate | Mean trade | Total return | Max DD |
|---|---|---|---|---|---|---|---|
| **Rules as configured** | ridge | 5 | 0 (no day passed the hurdle) | – | – | **0.0%** | 0.0% |
| Hurdle ignored (always buy the top 20%) | ridge | 5 | 278 | 28% | −3.0% | −37.3% | −38.7% |
| Hurdle ignored | lgbm_reg | 5 | 329 | 35% | −1.9% | −24.6% | −32.0% |
| Hurdle ignored | ridge | 10 | 257 | 34% | −1.6% | −22.0% | −30.6% |
| Benchmark: equal-weight floor universe, no costs | – | – | – | – | – | −22.0% | – |
| Benchmark: cash | – | – | – | – | – | 0.0% | 0.0% |

### What it says
1. **The hurdle earned its keep.**
   - Over six falling months the configured rules never bought, which preserved capital.
   - Buying the model's top names anyway lost as much as or more than the whole market,
     because costs, slippage and stops at about 1.5 ATR add up.
2. **Ranking skill does not survive execution.**
   - The ranking IC is positive (ADR 0015), but trading it with these stop/target rules
     destroys the edge.
   - Most exits are expiry or stop; targets at 2R are rarely reached (≈6–15% of exits).
   - M3.2+ should study the exit rules: wider stops, or time exits only.
3. This is one regime and about 6 months. It is evidence, not a verdict.

## Consequences
- The rules stay as configured. There is no reason to loosen the hurdle.
- Exit-rule research is the next modelling step. It needs no new data, but must use the same
  walk-forward discipline.
- Reports live in `data/backtests/` (git-ignored, derived from vendor data).
