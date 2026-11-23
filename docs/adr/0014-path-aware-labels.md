# ADR 0014 — Path-aware multi-horizon labels `labels_v1`

- Status: Accepted
- Date: 2026-11-23
- Implements: PRD §10.1–10.3, §13.4; FR-021 / FR-022 / FR-024 (targets)

## Decision
- For a signal known after the close of day t:
  - **entry** = adjusted open of session t+1 (never the close of t);
  - **exit** = adjusted close of session t+H, for H ∈ {3, 5, 10, 20, 40} sessions.
- **net** = exit·(1−slip)·(1−sell) / (entry·(1+slip)·(1+buy)) − 1. Costs are Stockbit's from
  `config/trading.yaml` (0.1513% / 0.2513%); slippage is one IDX tick of the raw price per side.
- **MFE / MAE** are the highest high and lowest low over t+1…t+H against the entry (path risk,
  §10.3). **excess** = net − cross-sectional median net for the same t and H.
- No label for partial windows, and none when the entry session has no trade.
- Labels are targets only. Joining them as same-day features is forbidden.

## First build (2026-11-23) and what it says
456 stocks, 107,678 rows. Median net return by horizon: H3 −2.4%, H5 −2.7%, H10 −3.2%,
H20 −4.3%, H40 −6.4%. Share with positive net return 25–31%.

| Price band | Median gross H3 | Median net H3 | P(net > 0) |
|---|---|---|---|
| < Rp 100 | 0.0% | −4.3% | 15% |
| Rp 100–500 | −0.6% | −2.2% | 26% |
| Rp 500–2,000 | −0.4% | −1.9% | 27% |
| Rp 2,000–5,000 | −0.2% | −1.2% | 31% |
| ≥ Rp 5,000 | 0.0% | −1.1% | 31% |

- Round-trip cost ranges from about 1% for higher-priced stocks to about 4% for stocks under
  Rp 100, where one tick is 1–2% of the price. Short-horizon trading of very low-priced stocks
  cannot survive these costs.
- The year 2025-11 → 2026-11 was bearish (gross H20 medians −1% to −5%).
- **Consequences for M2.3/M2.4:** evaluate on *net* labels; require a price/liquidity floor in
  the BUY gate; the BUY threshold must beat the round trip, not zero.
- **Open assumption:** one full tick per side is conservative for liquid stocks. The backtester
  (M3.1) should measure it.
