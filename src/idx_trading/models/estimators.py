"""Candidate models (ADR 0015). Every model sees the same inputs: per-day percentile ranks
of the `tech_v1` features. Each returns a ranking `score` (higher = better expected
excess return); probabilistic models also return `prob` = P(net return after costs > 0).

All randomness is seeded and LightGBM runs single-threaded and deterministic, so the same
data gives byte-identical predictions (the comparison report is reproducible).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
import pandas as pd

from idx_trading.models.dataset import FEATURES

SEED = 20261124


class Model(Protocol):
    name: str
    probabilistic: bool

    def fit(self, x: pd.DataFrame, net: pd.Series, excess: pd.Series) -> Model: ...
    def predict(self, x: pd.DataFrame) -> tuple[np.ndarray, np.ndarray | None]: ...
    def params(self) -> dict[str, Any]: ...


@dataclass
class Momentum:
    """No fitting: rank by 20-session return relative to the market (rs_market_20)."""

    name: str = "momentum"
    probabilistic: bool = False
    feature: str = "rs_market_20"

    def fit(self, x: pd.DataFrame, net: pd.Series, excess: pd.Series) -> Momentum:
        return self

    def predict(self, x: pd.DataFrame) -> tuple[np.ndarray, np.ndarray | None]:
        return x[self.feature].fillna(0.5).to_numpy(), None

    def params(self) -> dict[str, Any]:
        return {"feature": self.feature}


def _linear_pipeline(est: Any) -> Any:
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import make_pipeline

    # Inputs are ranks in (0, 1]: impute the neutral 0.5, no scaling needed.
    return make_pipeline(SimpleImputer(strategy="constant", fill_value=0.5, keep_empty_features=True), est)


@dataclass
class RidgeExcess:
    name: str = "ridge"
    probabilistic: bool = False
    alpha: float = 10.0
    _m: Any = None

    def fit(self, x: pd.DataFrame, net: pd.Series, excess: pd.Series) -> RidgeExcess:
        from sklearn.linear_model import Ridge

        self._m = _linear_pipeline(Ridge(alpha=self.alpha)).fit(x[list(FEATURES)], excess)
        return self

    def predict(self, x: pd.DataFrame) -> tuple[np.ndarray, np.ndarray | None]:
        return np.asarray(self._m.predict(x[list(FEATURES)])), None

    def params(self) -> dict[str, Any]:
        return {"alpha": self.alpha, "target": "excess"}


@dataclass
class LogisticNet:
    name: str = "logistic"
    probabilistic: bool = True
    c: float = 0.1
    _m: Any = None

    def fit(self, x: pd.DataFrame, net: pd.Series, excess: pd.Series) -> LogisticNet:
        from sklearn.linear_model import LogisticRegression

        est = LogisticRegression(C=self.c, max_iter=1000)
        self._m = _linear_pipeline(est).fit(x[list(FEATURES)], (net > 0).astype(int))
        return self

    def predict(self, x: pd.DataFrame) -> tuple[np.ndarray, np.ndarray | None]:
        p = np.asarray(self._m.predict_proba(x[list(FEATURES)])[:, 1])
        return p, p

    def params(self) -> dict[str, Any]:
        return {"C": self.c, "target": "net>0"}


_LGBM_BASE: dict[str, Any] = {
    "n_estimators": 300,
    "learning_rate": 0.03,
    "num_leaves": 15,
    "min_child_samples": 200,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "random_state": SEED,
    "n_jobs": 1,
    "deterministic": True,
    "force_row_wise": True,
    "verbose": -1,
}


@dataclass
class LgbmClassifier:
    name: str = "lgbm_cls"
    probabilistic: bool = True
    overrides: dict[str, Any] = field(default_factory=dict)
    _m: Any = None

    def fit(self, x: pd.DataFrame, net: pd.Series, excess: pd.Series) -> LgbmClassifier:
        from lightgbm import LGBMClassifier

        self._m = LGBMClassifier(**self.params()).fit(x[list(FEATURES)], (net > 0).astype(int))
        return self

    def predict(self, x: pd.DataFrame) -> tuple[np.ndarray, np.ndarray | None]:
        p = np.asarray(self._m.predict_proba(x[list(FEATURES)])[:, 1])
        return p, p

    def params(self) -> dict[str, Any]:
        return {**_LGBM_BASE, **self.overrides}


@dataclass
class LgbmRegressor:
    name: str = "lgbm_reg"
    probabilistic: bool = False
    overrides: dict[str, Any] = field(default_factory=dict)
    _m: Any = None

    def fit(self, x: pd.DataFrame, net: pd.Series, excess: pd.Series) -> LgbmRegressor:
        from lightgbm import LGBMRegressor

        # Huber-like robustness: clip the target at its 1st/99th percentile of the train set.
        lo, hi = np.nanpercentile(excess, [1, 99])
        self._m = LGBMRegressor(**self.params()).fit(x[list(FEATURES)], excess.clip(lo, hi))
        return self

    def predict(self, x: pd.DataFrame) -> tuple[np.ndarray, np.ndarray | None]:
        return np.asarray(self._m.predict(x[list(FEATURES)])), None

    def params(self) -> dict[str, Any]:
        return {**_LGBM_BASE, **self.overrides}


REGISTRY: dict[str, type] = {
    "momentum": Momentum,
    "ridge": RidgeExcess,
    "logistic": LogisticNet,
    "lgbm_cls": LgbmClassifier,
    "lgbm_reg": LgbmRegressor,
}


def make(name: str) -> Model:
    try:
        model: Model = REGISTRY[name]()
    except KeyError:
        raise ValueError(f"unknown model {name!r}; known: {', '.join(REGISTRY)}") from None
    return model


__all__ = ["REGISTRY", "SEED", "Model", "make"]
