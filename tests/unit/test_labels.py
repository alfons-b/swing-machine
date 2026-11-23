import numpy as np
import pandas as pd
import pytest

from idx_trading.features.labels import HORIZONS, add_excess, instrument_labels


def path():
    # 50 sessions; open/close/high/low designed by hand around 1,000
    n = 50
    close = np.full(n, 1000.0)
    close[1:6] = [1010, 1030, 990, 1050, 1040]
    df = pd.DataFrame({"open": close.copy(), "high": close + 20, "low": close - 20, "close": close})
    df.loc[1, "open"] = 1005.0  # entry for a signal on day 0
    df["raw_open"], df["raw_close"] = df["open"], df["close"]
    return df


def test_entry_is_next_open_and_values_by_hand():
    df = path()
    lab = instrument_labels(df, buy=0.0, sell=0.0, ticks_per_side=0)
    # signal day 0 -> enter at open of day 1 (1005), exit at close of day 3 (990)
    assert lab.loc[0, "net_3"] == pytest.approx(990 / 1005 - 1)
    # path days 1..3: highs 1030, 1050, 1010 -> mfe vs entry 1005; lows 990, 1010, 970
    assert lab.loc[0, "mfe_3"] == pytest.approx(1050 / 1005 - 1)
    assert lab.loc[0, "mae_3"] == pytest.approx(970 / 1005 - 1)
    assert lab.loc[0, "net_5"] == pytest.approx(1040 / 1005 - 1)


def test_costs_and_slippage():
    df = path()
    lab = instrument_labels(df, buy=0.001513, sell=0.002513, ticks_per_side=1)
    entry, exit_ = 1005 * (1 + 5 / 1005) * 1.001513, 990 * (1 - 5 / 990) * (1 - 0.002513)
    assert lab.loc[0, "net_3"] == pytest.approx(exit_ / entry - 1)  # tick 5 for 500-2,000


def test_no_partial_windows_and_no_entry_without_trade():
    df = path()
    df.loc[11, "open"] = np.nan  # no trade on day 11 -> a signal on day 10 cannot enter
    lab = instrument_labels(df, 0.0, 0.0, 0)
    n = len(df)
    for h in HORIZONS:
        assert lab[f"net_{h}"].iloc[n - h :].isna().all()
        assert lab[f"net_{h}"].iloc[: n - h].drop(index=10, errors="ignore").notna().all()
    assert np.isnan(lab.loc[10, "net_3"])


def test_excess_vs_same_day_median():
    lab = pd.DataFrame({"trade_date": [1, 1, 1], **{f"net_{h}": [0.0, 0.1, 0.3] for h in HORIZONS}})
    out = add_excess(lab)
    assert list(out["excess_5"].round(6)) == [-0.1, 0.0, 0.2]
