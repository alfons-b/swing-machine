"""Technical feature set `tech_v1` (PRD §9.2, FR-014, FR-018; ADR 0013).

Inputs per instrument: total-return-adjusted OHLC (splits + dividends) and split-adjusted
volume from `adjusted_bars`, plus raw close x raw volume for traded value. Every feature
for day t uses only bars dated <= t: all windows are trailing and nothing is centred or
shifted forward (tested by recomputing on truncated history).

Changing any formula requires a new `FEATURE_SET` version; the definition hash in the
manifest changes with this file, so stale Parquet files are detectable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

FEATURE_SET = "tech_v1"
MIN_BARS = 60  # features before this many bars are left as NaN by the windows themselves


@dataclass(frozen=True)
class FeatureDef:
    name: str
    window: int
    description: str


DEFINITIONS: tuple[FeatureDef, ...] = (
    FeatureDef("ret_1", 1, "log return over 1 session (adjusted close)"),
    FeatureDef("ret_5", 5, "log return over 5 sessions"),
    FeatureDef("ret_10", 10, "log return over 10 sessions"),
    FeatureDef("ret_20", 20, "log return over 20 sessions"),
    FeatureDef("ret_60", 60, "log return over 60 sessions"),
    FeatureDef("vol_20", 20, "annualised std of daily log returns, 20 sessions"),
    FeatureDef("rsi_14", 14, "Wilder RSI, 14"),
    FeatureDef("macd_hist_pct", 35, "(MACD 12/26 - signal 9) / close"),
    FeatureDef("atr_14_pct", 14, "Wilder ATR 14 / close"),
    FeatureDef("adx_14", 28, "Wilder ADX 14"),
    FeatureDef("bb_pctb_20", 20, "Bollinger %B (20, 2 sd)"),
    FeatureDef("bb_width_20", 20, "Bollinger band width / middle band"),
    FeatureDef("dist_sma_20", 20, "close / SMA20 - 1"),
    FeatureDef("dist_sma_50", 50, "close / SMA50 - 1"),
    FeatureDef("dist_sma_200", 200, "close / SMA200 - 1"),
    FeatureDef("rel_vol_20", 20, "volume / mean volume of the previous 20 sessions"),
    FeatureDef("adv_20_idr", 20, "mean raw traded value (raw close x raw volume), 20 sessions"),
    FeatureDef("zero_vol_share_20", 20, "share of zero-volume sessions in the last 20"),
    FeatureDef("rs_market_20", 20, "ret_20 minus the cross-sectional median ret_20 that day"),
)
NAMES = tuple(d.name for d in DEFINITIONS)


def definition_hash() -> str:
    """Changes whenever this module's source changes."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def _wilder(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def instrument_features(df: pd.DataFrame) -> pd.DataFrame:
    """Per-instrument features. `df`: one instrument, sorted by date, columns
    open/high/low/close (adjusted), volume (split-adjusted), raw_close, raw_volume."""
    c, h, lo, v = df["close"], df["high"], df["low"], df["volume"]
    out = pd.DataFrame(index=df.index)
    lc = np.log(c)
    for n in (1, 5, 10, 20, 60):
        out[f"ret_{n}"] = lc.diff(n)
    out["vol_20"] = lc.diff().rolling(20).std() * np.sqrt(252)

    delta = c.diff()
    gain, loss = delta.clip(lower=0), (-delta).clip(lower=0)
    rs = _wilder(gain, 14) / _wilder(loss, 14)
    out["rsi_14"] = 100 - 100 / (1 + rs)

    ema12, ema26 = (
        c.ewm(span=12, adjust=False, min_periods=12).mean(),
        c.ewm(span=26, adjust=False, min_periods=26).mean(),
    )
    macd = ema12 - ema26
    out["macd_hist_pct"] = (macd - macd.ewm(span=9, adjust=False, min_periods=9).mean()) / c

    prev_c = c.shift(1)
    tr = pd.concat([h - lo, (h - prev_c).abs(), (lo - prev_c).abs()], axis=1).max(axis=1)
    atr = _wilder(tr, 14)
    out["atr_14_pct"] = atr / c
    up, down = h.diff(), -lo.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    plus_di = 100 * _wilder(plus_dm, 14) / atr
    minus_di = 100 * _wilder(minus_dm, 14) / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    out["adx_14"] = _wilder(dx, 14)

    mid = c.rolling(20).mean()
    sd = c.rolling(20).std()
    out["bb_pctb_20"] = (c - (mid - 2 * sd)) / (4 * sd)
    out["bb_width_20"] = 4 * sd / mid
    for n in (20, 50, 200):
        out[f"dist_sma_{n}"] = c / c.rolling(n).mean() - 1
    out["rel_vol_20"] = v / v.shift(1).rolling(20).mean()
    out["adv_20_idr"] = (df["raw_close"] * df["raw_volume"]).rolling(20).mean()
    out["zero_vol_share_20"] = (df["raw_volume"] == 0).astype(float).rolling(20).mean()
    return out.replace([np.inf, -np.inf], np.nan)


def cross_sectional(features: pd.DataFrame) -> pd.DataFrame:
    """Features needing all instruments on the same day (uses only that day's values)."""
    med = features.groupby("trade_date")["ret_20"].transform("median")
    features["rs_market_20"] = features["ret_20"] - med
    return features
