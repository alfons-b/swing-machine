"""M2.3: dataset, walk-forward purging, metrics, reproducibility, registry (ADR 0015)."""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.orm import Session, sessionmaker

from idx_trading.db.enums import ModelStatus
from idx_trading.features.labels import instrument_labels
from idx_trading.models.dataset import FEATURES, Floor, build_dataset, rank_transform
from idx_trading.models.evaluate import EvalConfig, render_markdown, to_json, walk_forward
from idx_trading.models.metrics import calibration_metrics, ranking_metrics, reliability
from idx_trading.models.registry import ModelRegistry, RegistryError
from idx_trading.models.walkforward import make_folds, split

H = 5


def synthetic(n_days: int = 140, n_stocks: int = 40, signal: float = 0.02, seed: int = 0) -> pd.DataFrame:
    """Rows like `build_dataset` output; net return depends on `atr_14_pct` (negatively)."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2026-01-05", periods=n_days + H)
    rows = []
    for i, d in enumerate(days[:n_days]):
        f = rng.normal(size=(n_stocks, len(FEATURES)))
        frame = pd.DataFrame(f, columns=list(FEATURES))
        frame["instrument_id"] = np.arange(n_stocks)
        frame["trade_date"] = d
        frame["label_end"] = days[i + H]
        frame["raw_close"] = 1000.0
        frame["adv_20_idr"] = 5e9
        frame["net"] = -signal * frame["atr_14_pct"] + rng.normal(scale=0.03, size=n_stocks) - 0.01
        frame["excess"] = frame["net"] - frame["net"].median()
        rows.append(frame)
    return pd.concat(rows, ignore_index=True)


# --- folds and purging ------------------------------------------------------------------------


def test_folds_expand_and_do_not_overlap():
    dates = pd.Series(pd.bdate_range("2026-01-01", periods=150))
    folds = make_folds(dates, min_train=60, test_size=20, embargo=2)
    assert len(folds) == 4  # (150 - 62) // 20
    for a, b in pairwise(folds):
        assert a.test_end < b.test_start
        assert a.train_end < b.train_end
    for f in folds:
        assert f.train_end < f.test_start
        gap = dates[(dates > f.train_end) & (dates < f.test_start)]
        assert len(gap) == 2  # the embargo


def test_split_purges_rows_whose_label_window_reaches_the_test_block():
    df = synthetic(100, 5)
    for fold in make_folds(df["trade_date"], min_train=40, test_size=20, embargo=0):
        tr, te = split(df, fold)
        assert (tr["label_end"] < fold.test_start).all()
        assert (tr["trade_date"] < fold.test_start).all()
        assert te["trade_date"].between(fold.test_start, fold.test_end).all()
        # with H=5 and no embargo, the last H sessions before the test block are purged
        before = df[(df["trade_date"] < fold.test_start)]["trade_date"].unique()
        assert len(before) - tr["trade_date"].nunique() == H


def test_no_fold_uses_the_future():
    """Scrambling every label after fold 0's test block leaves fold 0's predictions unchanged."""
    df = synthetic()
    cfg = EvalConfig(H, ("ridge", "lgbm_cls"), min_train=60, test_size=20)
    _, oos = walk_forward(df, cfg)
    fold0_end = make_folds(df["trade_date"], min_train=60, test_size=20)[0].test_end
    later = df["trade_date"] > fold0_end
    df2 = df.copy()
    rng = np.random.default_rng(9)
    df2.loc[later, "net"] = rng.normal(size=int(later.sum()))
    df2.loc[later, "excess"] = rng.normal(size=int(later.sum()))
    _, oos2 = walk_forward(df2, cfg)
    a = oos[oos["fold"] == 0].reset_index(drop=True)
    b = oos2[oos2["fold"] == 0].reset_index(drop=True)
    pd.testing.assert_series_equal(a["score"], b["score"])


def test_report_is_reproducible_and_recovers_a_planted_signal():
    df = synthetic()
    cfg = EvalConfig(H, ("momentum", "ridge", "logistic", "lgbm_cls", "lgbm_reg"))
    r1, _ = walk_forward(df, cfg, {"note": "x"})
    r2, _ = walk_forward(df, cfg, {"note": "x"})
    assert to_json(r1) == to_json(r2)
    res = r1["results"]
    assert res["ridge"]["overall"]["ic"] > 0.3
    assert res["lgbm_reg"]["overall"]["ic"] > 0.2
    assert abs(res["momentum"]["overall"]["ic"]) < 0.1  # momentum feature is pure noise here
    assert res["logistic"]["calibration"]["brier_skill"] > 0
    cal = res["ridge"]["excess_calibration"]
    assert cal["slope"] > 0 and cal["n"] > 100  # realised excess rises with the score
    assert r1["data"]["fingerprint"] and len(r1["folds"]) == 3
    assert "| ridge |" in render_markdown(r1)


def test_walk_forward_needs_enough_sessions():
    with pytest.raises(ValueError, match="not enough sessions"):
        walk_forward(synthetic(50, 5), EvalConfig(H, ("ridge",)))


# --- dataset ----------------------------------------------------------------------------------


def test_dataset_applies_floor_per_day_and_keeps_labels_out_of_features():
    days = pd.bdate_range("2026-03-02", periods=3).date
    feats = pd.DataFrame(
        {
            "instrument_id": [1, 1, 1, 2, 2, 2],
            "ticker": ["AAAA"] * 3 + ["BBBB"] * 3,
            "trade_date": list(days) * 2,
            "raw_close": [150, 250, 250, 1000, 1000, 1000],
            **{f: 0.1 for f in FEATURES},
        }
    )
    feats.loc[feats["instrument_id"] == 2, "adv_20_idr"] = [5e9, 5e8, 5e9]
    feats.loc[feats["instrument_id"] == 1, "adv_20_idr"] = 5e9
    labels = feats[["instrument_id", "trade_date"]].copy()
    labels["net_5"], labels["excess_5"], labels["end_5"] = 0.01, 0.0, days[-1]
    labels.loc[5, "net_5"] = np.nan  # incomplete window
    out = build_dataset(feats, labels, 5, Floor(200, 1e9))
    got = sorted(zip(out["instrument_id"], out["trade_date"].dt.date, strict=True))
    assert got == [(1, days[1]), (1, days[2]), (2, days[0])]
    assert not {"net", "excess", "label_end"} & set(FEATURES)


def test_rank_transform_is_per_day():
    df = pd.DataFrame(
        {"trade_date": ["d1", "d1", "d2", "d2"], **{f: [1.0, 2.0, 100.0, 50.0] for f in FEATURES}}
    )
    r = rank_transform(df)
    assert r["ret_1"].tolist() == [0.5, 1.0, 1.0, 0.5]


def test_labels_carry_exit_date():
    n = 30
    df = pd.DataFrame(
        {
            "trade_date": pd.bdate_range("2026-01-01", periods=n).date,
            "open": 1000.0,
            "high": 1010.0,
            "low": 990.0,
            "close": 1000.0,
        }
    )
    df["raw_open"], df["raw_close"] = df["open"], df["close"]
    lab = instrument_labels(df, 0.0, 0.0, 0)
    assert lab.loc[0, "end_5"] == df.loc[5, "trade_date"]
    assert pd.isna(lab.loc[n - 5, "end_5"])


# --- metrics ----------------------------------------------------------------------------------


def test_ranking_metrics_by_hand():
    day = pd.DataFrame({"trade_date": "d", "score": np.arange(10.0), "net": np.arange(10.0) / 100 - 0.03})
    m = ranking_metrics(day)
    assert m["ic"] == pytest.approx(1.0)
    assert m["top_net"] == pytest.approx((0.08 + 0.09 - 0.06) / 2)  # top 2 of 10
    assert m["spread"] == pytest.approx((0.08 + 0.09 - 0.06) / 2 - (0.00 + 0.01 - 0.06) / 2)
    assert m["all_net"] == pytest.approx(0.015)
    assert m["top_hit"] == 1.0 and m["days"] == 1
    small = day.head(5)
    assert ranking_metrics(small) == {"days": 0}


def test_calibration_metrics():
    y = np.array([0, 0, 1, 1], dtype=float)
    perfect = calibration_metrics(y.copy(), y, np.full(4, 0.5))
    assert perfect["brier"] == 0 and perfect["brier_ref"] == 0.25 and perfect["brier_skill"] == 1
    flat = calibration_metrics(np.full(4, 0.5), y, np.full(4, 0.5))
    assert flat["brier_skill"] == pytest.approx(0) and flat["ece"] == pytest.approx(0)
    table = reliability(np.array([0.05, 0.15, 0.95, 1.0]), np.array([0, 0, 1, 1.0]))
    assert [r["n"] for r in table] == [1, 1, 2]
    assert table[-1]["bin_high"] == 1.0


# --- registry ---------------------------------------------------------------------------------


@pytest.fixture
def registry(factory: sessionmaker[Session], tmp_path):
    with factory() as s:
        yield ModelRegistry(s, tmp_path / "models"), s


def test_register_is_candidate_and_idempotent(registry):
    reg, _ = registry
    df = synthetic(80, 10)
    mv = reg.train_and_register("ridge", H, df, {"overall": {"ic": 0.3}})
    assert mv.status == ModelStatus.CANDIDATE.value
    assert mv.train_rows == len(df) and mv.target == "excess"
    assert reg.train_and_register("ridge", H, df, None).model_id == mv.model_id
    model = reg.load(mv.model_id)
    score, prob = model.predict(rank_transform(df))
    assert len(score) == len(df) and prob is None


def test_approval_is_manual_and_single_per_horizon(registry):
    reg, _ = registry
    df = synthetic(80, 10)
    no_eval = reg.train_and_register("logistic", H, df, None)
    with pytest.raises(RegistryError, match="no walk-forward evaluation"):
        reg.approve(no_eval.model_id, "owner", "looks fine")
    a = reg.train_and_register("ridge", H, df, {"overall": {"ic": 0.3}})
    with pytest.raises(RegistryError, match="approver"):
        reg.approve(a.model_id, " ", "note")
    reg.approve(a.model_id, "owner", "best OOS IC")
    assert reg.production(H).model_id == a.model_id
    b = reg.train_and_register("lgbm_reg", H, df, {"overall": {"ic": 0.2}})
    reg.approve(b.model_id, "owner", "switch")
    assert reg.production(H).model_id == b.model_id
    assert a.status == ModelStatus.RETIRED.value and a.decision_note == f"replaced by {b.model_id}"
    with pytest.raises(RegistryError, match="only candidates"):
        reg.approve(a.model_id, "owner", "again")
    reg.reject(no_eval.model_id, "owner", "no evaluation")
    assert no_eval.status == ModelStatus.REJECTED.value
    assert reg.production(H + 1) is None


def test_tampered_artifact_is_refused(registry):
    reg, _ = registry
    mv = reg.train_and_register("momentum", H, synthetic(80, 10), None)
    path = reg.root / mv.artifact_path
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(RegistryError, match="hash mismatch"):
        reg.load(mv.model_id)
