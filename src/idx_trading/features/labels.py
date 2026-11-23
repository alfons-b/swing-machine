"""Path-aware multi-horizon labels `labels_v1` (PRD §10, FR-021/022/024; ADR 0014).

For a signal on day t (known after the close of t):
  entry  = open of session t+1                       (never the close of t, PRD §13.4)
  exit   = close of session t+H, H in HORIZONS (sessions)
  net    = exit*(1-slip_x)*(1-sell) / (entry*(1+slip_e)*(1+buy)) - 1
  mfe    = max(high[t+1..t+H]) / entry - 1           (best excursion while holding)
  mae    = min(low[t+1..t+H]) / entry - 1            (worst excursion while holding)
  excess = net - cross-sectional median net for the same t and H
Prices are total-return adjusted; slippage is one IDX tick of the *raw* price per side
(config/trading.yaml). A label exists only when all H sessions after t exist, and not when
the entry session has no trade (no open). Labels use future data by definition: they are
targets only and must never be joined as features for the same day.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from idx_trading.ingestion.validation import idx_tick

LABEL_SET = "labels_v1"
HORIZONS = (3, 5, 10, 20, 40)


def _tick_frac(raw: pd.Series) -> pd.Series:
    return raw.map(lambda p: idx_tick(p) / p if p and p > 0 else np.nan)


def instrument_labels(df: pd.DataFrame, buy: float, sell: float, ticks_per_side: int = 1) -> pd.DataFrame:
    """`df`: one instrument sorted by date with open/high/low/close (adjusted) and raw_open, raw_close."""
    out = pd.DataFrame(index=df.index)
    entry = df["open"].shift(-1)
    slip_e = _tick_frac(df["raw_open"]).shift(-1) * ticks_per_side
    slip_x_all = _tick_frac(df["raw_close"]) * ticks_per_side
    n = len(df)
    for h in HORIZONS:
        exit_ = df["close"].shift(-h)
        slip_x = slip_x_all.shift(-h)
        net = exit_ * (1 - slip_x) * (1 - sell) / (entry * (1 + slip_e) * (1 + buy)) - 1
        # path over t+1..t+h: rolling window ending at t+h, then shifted back to t
        hi = df["high"].rolling(h).max().shift(-h)
        lo = df["low"].rolling(h).min().shift(-h)
        valid = pd.Series(np.arange(n) + h < n, index=df.index) & entry.notna()
        out[f"net_{h}"] = net.where(valid)
        out[f"mfe_{h}"] = (hi / entry - 1).where(valid)
        out[f"mae_{h}"] = (lo / entry - 1).where(valid)
    return out


def add_excess(labels: pd.DataFrame) -> pd.DataFrame:
    for h in HORIZONS:
        med = labels.groupby("trade_date")[f"net_{h}"].transform("median")
        labels[f"excess_{h}"] = labels[f"net_{h}"] - med
    return labels


LABEL_COLUMNS = tuple(f"{k}_{h}" for h in HORIZONS for k in ("net", "mfe", "mae", "excess"))
