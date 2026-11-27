"""Historical backtest of the recommendation rules on walk-forward signals (ADR 0017).

Signals for day t come only from information available at t:
- scores: the walk-forward model of the fold containing t (trained on labels closed before
  the fold, ADR 0015);
- the excess calibration line: fitted on the out-of-sample predictions of *earlier* folds
  only (fold 0 has none, so trading starts with fold 1);
- the market drift: labels closed by t (ADR 0016).
Then `signals.engine.decide` makes BUY/WATCH/AVOID with the configured rules, and BUYs become
orders for the next open, simulated by `backtest.engine.simulate`.

Known simplifications (stated in the report): every stock above the floor counts as eligible
(the listing history has no delisting dates yet, OI-37), and candidates are the rows that
have labels (a stock suspended right after t drops out; rare in the sample).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from idx_trading.backtest.engine import Costs, Order, Result, equal_weight_benchmark, metrics, simulate
from idx_trading.features.store import bars_frame
from idx_trading.models.dataset import Floor, load_dataset
from idx_trading.models.evaluate import EvalConfig, walk_forward
from idx_trading.signals.engine import Action, DayContext, decide, market_drift
from idx_trading.trading_config import TradingConfig


def _calibration(prior: pd.DataFrame) -> tuple[float, float] | None:
    q = prior[["score", "excess"]].dropna()
    if len(q) < 100 or q["score"].std() == 0:
        return None
    slope, intercept = np.polyfit(q["score"].to_numpy(), q["excess"].to_numpy(), 1)
    return float(intercept), float(slope)


def build_orders(
    data: pd.DataFrame,
    oos: pd.DataFrame,
    labels: pd.DataFrame,
    cfg: TradingConfig,
    horizon: int,
    sessions: list[date],
    *,
    ignore_hurdle: bool = False,
    overrides: dict[str, float] | None = None,
) -> tuple[list[Order], dict[str, Any]]:
    sig_cfg = cfg.signals.model_copy(update=overrides or {})
    if ignore_hurdle:
        sig_cfg = sig_cfg.model_copy(update={"buy_min_expected_net": -1.0})
    pos = {d: i for i, d in enumerate(sessions)}
    feats = data.set_index(["instrument_id", "trade_date"])
    orders: list[Order] = []
    days_traded, days_blocked = 0, 0
    for k in sorted(oos["fold"].unique()):
        cal = _calibration(oos[oos["fold"] < k])
        if cal is None:
            continue
        fold = oos[oos["fold"] == k]
        for t, day in fold.groupby("trade_date"):
            t_date = pd.Timestamp(t).date()
            i = pos.get(t_date)
            if i is None or i + horizon >= len(sessions):
                continue
            f = feats.loc[[(iid, t) for iid in day["instrument_id"]]]
            cands = pd.DataFrame(
                {
                    "instrument_id": day["instrument_id"].to_numpy(),
                    "ticker": f["ticker"].to_numpy(),
                    "eligible": True,
                    "eligibility_reason": None,
                    "last_date": pd.Timestamp(t),
                    "raw_close": f["raw_close"].to_numpy(),
                    "adv_20_idr": f["adv_20_idr"].to_numpy(),
                    "atr_14_pct": f["atr_14_pct"].to_numpy(),
                    "open_errors": 0,
                    "score": cal[0] + cal[1] * day["score"].to_numpy(),
                }
            )
            ctx = DayContext(
                t_date,
                sessions[i + horizon],
                market_drift(labels, horizon, t_date, sig_cfg.drift_sessions),
                float(cfg.capital_idr),
                cfg.lot_size,
                float(cfg.universe.min_price_idr),
                float(cfg.universe.min_adv_idr),
            )
            buys = [d for d in decide(cands, ctx, sig_cfg) if d.action == Action.BUY]
            days_traded += 1 if buys else 0
            days_blocked += 0 if buys else 1
            for d in buys:
                assert d.max_entry is not None and d.stop is not None and d.target is not None
                orders.append(
                    Order(
                        t_date,
                        d.instrument_id,
                        d.ticker,
                        d.max_entry,
                        d.stop,
                        d.target,
                        ctx.expiry_date,
                        d.lots,
                    )
                )
    return orders, {"days_with_buys": days_traded, "days_without_buys": days_blocked}


def run_backtest(
    s: Session,
    features_root: Path,
    cfg: TradingConfig,
    horizon: int,
    model: str,
    *,
    ignore_hurdle: bool = False,
) -> tuple[Result, dict[str, Any]]:
    u = cfg.universe
    data, info = load_dataset(features_root, horizon, Floor(float(u.min_price_idr), float(u.min_adv_idr)))
    _, oos = walk_forward(data, EvalConfig(horizon, (model,)), info)
    labels = pd.read_parquet(features_root / "labels_v1.parquet")
    feats_all = pd.read_parquet(features_root / "tech_v1.parquet", columns=["trade_date"])
    sessions = sorted(pd.to_datetime(feats_all["trade_date"]).dt.date.unique())
    orders, info_orders = build_orders(data, oos, labels, cfg, horizon, sessions, ignore_hurdle=ignore_hurdle)

    first_fold = sorted(oos["fold"].unique())[1] if oos["fold"].nunique() > 1 else 0
    start = pd.Timestamp(oos[oos["fold"] == first_fold]["trade_date"].min()).date()
    sim_sessions = [d for d in sessions if d >= start]
    universe = sorted(data[data["trade_date"] == pd.Timestamp(start)]["instrument_id"].unique())
    needed = sorted({o.instrument_id for o in orders} | set(universe))
    bars: dict[int, pd.DataFrame] = {}
    for iid in needed:
        df = bars_frame(s, int(iid), None)
        if len(df):
            bars[int(iid)] = df.set_index("trade_date")
    costs = Costs(float(cfg.costs.buy), float(cfg.costs.sell), cfg.slippage.ticks_per_side, cfg.lot_size)
    res = simulate(orders, bars, sim_sessions, float(cfg.capital_idr), costs)
    res.benchmarks["cash"] = pd.Series(float(cfg.capital_idr), index=sim_sessions, dtype=float)
    res.benchmarks["equal_weight_floor_universe"] = equal_weight_benchmark(
        {i: bars[i] for i in universe if i in bars}, sim_sessions, float(cfg.capital_idr)
    )
    report = {
        "horizon": horizon,
        "model": model,
        "rules": "configured" if not ignore_hurdle else "hurdle ignored (top fraction always bought)",
        "period": f"{sim_sessions[0]} .. {sim_sessions[-1]}" if sim_sessions else None,
        "orders": len(orders),
        **info_orders,
        **metrics(res),
        "costs": {"buy": costs.buy, "sell": costs.sell, "ticks_per_side": costs.ticks},
        "simplifications": [
            "all stocks above the floor treated as eligible (no delisting dates yet)",
            "candidates = rows with complete labels",
            "equal-weight benchmark pays no costs",
        ],
    }
    return res, report


def trades_frame(res: Result) -> pd.DataFrame:
    rows = []
    for t in res.trades:
        rows.append(
            {
                "signal_date": t.order.signal_date,
                "ticker": t.order.ticker,
                "entry_date": t.entry_date,
                "shares": t.shares,
                "entry_fill": t.entry_fill,
                "stop": t.order.stop,
                "target": t.order.target,
                "exit_date": t.exit_date,
                "exit_reason": t.exit_reason,
                "net_return": t.net_return if t.entry_date else None,
                "pnl": t.pnl if t.entry_date else None,
                "skipped": t.skipped,
            }
        )
    return pd.DataFrame(rows)


__all__ = ["build_orders", "run_backtest", "trades_frame"]
