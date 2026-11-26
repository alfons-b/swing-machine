"""M3.1: backtester against hand-computed cases (ADR 0017)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from idx_trading.backtest.engine import Costs, Order, equal_weight_benchmark, metrics, simulate

DAYS = [d.date() for d in pd.bdate_range("2026-03-02", periods=8)]
NO_COST = Costs(buy=0.0, sell=0.0, ticks=0)


def bars(rows: list[tuple[float, float, float, float]], factor: float = 1.0) -> pd.DataFrame:
    """rows of raw (open, high, low, close); adjusted = raw x factor."""
    df = pd.DataFrame(rows, columns=["raw_open", "high", "low", "raw_close"], index=DAYS[: len(rows)])
    df["open"], df["close"] = df["raw_open"] * factor, df["raw_close"] * factor
    df["high"], df["low"] = df["high"] * factor, df["low"] * factor
    return df


def order(**kw: object) -> Order:
    base: dict[str, object] = {
        "signal_date": DAYS[0],
        "instrument_id": 1,
        "ticker": "AAAA",
        "max_entry": 1020.0,
        "stop": 940.0,
        "target": 1120.0,
        "expiry": DAYS[5],
        "lots": 10,
    }
    base.update(kw)
    return Order(**base)  # type: ignore[arg-type]


FLAT = [(1000, 1010, 990, 1000)] * 8


def test_target_hit_with_costs_and_slippage_by_hand():
    b = bars([(1000, 1010, 990, 1000), (1000, 1030, 995, 1020), (1020, 1125, 1010, 1100), *FLAT[3:]])
    c = Costs(buy=0.001513, sell=0.002513, ticks=1)
    res = simulate([order()], {1: b}, DAYS, 100e6, c)
    t = res.trades[0]
    # entry day 2: open 1000 + 1 tick (5) = 1005; 1,000 shares
    assert (t.entry_date, t.entry_fill, t.shares) == (DAYS[1], 1005.0, 1000)
    assert t.cost_in == pytest.approx(1000 * 1005 * 1.001513)
    # day 3: high 1125 >= target 1120 -> exit at 1120 - 1 tick (5) = 1115, minus 0.2513%
    assert (t.exit_date, t.exit_reason) == (DAYS[2], "target")
    assert t.proceeds == pytest.approx(1000 * 1115 * (1 - 0.002513))
    assert res.equity.iloc[-1] == pytest.approx(100e6 + t.pnl)
    assert t.net_return == pytest.approx(1115 * 0.997487 / (1005 * 1.001513) - 1)


def test_stop_first_when_both_touched_and_gap_through_stop():
    both = bars([(1000, 1010, 990, 1000), (1000, 1000, 1000, 1000), (1000, 1130, 930, 1000), *FLAT[3:]])
    t = simulate([order()], {1: both}, DAYS, 100e6, NO_COST).trades[0]
    assert (t.exit_reason, t.proceeds) == ("stop", 940 * 1000)
    gap = bars([(1000, 1010, 990, 1000), (1000, 1000, 1000, 1000), (900, 920, 880, 900), *FLAT[3:]])
    t = simulate([order()], {1: gap}, DAYS, 100e6, NO_COST).trades[0]
    assert (t.exit_reason, t.proceeds) == ("stop (gap)", 900 * 1000)  # filled at the open, not 940


def test_expiry_exit_at_close_and_end_of_data():
    t = simulate([order()], {1: bars(FLAT)}, DAYS, 100e6, NO_COST).trades[0]
    assert (t.exit_date, t.exit_reason) == (DAYS[5], "expiry")
    t = simulate([order(expiry=date(2027, 1, 1))], {1: bars(FLAT)}, DAYS, 100e6, NO_COST).trades[0]
    assert (t.exit_date, t.exit_reason) == (DAYS[-1], "end of data")


def test_skips_gap_above_max_entry_below_stop_no_trade_cash_and_duplicates():
    hi = bars([(1000, 1010, 990, 1000), (1050, 1060, 1040, 1050), *FLAT[2:]])
    assert "above max entry" in str(simulate([order()], {1: hi}, DAYS, 100e6, NO_COST).trades[0].skipped)
    lo = bars([(1000, 1010, 990, 1000), (930, 940, 920, 930), *FLAT[2:]])
    assert "below the stop" in str(simulate([order()], {1: lo}, DAYS, 100e6, NO_COST).trades[0].skipped)
    missing = bars(FLAT).drop(index=DAYS[1])
    assert (
        simulate([order()], {1: missing}, DAYS, 100e6, NO_COST).trades[0].skipped
        == "no trade on the entry day"
    )
    # cash: 250,000 buys 2 lots at 1,000 (not 10)
    t = simulate([order()], {1: bars(FLAT)}, DAYS, 250_000, NO_COST).trades[0]
    assert t.shares == 200
    assert simulate([order()], {1: bars(FLAT)}, DAYS, 50_000, NO_COST).trades[0].skipped == "not enough cash"
    two = [order(), order(signal_date=DAYS[1])]
    res = simulate(two, {1: bars(FLAT)}, DAYS, 100e6, NO_COST)
    assert sorted(str(t.skipped) for t in res.trades) == ["None", "already held"]


def test_dividend_during_trade_uses_adjusted_prices():
    """Raw price drops 50 on an ex-date; adjusted history is scaled so no loss is shown."""
    raw = [(1000, 1000, 1000, 1000)] * 3 + [(950, 950, 950, 950)] * 5
    b = bars(raw)
    # adjusted series: before the ex-date (days 0-2) x 0.95, after x 1.0
    for col in ("open", "high", "low", "close"):
        b.loc[DAYS[:3], col] = b.loc[DAYS[:3], col] * 0.95
    o = order(stop=900.0, target=2000.0, expiry=DAYS[6])
    t = simulate([o], {1: b}, DAYS, 100e6, NO_COST).trades[0]
    # bought at raw 1000 (adj 950), sold at raw 950 (adj 950): total return 0
    assert t.net_return == pytest.approx(0.0)
    assert t.exit_reason == "expiry"


def test_equity_marks_to_market_and_benchmark():
    rows = [(1000, 1000, 1000, 1000), (1000, 1000, 1000, 1000), (1000, 1100, 1000, 1100)] + [
        (1100, 1100, 1100, 1100)
    ] * 5
    b = bars(rows)
    res = simulate([order(target=5000.0)], {1: b}, DAYS, 1e6, NO_COST)
    assert res.equity[DAYS[2]] == pytest.approx(1e6 + 1000 * 100)  # 1,000 shares up Rp 100
    bench = equal_weight_benchmark({1: b, 2: bars(FLAT)}, DAYS, 1e6)
    assert bench.iloc[-1] == pytest.approx(1e6 * (1.1 + 1.0) / 2)
    res.benchmarks["equal_weight"] = bench
    m = metrics(res)
    assert m["trades"] == 1 and m["benchmark_equal_weight"] == pytest.approx(0.05)
    assert m["max_drawdown"] <= 0


# --- historical orders: no look-ahead ---------------------------------------------------------


def _hist(seed: int = 0):
    import numpy as np

    from idx_trading.trading_config import load_trading_config

    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2026-01-05", periods=90)
    rows, oos = [], []
    for i, d in enumerate(days[:80]):
        for k in range(30):
            score = rng.normal(0, 0.01)
            rows.append(
                {
                    "instrument_id": k,
                    "ticker": f"S{k:02d}",
                    "trade_date": d,
                    "raw_close": 1000.0,
                    "adv_20_idr": 5e9,
                    "atr_14_pct": 0.03,
                }
            )
            oos.append(
                {
                    "instrument_id": k,
                    "trade_date": d,
                    "score": score,
                    "excess": 2 * score + rng.normal(0, 0.005),
                    "fold": i // 20,
                }
            )
    labels = pd.DataFrame(
        [{"trade_date": d, "net_5": 0.02, "end_5": days[i + 5]} for i, d in enumerate(days[:80])]
    )
    cfg = load_trading_config()
    cfg = cfg.model_copy(update={"signals": cfg.signals.model_copy(update={"min_cross_section": 10})})
    return pd.DataFrame(rows), pd.DataFrame(oos), labels, cfg, [d.date() for d in days]


def test_historical_orders_use_only_earlier_folds():
    from idx_trading.backtest.run import build_orders

    data, oos, labels, cfg, sessions = _hist()
    orders, info = build_orders(data, oos, labels, cfg, 5, sessions)
    assert orders and info["days_with_buys"] > 0
    first_test = oos[oos["fold"] == 1]["trade_date"].min().date()
    assert min(o.signal_date for o in orders) >= first_test  # fold 0 has no calibration
    # rewriting fold 3's outcomes must not change any order of folds 1-2
    cut = oos[oos["fold"] == 3]["trade_date"].min().date()
    oos2 = oos.copy()
    oos2.loc[oos2["fold"] == 3, "excess"] = -oos2.loc[oos2["fold"] == 3, "excess"]
    orders2, _ = build_orders(data, oos2, labels, cfg, 5, sessions)
    early = [o for o in orders if o.signal_date < cut]
    assert early == [o for o in orders2 if o.signal_date < cut]
    # the hurdle is honoured: a strongly negative drift leaves no BUY unless it is ignored
    bad = labels.assign(net_5=-0.05)
    assert build_orders(data, oos, bad, cfg, 5, sessions)[0] == []
    assert build_orders(data, oos, bad, cfg, 5, sessions, ignore_hurdle=True)[0]
