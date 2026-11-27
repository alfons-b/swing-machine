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

## Addendum 2026-11-27 — exit-rule study (OI-47) and a correction

The same walk-forward signals were run with stop widths of 1–4 ATR or none, and targets of
1R–3R or none (). This is research only: picking the best row of
these tables on the same period would be in-sample selection, so **no configuration was
changed**.

### Correction to the first results
The "0 trades with the configured rules" result above holds for **ridge** only. With the
LightGBM regressor the configured rules (hurdle on, stop 1.5 ATR, target 2R) did trade:
- H5: 20 trades, −0.6%.
- H10: 82 trades, hit rate 34%, **−10.5%** (max drawdown −16.7%). That is better than the
  market (−22%) but a loss.

The +0.5% hurdle therefore does not always protect capital. The calibration line, fitted on
earlier folds from a different phase of the year, can be optimistic.

### Findings
1. **Without the hurdle no exit rule rescues the strategy.** Every one of the 20 stop/target
   pairs loses, at H5 and H10:
   - H5: −15% to −47%;
   - H10: −8% to −22%.
   Wider stops lose less than tight ones: 1 ATR stops are the worst. Targets rarely matter.
2. **With the hurdle the trade counts are too small to conclude anything.** At H5 there were
   15–23 trades, with a total return between −2.8% and +1.4%. That is noise.
3. **With no stop, H10 with the hurdle lost only −0.9% (161 trades).** This row was found by
   looking at the results, so it is a hypothesis to re-test on later data, not a setting to
   adopt.

### Consequences
- A model may be approved only if its *own* backtest with the configured rules is acceptable:
  - ridge H5: 0 trades, so it adds nothing;
  - LightGBM H10: −10.5%, so it is not acceptable;
  - none of the current candidates qualifies.
- The evaluation of 30 Nov should state this.

### H5 table (LightGBM regressor)
| stop ATR | reward:risk | hurdle | trades | hit | mean trade | total | max DD | stop/target/expiry |
|---|---|---|---|---|---|---|---|---|
| 1.0 | 1.0 | on | 23 | 48% | -0.54% | -2.8% | -4.0% | 7/8/8 |
| 1.0 | 2.0 | on | 22 | 50% | +0.18% | +0.3% | -3.9% | 7/4/11 |
| 1.0 | 3.0 | on | 20 | 55% | +0.28% | +1.4% | -3.8% | 5/1/14 |
| 1.0 | none | on | 19 | 58% | +0.13% | +0.8% | -3.8% | 4/0/15 |
| 1.5 | 1.0 | on | 22 | 50% | -0.66% | -1.2% | -2.9% | 4/5/13 |
| 1.5 | 2.0 | on | 20 | 55% | -0.52% | -0.6% | -2.8% | 4/1/15 |
| 1.5 | 3.0 | on | 19 | 58% | -0.34% | +0.1% | -2.8% | 3/0/16 |
| 1.5 | none | on | 19 | 58% | -0.34% | +0.1% | -2.8% | 3/0/16 |
| 2.5 | 1.0 | on | 19 | 47% | -1.07% | -0.5% | -1.7% | 1/1/17 |
| 2.5 | 2.0 | on | 18 | 50% | -0.81% | -0.1% | -1.7% | 0/0/18 |
| 2.5 | 3.0 | on | 18 | 50% | -0.81% | -0.1% | -1.7% | 0/0/18 |
| 2.5 | none | on | 18 | 50% | -0.81% | -0.1% | -1.7% | 0/0/18 |
| 4.0 | 1.0 | on | 19 | 47% | -1.64% | -0.8% | -1.6% | 1/0/18 |
| 4.0 | 2.0 | on | 19 | 47% | -1.64% | -0.8% | -1.6% | 1/0/18 |
| 4.0 | 3.0 | on | 19 | 47% | -1.64% | -0.8% | -1.6% | 1/0/18 |
| 4.0 | none | on | 19 | 47% | -1.64% | -0.8% | -1.6% | 1/0/18 |
| none | 1.0 | on | 15 | 40% | -4.57% | -0.6% | -0.8% | 0/0/15 |
| none | 2.0 | on | 15 | 40% | -4.57% | -0.6% | -0.8% | 0/0/15 |
| none | 3.0 | on | 15 | 40% | -4.57% | -0.6% | -0.8% | 0/0/15 |
| none | none | on | 15 | 40% | -4.57% | -0.6% | -0.8% | 0/0/15 |
| 1.0 | 1.0 | off | 343 | 41% | -2.02% | -47.2% | -48.9% | 158/135/50 |
| 1.0 | 2.0 | off | 325 | 34% | -1.69% | -42.4% | -43.3% | 151/47/127 |
| 1.0 | 3.0 | off | 325 | 32% | -1.31% | -33.4% | -34.0% | 153/21/151 |
| 1.0 | none | off | 325 | 33% | -1.74% | -34.8% | -36.5% | 156/0/169 |
| 1.5 | 1.0 | off | 369 | 39% | -1.84% | -32.7% | -34.7% | 103/99/167 |
| 1.5 | 2.0 | off | 329 | 35% | -1.90% | -24.6% | -32.0% | 96/25/208 |
| 1.5 | 3.0 | off | 317 | 36% | -1.81% | -21.5% | -32.0% | 105/9/203 |
| 1.5 | none | off | 334 | 39% | -1.05% | -19.2% | -31.8% | 93/0/241 |
| 2.5 | 1.0 | off | 430 | 39% | -0.85% | -27.1% | -27.6% | 55/65/310 |
| 2.5 | 2.0 | off | 405 | 37% | -0.70% | -23.0% | -25.1% | 48/9/348 |
| 2.5 | 3.0 | off | 439 | 37% | -0.79% | -14.8% | -22.2% | 47/2/390 |
| 2.5 | none | off | 407 | 35% | -0.91% | -18.5% | -22.2% | 45/0/362 |
| 4.0 | 1.0 | off | 510 | 38% | -0.87% | -26.6% | -29.8% | 33/21/456 |
| 4.0 | 2.0 | off | 512 | 37% | -0.72% | -24.3% | -25.7% | 31/3/478 |
| 4.0 | 3.0 | off | 522 | 37% | -0.83% | -23.5% | -26.2% | 35/2/485 |
| 4.0 | none | off | 509 | 36% | -0.96% | -25.2% | -27.5% | 34/0/475 |
| none | 1.0 | off | 1132 | 33% | -2.07% | -22.0% | -22.1% | 0/0/1132 |
| none | 2.0 | off | 1132 | 33% | -2.07% | -22.0% | -22.1% | 0/0/1132 |
| none | 3.0 | off | 1132 | 33% | -2.07% | -22.0% | -22.1% | 0/0/1132 |
| none | none | off | 1132 | 33% | -2.07% | -22.0% | -22.1% | 0/0/1132 |

### H10 table (LightGBM regressor)
| stop ATR | reward:risk | hurdle | trades | hit | mean trade | total | max DD | stop/target/expiry |
|---|---|---|---|---|---|---|---|---|
| 1.0 | 1.0 | on | 112 | 50% | -1.35% | -14.4% | -18.6% | 46/55/11 |
| 1.0 | 2.0 | on | 98 | 35% | -1.35% | -17.0% | -20.9% | 47/23/28 |
| 1.0 | 3.0 | on | 89 | 34% | -1.60% | -14.4% | -19.9% | 45/12/32 |
| 1.0 | none | on | 75 | 31% | -2.11% | -13.7% | -21.4% | 40/0/35 |
| 1.5 | 1.0 | on | 110 | 45% | -0.86% | -13.7% | -19.4% | 39/44/27 |
| 1.5 | 2.0 | on | 82 | 34% | -1.34% | -10.5% | -16.7% | 29/10/43 |
| 1.5 | 3.0 | on | 78 | 35% | -0.77% | -4.7% | -14.1% | 26/7/45 |
| 1.5 | none | on | 70 | 36% | -0.82% | -12.8% | -21.8% | 24/0/46 |
| 2.5 | 1.0 | on | 92 | 35% | -1.52% | -12.3% | -13.5% | 18/18/56 |
| 2.5 | 2.0 | on | 94 | 40% | -1.65% | -11.5% | -12.9% | 19/4/71 |
| 2.5 | 3.0 | on | 86 | 36% | -1.41% | -11.1% | -14.4% | 18/4/64 |
| 2.5 | none | on | 92 | 37% | -1.52% | -10.1% | -12.5% | 18/0/74 |
| 4.0 | 1.0 | on | 104 | 42% | -1.15% | -9.2% | -11.4% | 11/11/82 |
| 4.0 | 2.0 | on | 106 | 41% | -0.98% | -6.6% | -8.9% | 10/3/93 |
| 4.0 | 3.0 | on | 109 | 43% | -0.48% | -5.5% | -8.2% | 10/2/97 |
| 4.0 | none | on | 102 | 40% | -1.14% | -8.7% | -11.3% | 10/0/92 |
| none | 1.0 | on | 161 | 39% | -0.72% | -0.9% | -2.2% | 0/0/161 |
| none | 2.0 | on | 161 | 39% | -0.72% | -0.9% | -2.2% | 0/0/161 |
| none | 3.0 | on | 161 | 39% | -0.72% | -0.9% | -2.2% | 0/0/161 |
| none | none | on | 161 | 39% | -0.72% | -0.9% | -2.2% | 0/0/161 |
| 1.0 | 1.0 | off | 349 | 45% | -1.42% | -40.8% | -42.9% | 171/153/25 |
| 1.0 | 2.0 | off | 288 | 31% | -1.29% | -30.8% | -32.9% | 169/59/60 |
| 1.0 | 3.0 | off | 266 | 27% | -1.19% | -28.3% | -30.4% | 159/31/76 |
| 1.0 | none | off | 240 | 28% | -0.98% | -28.0% | -32.3% | 140/0/100 |
| 1.5 | 1.0 | off | 312 | 40% | -1.35% | -30.0% | -36.1% | 129/110/73 |
| 1.5 | 2.0 | off | 276 | 36% | -0.68% | -14.6% | -28.9% | 112/37/127 |
| 1.5 | 3.0 | off | 271 | 33% | -1.30% | -13.7% | -28.0% | 126/13/132 |
| 1.5 | none | off | 252 | 37% | -0.32% | -21.6% | -26.3% | 109/0/143 |
| 2.5 | 1.0 | off | 338 | 41% | -1.20% | -15.8% | -22.6% | 85/64/189 |
| 2.5 | 2.0 | off | 296 | 41% | +0.19% | -8.2% | -23.2% | 75/21/200 |
| 2.5 | 3.0 | off | 300 | 40% | -0.04% | -6.3% | -23.2% | 76/11/213 |
| 2.5 | none | off | 306 | 41% | -0.51% | -21.7% | -23.2% | 78/0/228 |
| 4.0 | 1.0 | off | 371 | 43% | -0.37% | -18.2% | -23.8% | 45/36/290 |
| 4.0 | 2.0 | off | 329 | 40% | -0.47% | -19.0% | -24.0% | 45/6/278 |
| 4.0 | 3.0 | off | 321 | 42% | +0.10% | -17.3% | -23.4% | 40/4/277 |
| 4.0 | none | off | 326 | 40% | -0.62% | -21.1% | -23.5% | 47/0/279 |
| none | 1.0 | off | 678 | 33% | -3.21% | -20.5% | -20.9% | 0/0/678 |
| none | 2.0 | off | 678 | 33% | -3.21% | -20.5% | -20.9% | 0/0/678 |
| none | 3.0 | off | 678 | 33% | -3.21% | -20.5% | -20.9% | 0/0/678 |
| none | none | off | 678 | 33% | -3.21% | -20.5% | -20.9% | 0/0/678 |
