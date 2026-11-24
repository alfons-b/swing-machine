"""Model registry: train-and-register, manual approval, verified loading (ADR 0015).

Lifecycle: a trained model is registered as `candidate`. Only an explicit owner decision
(`approve`, with name and note) makes it `approved`; approving retires the previously
approved model for the same horizon, so at most one production model exists per horizon.
Nothing is deleted. The pickled estimator is written once, its SHA-256 is stored, and
`load` refuses a file whose hash differs (pickles are only ever read from our own store).
"""

from __future__ import annotations

import hashlib
import pickle
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from idx_trading import __version__
from idx_trading.db.enums import ModelStatus
from idx_trading.db.models import ModelVersion
from idx_trading.features.labels import LABEL_SET
from idx_trading.features.technical import FEATURE_SET, definition_hash
from idx_trading.models.dataset import FEATURES, rank_transform
from idx_trading.models.estimators import Model, make
from idx_trading.models.evaluate import frame_fingerprint


class RegistryError(RuntimeError):
    pass


def _target(m: Model) -> str:
    return "net>0" if m.probabilistic else "excess"


class ModelRegistry:
    def __init__(self, session: Session, root: Path) -> None:
        self.s = session
        self.root = root

    def train_and_register(
        self, name: str, horizon: int, data: pd.DataFrame, evaluation: dict[str, Any] | None = None
    ) -> ModelVersion:
        """Fit `name` on all rows of `data` (from `build_dataset`) and register a candidate.

        `evaluation`: this model's walk-forward summary, kept with the version so an approval
        can be judged on out-of-sample evidence."""
        if data.empty:
            raise RegistryError("no training rows")
        model = make(name).fit(rank_transform(data), data["net"], data["excess"])
        blob = pickle.dumps(model, protocol=5)
        sha = hashlib.sha256(blob).hexdigest()
        fp = frame_fingerprint(data[["instrument_id", "trade_date", *FEATURES, "net"]])
        model_id = f"{name}-h{horizon}-{sha[:12]}"
        existing = self.s.get(ModelVersion, model_id)
        if existing is not None:
            return existing  # same bytes = same model
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{model_id}.pkl"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        tmp.replace(path)
        mv = ModelVersion(
            model_id=model_id,
            name=name,
            horizon=horizon,
            target=_target(model),
            feature_set=FEATURE_SET,
            feature_hash=definition_hash(),
            label_set=LABEL_SET,
            code_version=__version__,
            data_fingerprint=fp,
            train_first_date=data["trade_date"].min().date(),
            train_last_date=data["trade_date"].max().date(),
            train_rows=len(data),
            params=model.params(),
            evaluation=evaluation,
            artifact_path=path.name,
            artifact_sha256=sha,
            status=ModelStatus.CANDIDATE.value,
        )
        self.s.add(mv)
        self.s.flush()
        return mv

    def _get(self, model_id: str) -> ModelVersion:
        mv = self.s.get(ModelVersion, model_id)
        if mv is None:
            raise RegistryError(f"unknown model {model_id}")
        return mv

    def _decide(self, mv: ModelVersion, status: ModelStatus, by: str, note: str) -> None:
        if not by.strip() or not note.strip():
            raise RegistryError("a decision needs the approver's name and a note")
        mv.status = status.value
        mv.decided_by = by.strip()
        mv.decided_at = datetime.now(tz=UTC)
        mv.decision_note = note.strip()

    def approve(self, model_id: str, by: str, note: str) -> ModelVersion:
        mv = self._get(model_id)
        if mv.status != ModelStatus.CANDIDATE.value:
            raise RegistryError(f"{model_id} is {mv.status}; only candidates can be approved")
        if not mv.evaluation:
            raise RegistryError(f"{model_id} has no walk-forward evaluation; evaluate before approving")
        if mv.feature_hash != definition_hash():
            raise RegistryError(f"{model_id} was trained on other feature definitions")
        for old in self.s.scalars(
            select(ModelVersion).where(
                ModelVersion.horizon == mv.horizon, ModelVersion.status == ModelStatus.APPROVED.value
            )
        ):
            self._decide(old, ModelStatus.RETIRED, by, f"replaced by {model_id}")
        self._decide(mv, ModelStatus.APPROVED, by, note)
        self.s.flush()
        return mv

    def reject(self, model_id: str, by: str, note: str) -> ModelVersion:
        mv = self._get(model_id)
        if mv.status != ModelStatus.CANDIDATE.value:
            raise RegistryError(f"{model_id} is {mv.status}; only candidates can be rejected")
        self._decide(mv, ModelStatus.REJECTED, by, note)
        self.s.flush()
        return mv

    def production(self, horizon: int) -> ModelVersion | None:
        return self.s.scalar(
            select(ModelVersion).where(
                ModelVersion.horizon == horizon, ModelVersion.status == ModelStatus.APPROVED.value
            )
        )

    def load(self, model_id: str) -> Model:
        mv = self._get(model_id)
        blob = (self.root / mv.artifact_path).read_bytes()
        if hashlib.sha256(blob).hexdigest() != mv.artifact_sha256:
            raise RegistryError(f"{model_id}: artifact hash mismatch, refusing to load")
        model: Model = pickle.loads(blob)
        return model

    def list(self) -> list[ModelVersion]:
        return list(self.s.scalars(select(ModelVersion).order_by(ModelVersion.created_at)))


__all__ = ["ModelRegistry", "RegistryError"]
