# ADR 0015 — Walk-forward evaluation, candidate models and the model registry

- Status: Accepted
- Date: 2026-11-24
- Implements: PRD FR-019 (baselines), FR-020 (model competition), FR-023 (calibration
  metrics), FR-026 (model registry), FR-038 (walk-forward validation); M2.3

## Decision

### Dataset
- Each row is one stock on day t: `tech_v1` features of t joined with `labels_v1` targets of t.
  The join key is (instrument_id, trade_date). Labels are never model inputs; the input list is
  the closed tuple `FEATURES`.
- **Floor**, applied per day: raw close ≥ Rp 200 and 20-day mean traded value ≥ Rp 1 bn
  (`config/trading.yaml` → `universe`). Rows also need at least 20 sessions of history.
  The floor follows ADR 0014: below Rp 200 one tick is 0.5–1% of the price.
- **Inputs** are per-day percentile ranks of the features. A rank uses only values from the same
  day, so it adds no look-ahead, and it removes market-wide level drift between periods.

### Walk-forward
- Test blocks are 20 consecutive sessions, and the training window expands.
- Minimum training is 60 sessions.
- **Purging:** a training row is used only if its label's exit session (`end_H`, now stored with
  `labels_v1`) is before the test block starts. Using the exit session is exact even for stocks
  with trading gaps. An extra embargo of 2 sessions follows.
- Tests check the following:
  - Scrambling every label after fold 0 leaves fold 0's predictions unchanged.
  - Two runs give byte-identical reports.

### Candidates
| Name | What it does | Target |
|---|---|---|
| `momentum` | No fitting; ranks by `rs_market_20` | – |
| `ridge` | Ridge (α = 10) | excess return |
| `logistic` | Logistic regression (C = 0.1) | P(net > 0) |
| `lgbm_cls` | LightGBM classifier | P(net > 0) |
| `lgbm_reg` | LightGBM regressor | excess return, clipped at p1/p99 |

- Models with no seed are deterministic. LightGBM uses a fixed seed, `deterministic=True` and
  one thread.

### Metrics
- Metrics are computed on pooled out-of-sample predictions, and per fold.
- **Ranking**, per day and then averaged:
  - Spearman IC against net return;
  - mean net return of the top 20% (the candidate BUY list);
  - hit rate;
  - top-minus-bottom spread;
  - the buy-everything mean as a reference.
- **Calibration**, for probabilistic models:
  - Brier score, and Brier skill against the training base rate;
  - log loss;
  - ECE;
  - a 10-bin reliability table.

### Registry (`model_versions`, migration 0005)
- `idx models train` fits on all eligible rows and stores the following:
  - the model, pickled, with its SHA-256;
  - the data fingerprint;
  - the feature definition hash;
  - the label set and code version;
  - the training window;
  - its walk-forward summary.
- New models are always registered as `candidate`.
- `idx models approve <id> --by --note` is the only path to `approved`:
  - The model must have a walk-forward evaluation.
  - Its features must match the current definitions.
  - Approval retires the previous approved model for that horizon.
- `load` refuses a file whose hash differs.
- No rows are deleted.

## First comparison (2026-11-24)
- Data: 273 stocks above the floor, about 34k rows per horizon, 2025-12-19 → 2026-11.
- Folds: 5–7 per horizon.
- Reports: `data/models/reports/wf-h<H>-<fingerprint>.{json,md}`.

| H | Model | OOS IC | Top-20% net | All net | Spread |
|---|---|---|---|---|---|
| 5 | ridge | 0.133 | −1.82% | −2.49% | +1.87% |
| 5 | lgbm_reg | 0.125 | −1.88% | −2.49% | +1.74% |
| 5 | momentum | −0.003 | −2.72% | −2.49% | −0.05% |
| 10 | ridge | 0.145 | −2.13% | −3.79% | +3.08% |
| 10 | lgbm_reg | 0.146 | −2.93% | −3.79% | +3.36% |
| 20 | ridge | 0.172 | −2.46% | −4.93% | +4.21% |
| 40 | ridge | 0.187 | −0.92% | −5.90% | +7.82% |
| 40 | logistic | 0.197 | −1.58% | −5.90% | +9.21% |

### What it says
1. **Ranking works.** Regression on excess return (ridge, LightGBM) ranks stocks consistently.
   IC is positive on 75–80% of days and in nearly every fold.
2. **It is not a leak.**
   - The edge comes from features known before entry. Single-feature ICs:
     `atr_14_pct` −0.16, `vol_20` −0.14, `bb_width_20` −0.11, `dist_sma_200` +0.12.
   - In this falling year, low-volatility stocks lost less after costs than high-volatility
     ones.
   - That is a regime-dependent effect; it is not a timeless law.
3. **No model makes money in absolute terms in this year.**
   - Even the top quintile has a negative mean net return at every horizon.
   - The models choose losers that lose less.
   - M2.4 must therefore allow "no BUY today". A BUY needs a positive expected net return, not
     just a high rank.
4. **Probabilities are not yet calibrated.**
   - Brier skill is about 0 or negative, and ECE is 0.03–0.2.
   - The classifiers target P(net > 0). High-volatility stocks have a higher chance of a
     positive outcome but a lower mean, so these models rank worse than the regressions.
   - Before any probability is shown as "confidence" (FR-023), it needs a calibration step
     fitted within each training fold, such as isotonic regression.
5. **Statistical caveat.**
   - The IC t-statistics are overstated: daily ICs of overlapping H-day labels are
     autocorrelated.
   - The sample covers about one year and one regime. Treat it as evidence of ranking ability,
     not as a forecast of returns.
6. **Not approved.** Candidates `ridge` and `lgbm_reg` for H5 and H10 are registered. Approval is
   the owner's decision; it is not taken here.

## Consequences
- M2.4 ranks by a regression score. It also needs the following gates:
  - expected net > 0;
  - the floor;
  - the data-quality gates.
- `dist_sma_200` is empty until stocks have 200 bars. The linear models keep the column
  (imputed 0.5) so the input shape is stable.
