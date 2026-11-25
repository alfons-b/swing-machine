"""Recommendation engine: BUY / WATCH / AVOID with entry, stop, target, expiry, size (ADR 0016).

`decide` is a pure function over one day's candidate table, so every rule is unit-tested
without a database. Rules, in order (the first failing gate decides, and is recorded):

AVOID gates (data and eligibility)
  - not eligible (listing status, board, instrument type)   -> reason from the instrument master
  - no bar on the signal date (stale data)                  -> "no price for <date>"
  - open data-quality errors in the lookback window         -> "open data-quality errors"
  - below the price/liquidity floor                         -> "below floor"
  - no ATR (too little history)                             -> "no ATR"
WATCH gates (no BUY, but worth following)
  - cross-section too small to rank                         -> "cross-section N < min"
  - not in the top fraction by score                        -> AVOID instead ("rank")
  - expected net return below the hurdle                    -> WATCH
  - position rounds to zero lots                            -> WATCH
BUY: everything passed.

Expected net return = predicted excess return (model) + market drift, where the drift is the
median net return of stocks over the last `drift_sessions` days whose labels had closed by
the signal date. In a falling market the drift is negative, so "best of a bad lot" stays WATCH.

Levels use the raw close of the signal date (orders are placed in raw prices):
  max_entry = close + max_entry_atr x ATR   (rounded down to the tick)
  stop      = close - stop_atr x ATR        (rounded down to the tick)
  target    = close + reward_risk x (close - stop)  (rounded up to the tick)
  expiry    = the H-th session after the signal date (exit at that close at the latest)
Size: the smallest of risk/(close - stop), max_position x capital / close and
max_adv_share x ADV / close shares, in whole lots.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from idx_trading.trading_config import Signals


class Action:
    BUY = "BUY"
    WATCH = "WATCH"
    AVOID = "AVOID"


def tick(price: float) -> int:
    return 1 if price < 200 else 2 if price < 500 else 5 if price < 2000 else 10 if price < 5000 else 25


def round_down(price: float) -> float:
    t = tick(price)
    return math.floor(price / t) * t


def round_up(price: float) -> float:
    t = tick(price)
    return math.ceil(price / t) * t


@dataclass(frozen=True)
class DayContext:
    signal_date: date
    expiry_date: date
    drift: float  # market drift estimate (median net return), may be negative
    capital: float
    lot_size: int
    min_price: float
    min_adv: float


@dataclass
class Decision:
    instrument_id: int
    ticker: str
    action: str
    reasons: list[str] = field(default_factory=list)
    rank: int | None = None
    score: float | None = None
    expected_net: float | None = None
    entry_ref: float | None = None
    max_entry: float | None = None
    stop: float | None = None
    target: float | None = None
    lots: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


CANDIDATE_COLUMNS = (
    "instrument_id",
    "ticker",
    "eligible",
    "eligibility_reason",
    "last_date",
    "raw_close",
    "adv_20_idr",
    "atr_14_pct",
    "open_errors",
    "score",  # predicted excess return; NaN when the model could not score the stock
)


def _f(x: Any) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def decide(cands: pd.DataFrame, ctx: DayContext, cfg: Signals) -> list[Decision]:
    """One decision per row of `cands` (columns `CANDIDATE_COLUMNS`)."""
    out: list[Decision] = []
    ranked: list[Decision] = []
    for r in cands.itertuples(index=False):
        d = Decision(int(r.instrument_id), str(r.ticker), Action.AVOID)
        close, adv, atr = _f(r.raw_close), _f(r.adv_20_idr), _f(r.atr_14_pct)
        if not bool(r.eligible):
            d.reasons.append(f"not eligible: {r.eligibility_reason}")
        elif (
            r.last_date is None or pd.isna(r.last_date) or pd.Timestamp(r.last_date).date() != ctx.signal_date
        ):
            d.reasons.append(f"no price for {ctx.signal_date} (latest {r.last_date})")
        elif int(r.open_errors) > 0:
            d.reasons.append(f"{int(r.open_errors)} open data-quality error(s)")
        elif close is None or adv is None or close < ctx.min_price or adv < ctx.min_adv:
            d.reasons.append(f"below floor (price {close}, ADV {adv})")
        elif atr is None or atr <= 0:
            d.reasons.append("no ATR (history too short)")
        elif _f(r.score) is None:
            d.reasons.append("model could not score")
        else:
            d.score = float(r.score)
            d.expected_net = d.score + ctx.drift
            _levels(d, close, atr, adv, ctx, cfg)
            ranked.append(d)
        out.append(d)

    n = len(ranked)
    ranked.sort(key=lambda x: (-(x.score or 0.0), x.ticker))
    top_k = max(1, int(np.floor(n * cfg.top_fraction))) if n else 0
    for i, d in enumerate(ranked, start=1):
        d.rank = i
        if i > top_k:
            d.action = Action.AVOID
            d.reasons.append(f"rank {i}/{n} outside the top {cfg.top_fraction:.0%}")
            continue
        d.action = Action.WATCH
        if n < cfg.min_cross_section:
            d.reasons.append(f"cross-section {n} < {cfg.min_cross_section}: ranks not reliable")
        elif d.expected_net is None or not math.isfinite(d.expected_net):
            d.reasons.append("no market-drift estimate: expected return unknown")
        elif d.expected_net < cfg.buy_min_expected_net:
            d.reasons.append(f"expected net {d.expected_net:+.2%} < hurdle {cfg.buy_min_expected_net:+.2%}")
        elif d.lots == 0:
            d.reasons.append("position rounds to zero lots")
        else:
            d.action = Action.BUY
    return out


def _levels(d: Decision, close: float, atr_pct: float, adv: float, ctx: DayContext, cfg: Signals) -> None:
    atr = atr_pct * close
    d.entry_ref = close
    d.max_entry = round_down(close + cfg.max_entry_atr * atr)
    stop = round_down(close - cfg.stop_atr * atr)
    if stop >= close:  # ATR smaller than one tick: one tick below
        stop = close - tick(close)
    d.stop = max(stop, 1.0)
    d.target = round_up(close + cfg.reward_risk * (close - d.stop))
    risk_per_share = close - d.stop
    shares = min(
        ctx.capital * cfg.risk_per_trade / risk_per_share,
        ctx.capital * cfg.max_position / close,
        adv * cfg.max_adv_share / close,
    )
    d.lots = int(shares // ctx.lot_size)


def market_drift(labels: pd.DataFrame, horizon: int, signal_date: date, sessions: int) -> float:
    """Median net return over the last `sessions` trade dates whose H-day labels had closed
    by `signal_date` (only information known on that date)."""
    lab = labels[["trade_date", f"net_{horizon}", f"end_{horizon}"]].dropna()
    known = lab[pd.to_datetime(lab[f"end_{horizon}"]) <= pd.Timestamp(signal_date)]
    if known.empty:
        return float("nan")
    days = sorted(pd.to_datetime(known["trade_date"]).unique())[-sessions:]
    sub = known[pd.to_datetime(known["trade_date"]).isin(days)]
    return float(sub[f"net_{horizon}"].median())


__all__ = ["CANDIDATE_COLUMNS", "Action", "DayContext", "Decision", "decide", "market_drift"]
