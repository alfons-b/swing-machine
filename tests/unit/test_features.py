from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from idx_trading.features.technical import NAMES, cross_sectional, instrument_features


def frame(n=260, seed=1):
    rng = np.random.default_rng(seed)
    c = 1000 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    h, lo = c * (1 + rng.uniform(0, 0.02, n)), c * (1 - rng.uniform(0, 0.02, n))
    v = rng.integers(0, 1_000_000, n).astype(float)
    d = [date(2025, 1, 1) + timedelta(days=i) for i in range(n)]
    return pd.DataFrame(
        {
            "trade_date": d,
            "open": c,
            "high": h,
            "low": lo,
            "close": c,
            "volume": v,
            "raw_close": c,
            "raw_volume": v,
        }
    )


def test_known_values():
    df = frame()
    f = instrument_features(df)
    t = 100
    assert f["ret_5"][t] == pytest.approx(np.log(df["close"][t] / df["close"][t - 5]))
    assert f["dist_sma_20"][t] == pytest.approx(df["close"][t] / df["close"][t - 19 : t + 1].mean() - 1)
    assert f["rel_vol_20"][t] == pytest.approx(df["volume"][t] / df["volume"][t - 20 : t].mean())
    assert np.isnan(f["dist_sma_200"][150]) and not np.isnan(f["dist_sma_200"][210])
    up = frame()
    up["close"] = np.arange(1, 261, dtype=float) + 100
    assert instrument_features(up)["rsi_14"].iloc[-1] == pytest.approx(100)


def test_no_look_ahead():
    """Features for day t must not change when later bars are removed."""
    df = frame()
    full = instrument_features(df)
    for t in (60, 120, 205, 259):
        cut = instrument_features(df.iloc[: t + 1].copy())
        a, b = full.iloc[t], cut.iloc[t]
        pd.testing.assert_series_equal(a, b, check_names=False)


def test_cross_sectional_median_uses_same_day_only():
    f = pd.DataFrame({"trade_date": [1, 1, 1, 2, 2], "ret_20": [0.1, 0.2, 0.3, -0.1, 0.5]})
    out = cross_sectional(f.copy())
    assert list(out["rs_market_20"].round(6)) == [-0.1, 0.0, 0.1, -0.3, 0.3]


def test_all_defined_features_are_produced():
    f = cross_sectional(instrument_features(frame()).assign(trade_date=frame()["trade_date"]))
    assert set(NAMES) <= set(f.columns)
