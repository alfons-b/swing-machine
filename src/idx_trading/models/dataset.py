"""Modelling dataset: features of day t joined with labels of day t (ADR 0015).

The join is on (instrument_id, trade_date) only. Labels are *targets*: they describe
t+1 .. t+H and must never become columns of the feature matrix (`FEATURES` is the
closed list of model inputs, so a stray label column cannot leak in by accident).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from idx_trading.features.technical import NAMES

FEATURES: tuple[str, ...] = NAMES
KEYS = ("instrument_id", "trade_date")


@dataclass(frozen=True)
class Floor:
    min_price_idr: float = 200.0
    min_adv_idr: float = 1e9


def build_dataset(features: pd.DataFrame, labels: pd.DataFrame, horizon: int, floor: Floor) -> pd.DataFrame:
    """One row per (stock, day) passing the price/liquidity floor *on that day*, with
    `net`/`excess` targets for `horizon`. Rows whose label window is incomplete are dropped."""
    lab = labels[[*KEYS, f"net_{horizon}", f"excess_{horizon}", f"end_{horizon}"]].rename(
        columns={f"net_{horizon}": "net", f"excess_{horizon}": "excess", f"end_{horizon}": "label_end"}
    )
    df = features.merge(lab, on=list(KEYS), how="inner", validate="one_to_one")
    ok = (
        (df["raw_close"] >= floor.min_price_idr)
        & (df["adv_20_idr"] >= floor.min_adv_idr)
        & df["net"].notna()
        & df["ret_20"].notna()  # at least 20 sessions of history behind every row
    )
    df = df.loc[ok].copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["label_end"] = pd.to_datetime(df["label_end"])
    return df.sort_values(["trade_date", "instrument_id"]).reset_index(drop=True)


def rank_transform(df: pd.DataFrame, cols: tuple[str, ...] = FEATURES) -> pd.DataFrame:
    """Per-day percentile ranks in (0, 1]; NaN stays NaN. Uses only same-day values, so it
    adds no look-ahead and removes the market-wide level drift between train and test."""
    out = df.groupby("trade_date")[list(cols)].rank(pct=True)
    return out.astype(np.float64)


def load_dataset(features_root: Path, horizon: int, floor: Floor) -> tuple[pd.DataFrame, dict[str, object]]:
    """Read the feature store (definition hash checked) and labels, then `build_dataset`."""
    from idx_trading.features.store import read_features

    feats = read_features(features_root)
    labels = pd.read_parquet(features_root / "labels_v1.parquet")
    if f"end_{horizon}" not in labels.columns:
        raise RuntimeError("labels_v1 lacks exit dates (built before ADR 0015): run `idx features labels`")
    fman = json.loads((features_root / "tech_v1.manifest.json").read_text())
    lman = json.loads((features_root / "labels_v1.manifest.json").read_text())
    info: dict[str, object] = {
        "feature_set": fman["feature_set"],
        "feature_hash": fman["definition_hash"],
        "features_built_at": fman["built_at"],
        "label_set": lman["label_set"],
        "labels_built_at": lman["built_at"],
        "costs": {
            "buy": lman["buy_cost"],
            "sell": lman["sell_cost"],
            "ticks_per_side": lman["ticks_per_side"],
        },
        "floor": {"min_price_idr": floor.min_price_idr, "min_adv_idr": floor.min_adv_idr},
    }
    return build_dataset(feats, labels, horizon, floor), info


__all__ = ["FEATURES", "Floor", "build_dataset", "load_dataset", "rank_transform"]
