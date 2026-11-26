"""Long-only daily backtester (M3.1, ADR 0017).

Execution model, chosen to be conservative and simple enough to check by hand:
- A signal from the close of day t is an order for the open of the next session t+1.
  It fills at the raw open + `ticks` ticks (slippage) if the open is <= `max_entry`; a higher
  open is skipped (no chasing). An open already at or below the stop is skipped too.
- Shares = lots x lot size, reduced to the whole lots that cash can pay for (with the buy
  cost). Zero lots -> skipped. One position per stock at a time.
- From the entry day on, each session in this order:
    open <= stop          -> exit at the open (gap through the stop)     [not on the entry day]
    open >= target        -> exit at the open (gap through the target)   [not on the entry day]
    low <= stop           -> exit at the stop          (stop first if both are touched: worst case)
    high >= target        -> exit at the target
    session >= expiry     -> exit at the close
  Exits pay `ticks` ticks of slippage and the sell cost. A day without a trade is skipped.
- Prices: stops and targets are raw prices. P&L uses total-return-adjusted prices, so
  dividends and splits during a trade are accounted for: a raw level is converted with the
  entry-day ratio adjusted/raw, and the position is worth shares x fill x adj_t / adj_fill.
- Positions still open on the last session are closed at its close ("end of data").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from idx_trading.signals.engine import tick


@dataclass(frozen=True)
class Order:
    signal_date: date
    instrument_id: int
    ticker: str
    max_entry: float
    stop: float
    target: float
    expiry: date
    lots: int


@dataclass
class Trade:
    order: Order
    entry_date: date | None = None
    shares: int = 0
    entry_fill: float = 0.0  # raw price paid per share (incl. slippage, excl. cost)
    cost_in: float = 0.0  # cash paid incl. buy cost
    exit_date: date | None = None
    exit_reason: str = ""
    proceeds: float = 0.0  # cash received after slippage and sell cost
    skipped: str | None = None
    _f: float = 1.0  # adjusted / raw on the entry day

    @property
    def pnl(self) -> float:
        return self.proceeds - self.cost_in

    @property
    def net_return(self) -> float:
        return self.pnl / self.cost_in if self.cost_in else float("nan")


@dataclass(frozen=True)
class Costs:
    buy: float
    sell: float
    ticks: int = 1
    lot_size: int = 100


@dataclass
class Result:
    trades: list[Trade]
    equity: pd.Series  # mark-to-market at each session close
    capital: float
    benchmarks: dict[str, pd.Series] = field(default_factory=dict)


def _slip(raw_px: float, ticks: int) -> float:
    return tick(raw_px) * ticks if raw_px > 0 else 0.0


def simulate(
    orders: list[Order], bars: dict[int, pd.DataFrame], sessions: list[date], capital: float, costs: Costs
) -> Result:
    """`bars[iid]`: indexed by date, columns open/high/low/close (adjusted), raw_open, raw_close."""
    by_entry: dict[date, list[Order]] = {}
    nxt = {d: sessions[i + 1] for i, d in enumerate(sessions[:-1])}
    for o in sorted(orders, key=lambda o: (o.signal_date, o.ticker)):
        d = nxt.get(o.signal_date)
        if d is not None:
            by_entry.setdefault(d, []).append(o)
    cash = capital
    open_: dict[int, Trade] = {}
    done: list[Trade] = []
    equity: dict[date, float] = {}

    for d in sessions:
        for o in by_entry.get(d, []):
            t = Trade(o)
            bar = bars.get(o.instrument_id)
            row = bar.loc[d] if bar is not None and d in bar.index else None
            if o.instrument_id in open_:
                t.skipped = "already held"
            elif row is None or pd.isna(row["raw_open"]):
                t.skipped = "no trade on the entry day"
            elif row["raw_open"] > o.max_entry:
                t.skipped = f"open {row['raw_open']:.0f} above max entry {o.max_entry:.0f}"
            elif row["raw_open"] <= o.stop:
                t.skipped = f"open {row['raw_open']:.0f} at or below the stop {o.stop:.0f}"
            else:
                fill = float(row["raw_open"]) + _slip(float(row["raw_open"]), costs.ticks)
                per_lot = fill * costs.lot_size * (1 + costs.buy)
                lots = min(o.lots, int(cash // per_lot))
                if lots <= 0:
                    t.skipped = "not enough cash"
                else:
                    t.entry_date, t.shares, t.entry_fill = d, lots * costs.lot_size, fill
                    t.cost_in = t.shares * fill * (1 + costs.buy)
                    t._f = float(row["open"]) / float(row["raw_open"])
                    cash -= t.cost_in
                    open_[o.instrument_id] = t
            if t.skipped:
                done.append(t)

        for iid, t in list(open_.items()):
            bar = bars[iid]
            if d not in bar.index or pd.isna(bar.loc[d, "raw_close"]):
                continue
            row = bar.loc[d]
            f = t._f
            stop, target = t.order.stop * f, t.order.target * f
            first = d == t.entry_date
            px: float | None = None
            reason = ""
            if not first and row["open"] <= stop:
                px, reason = float(row["open"]), "stop (gap)"
            elif not first and row["open"] >= target:
                px, reason = float(row["open"]), "target (gap)"
            elif row["low"] <= stop:
                px, reason = stop, "stop"
            elif row["high"] >= target:
                px, reason = target, "target"
            elif d >= t.order.expiry:
                px, reason = float(row["close"]), "expiry"
            if px is not None:
                cash += _close(t, px, row, d, reason, costs)
                done.append(t)
                del open_[iid]
        equity[d] = cash + sum(
            t.shares * float(bars[i].loc[:d, "close"].iloc[-1]) / t._f for i, t in open_.items()
        )

    if sessions:
        last = sessions[-1]
        for iid, t in list(open_.items()):
            row = bars[iid].loc[:last].iloc[-1]
            cash += _close(t, float(row["close"]), row, last, "end of data", costs)
            done.append(t)
        equity[last] = cash
    return Result(done, pd.Series(equity, dtype=float), capital)


def _close(t: Trade, px_adj: float, row: pd.Series, d: date, reason: str, costs: Costs) -> float:
    """Exit at adjusted price `px_adj`; returns the cash received."""
    f_today = float(row["close"]) / float(row["raw_close"])  # adjusted / raw on the exit day
    raw_px = px_adj / f_today
    exit_adj = (raw_px - _slip(raw_px, costs.ticks)) * f_today
    value = t.shares * exit_adj / t._f
    t.exit_date, t.exit_reason = d, reason
    t.proceeds = value * (1 - costs.sell)
    return t.proceeds


def equal_weight_benchmark(bars: dict[int, pd.DataFrame], sessions: list[date], capital: float) -> pd.Series:
    """Buy every stock with a bar on the first session in equal value at its close, hold to
    the end (adjusted closes, forward-filled over no-trade days). No costs: a generous yardstick."""
    if not sessions:
        return pd.Series(dtype=float)
    start = sessions[0]
    cols = []
    for b in bars.values():
        if start in b.index and not pd.isna(b.loc[start, "close"]):
            s = b["close"].reindex(sessions).ffill()
            cols.append(s / s.iloc[0])
    if not cols:
        return pd.Series(capital, index=sessions, dtype=float)
    return pd.concat(cols, axis=1).mean(axis=1) * capital


def metrics(res: Result) -> dict[str, Any]:
    eq = res.equity
    taken = [t for t in res.trades if t.entry_date]
    rets = np.array([t.net_return for t in taken]) if taken else np.array([])
    dd = (eq / eq.cummax() - 1).min() if len(eq) else float("nan")
    out: dict[str, Any] = {
        "total_return": float(eq.iloc[-1] / res.capital - 1) if len(eq) else 0.0,
        "max_drawdown": float(dd),
        "trades": len(taken),
        "skipped": len(res.trades) - len(taken),
        "hit_rate": float((rets > 0).mean()) if len(rets) else float("nan"),
        "mean_trade_return": float(rets.mean()) if len(rets) else float("nan"),
        "exits": pd.Series([t.exit_reason for t in taken]).value_counts().to_dict() if taken else {},
        "skip_reasons": pd.Series([str(t.skipped).split(" ")[0] for t in res.trades if t.skipped])
        .value_counts()
        .to_dict(),
    }
    for name, b in res.benchmarks.items():
        out[f"benchmark_{name}"] = float(b.iloc[-1] / res.capital - 1) if len(b) else float("nan")
    return out


__all__ = ["Costs", "Order", "Result", "Trade", "equal_weight_benchmark", "metrics", "simulate"]
