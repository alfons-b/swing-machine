"""Out-of-sample metrics (ADR 0015).

Ranking (all models), computed per day and then averaged over days so that busy days do
not dominate:
  ic            Spearman correlation of score vs net return across stocks that day
  top_net       mean net return of the top quintile by score (what a BUY list would earn)
  top_hit       share of the top quintile with net return > 0
  spread        top-quintile minus bottom-quintile mean net return
  all_net       mean net return of every eligible stock (the "buy everything" baseline)
Calibration (probabilistic models only), over all out-of-sample rows:
  brier, brier_ref (always predicting the training base rate), brier_skill = 1 - brier/brier_ref,
  log_loss, ece (expected calibration error, 10 equal-width bins), reliability table.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

QUANTILE = 0.2
MIN_NAMES = 10  # days with fewer eligible stocks are not ranked


def _daily(day: pd.DataFrame) -> dict[str, float] | None:
    if len(day) < MIN_NAMES or day["score"].nunique() < 2:
        return None
    k = max(1, round(len(day) * QUANTILE))
    ordered = day.sort_values("score", ascending=False, kind="mergesort")
    top, bottom = ordered.head(k), ordered.tail(k)
    ic = day["score"].rank().corr(day["net"].rank())
    return {
        "ic": float(ic),
        "top_net": float(top["net"].mean()),
        "top_hit": float((top["net"] > 0).mean()),
        "spread": float(top["net"].mean() - bottom["net"].mean()),
        "all_net": float(day["net"].mean()),
    }


def ranking_metrics(pred: pd.DataFrame) -> dict[str, float]:
    """`pred` columns: trade_date, score, net."""
    rows = [r for _, d in pred.groupby("trade_date") if (r := _daily(d)) is not None]
    if not rows:
        return {"days": 0}
    t = pd.DataFrame(rows)
    ic_sd = float(t["ic"].std(ddof=1)) if len(t) > 1 else float("nan")
    out = {k: float(t[k].mean()) for k in t.columns}
    out["days"] = len(t)
    out["ic_t"] = out["ic"] / ic_sd * np.sqrt(len(t)) if ic_sd and ic_sd > 0 else float("nan")
    out["ic_positive_days"] = float((t["ic"] > 0).mean())
    return out


def reliability(prob: np.ndarray, y: np.ndarray, bins: int = 10) -> list[dict[str, float]]:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(prob, edges[1:-1]), 0, bins - 1)
    out = []
    for b in range(bins):
        m = idx == b
        if m.any():
            out.append(
                {
                    "bin_low": float(edges[b]),
                    "bin_high": float(edges[b + 1]),
                    "n": int(m.sum()),
                    "mean_pred": float(prob[m].mean()),
                    "observed": float(y[m].mean()),
                }
            )
    return out


def calibration_metrics(prob: np.ndarray, y: np.ndarray, base_rate: np.ndarray) -> dict[str, Any]:
    """`base_rate`: per-row training base rate of its fold (the no-skill forecast)."""
    eps = 1e-12
    p = np.clip(prob, eps, 1 - eps)
    brier = float(np.mean((prob - y) ** 2))
    brier_ref = float(np.mean((base_rate - y) ** 2))
    table = reliability(prob, y)
    n = len(y)
    ece = sum(r["n"] / n * abs(r["mean_pred"] - r["observed"]) for r in table) if n else float("nan")
    return {
        "brier": brier,
        "brier_ref": brier_ref,
        "brier_skill": 1 - brier / brier_ref if brier_ref > 0 else float("nan"),
        "log_loss": float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))),
        "ece": float(ece),
        "mean_pred": float(prob.mean()),
        "observed_rate": float(y.mean()),
        "reliability": table,
    }


__all__ = ["calibration_metrics", "ranking_metrics", "reliability"]
