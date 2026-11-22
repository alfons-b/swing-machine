# ADR 0013 — Feature store `tech_v1`

- Status: Accepted
- Date: 2026-11-22
- Implements: FR-014 (technical features), FR-018 (point in time, technical part), PRD §9.2

## Decision
- 19 technical features (`features/technical.py`):
  - returns over 1/5/10/20/60 sessions and 20-day volatility;
  - Wilder RSI 14, ATR 14 and ADX 14; MACD histogram;
  - Bollinger %B and width; distance to SMA 20/50/200;
  - relative volume, 20-day average traded value (IDR), share of zero-volume sessions;
  - relative strength against the cross-sectional median.
- **Inputs:** total-return-adjusted OHLC and split-adjusted volume from `adjusted_bars`; raw
  close × raw volume for traded value. Invalid bars and no-trade days are excluded.
- **Point in time:** every window is trailing. `build_features(as_of=…)` also limits the
  corporate-action adjustments to that date. A test recomputes on truncated history and
  requires identical values.
- **Versioning:** the feature-set name plus a hash of the module source are stored in the
  manifest; reading a file built from other definitions is refused.
- **Storage:** Parquet in `data/features/` (git-ignored), the analytical layer of PRD §7.1.
- Fundamental features (PIT ratios from ADR 0010/0012) come in `fund_v1` once more reports
  exist.

## First build (2026-11-22)
440 stocks, 103,866 rows, 2025-11-21 → 2026-11-20; 510 stocks skipped (fewer than 60 bars yet).
Median 20-day traded value Rp 0.9 billion per day; BBCA Rp 1.25 trillion. This informs the
empirical liquidity cut-off (OI-09).
