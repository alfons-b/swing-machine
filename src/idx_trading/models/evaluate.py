"""Walk-forward comparison of candidate models (ADR 0015).

For every fold: fit each model on the purged training rows, predict the test block, keep the
out-of-sample predictions. Metrics are computed on the pooled out-of-sample predictions and
per fold. The report records the data fingerprint, fold boundaries, model parameters and
code version, so the same inputs reproduce the same numbers.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from idx_trading import __version__
from idx_trading.models.dataset import FEATURES, rank_transform
from idx_trading.models.estimators import make
from idx_trading.models.metrics import calibration_metrics, ranking_metrics
from idx_trading.models.walkforward import Fold, make_folds, split


@dataclass(frozen=True)
class EvalConfig:
    horizon: int
    models: tuple[str, ...]
    min_train: int = 60
    test_size: int = 20
    embargo: int = 2


def frame_fingerprint(df: pd.DataFrame) -> str:
    h = pd.util.hash_pandas_object(df, index=False).to_numpy()
    return hashlib.sha256(h.tobytes()).hexdigest()[:16]


def walk_forward(
    data: pd.DataFrame, cfg: EvalConfig, data_info: dict[str, Any] | None = None
) -> tuple[dict[str, Any], pd.DataFrame]:
    """`data` from `build_dataset`. Returns (report, out-of-sample predictions)."""
    x = rank_transform(data)
    folds = make_folds(
        data["trade_date"], min_train=cfg.min_train, test_size=cfg.test_size, embargo=cfg.embargo
    )
    if not folds:
        raise ValueError(
            f"not enough sessions for one fold (have {data['trade_date'].nunique()}, "
            f"need {cfg.min_train + cfg.embargo + cfg.test_size})"
        )
    preds: list[pd.DataFrame] = []
    fold_rows: list[dict[str, Any]] = []
    params: dict[str, Any] = {}
    for fold in folds:
        tr, te = split(data, fold)
        fold_rows.append(_fold_info(fold, tr, te))
        base = float((tr["net"] > 0).mean())
        for name in cfg.models:
            m = make(name).fit(x.loc[tr.index], tr["net"], tr["excess"])
            params[name] = m.params()
            score, prob = m.predict(x.loc[te.index])
            p = te[["instrument_id", "trade_date", "net", "excess"]].copy()
            p["model"], p["fold"], p["score"] = name, fold.index, score
            p["prob"] = prob if prob is not None else np.nan
            p["base_rate"] = base
            preds.append(p)
    oos = pd.concat(preds, ignore_index=True)
    report: dict[str, Any] = {
        "report": "walk_forward_v1",
        "code_version": __version__,
        "config": {
            "horizon": cfg.horizon,
            "models": list(cfg.models),
            "min_train": cfg.min_train,
            "test_size": cfg.test_size,
            "embargo": cfg.embargo,
            "features": list(FEATURES),
            "input_transform": "per-day percentile rank",
        },
        "data": {
            **(data_info or {}),
            "rows": len(data),
            "instruments": int(data["instrument_id"].nunique()),
            "first_date": str(data["trade_date"].min().date()),
            "last_date": str(data["trade_date"].max().date()),
            "fingerprint": frame_fingerprint(data[["instrument_id", "trade_date", *FEATURES, "net"]]),
        },
        "folds": fold_rows,
        "params": params,
        "results": {name: _summarise(oos[oos["model"] == name]) for name in cfg.models},
    }
    return report, oos


def _fold_info(fold: Fold, tr: pd.DataFrame, te: pd.DataFrame) -> dict[str, Any]:
    return {
        "fold": fold.index,
        "train_rows": len(tr),
        "train_last_label_end": str(tr["label_end"].max().date()) if len(tr) else None,
        "test_start": str(fold.test_start.date()),
        "test_end": str(fold.test_end.date()),
        "test_rows": len(te),
    }


def _summarise(p: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {"overall": ranking_metrics(p)}
    out["by_fold"] = [{"fold": int(k), **ranking_metrics(g)} for k, g in p.groupby("fold")]
    if p["prob"].notna().all() and len(p):
        y = (p["net"] > 0).to_numpy(dtype=float)
        out["calibration"] = calibration_metrics(p["prob"].to_numpy(), y, p["base_rate"].to_numpy())
    return out


def _f(v: Any, pct: bool = False) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "-"
    return f"{v:.2%}" if pct else f"{v:.3f}"


def render_markdown(report: dict[str, Any]) -> str:
    c, d = report["config"], report["data"]
    lines = [
        f"# Walk-forward comparison — H{c['horizon']}",
        "",
        f"Data: {d['rows']:,} rows, {d['instruments']} stocks, {d['first_date']} → {d['last_date']}"
        f" (fingerprint `{d['fingerprint']}`). Folds: {len(report['folds'])} x {c['test_size']}"
        f" sessions, min train {c['min_train']}, purge by label end + embargo {c['embargo']}.",
        "",
        "| Model | IC | IC t | IC>0 days | Top-20% net | Top hit | Spread | All net | Brier skill | ECE |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, r in report["results"].items():
        o, cal = r["overall"], r.get("calibration", {})
        lines.append(
            f"| {name} | {_f(o.get('ic'))} | {_f(o.get('ic_t'))} | {_f(o.get('ic_positive_days'), True)}"
            f" | {_f(o.get('top_net'), True)} | {_f(o.get('top_hit'), True)} | {_f(o.get('spread'), True)}"
            f" | {_f(o.get('all_net'), True)} | {_f(cal.get('brier_skill'))} | {_f(cal.get('ece'))} |"
        )
    lines += [
        "",
        "Per fold IC:",
        "",
        "| Model | " + " | ".join(f"F{f['fold']}" for f in report["folds"]) + " |",
    ]
    lines.append("|---|" + "---|" * len(report["folds"]))
    for name, r in report["results"].items():
        lines.append(f"| {name} | " + " | ".join(_f(f.get("ic")) for f in r["by_fold"]) + " |")
    return "\n".join(lines) + "\n"


def to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, default=str)


__all__ = ["EvalConfig", "frame_fingerprint", "render_markdown", "to_json", "walk_forward"]
