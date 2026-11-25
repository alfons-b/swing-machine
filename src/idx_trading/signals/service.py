"""Generate and store one day's recommendations (ADR 0016).

Production signals need an owner-approved model (ADR 0015) and are made only for the latest
session in the feature store. Anything else (a candidate model, a past date) is `shadow`:
recorded for research, never presented as a recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from idx_trading import __version__
from idx_trading.calendar import CalendarService
from idx_trading.db.enums import IssueStatus, ModelStatus, Severity, SignalMode
from idx_trading.db.models import DataQualityIssue, Signal, SignalRun
from idx_trading.features.store import read_features
from idx_trading.features.technical import definition_hash
from idx_trading.instruments import InstrumentService
from idx_trading.models.dataset import rank_transform
from idx_trading.models.registry import ModelRegistry
from idx_trading.signals.engine import DayContext, Decision, decide, market_drift
from idx_trading.trading_config import TradingConfig


class SignalError(RuntimeError):
    pass


@dataclass
class RunSummary:
    run_id: int
    signal_date: date
    mode: str
    model_id: str
    counts: dict[str, int]
    drift: float
    cross_section: int
    decisions: list[Decision]


def _open_errors(s: Session, on: date, days: int) -> dict[int, int]:
    rows = s.execute(
        select(DataQualityIssue.instrument_id, func.count())
        .where(
            DataQualityIssue.severity == Severity.ERROR.value,
            DataQualityIssue.status == IssueStatus.OPEN.value,
            DataQualityIssue.instrument_id.is_not(None),
            DataQualityIssue.trade_date.between(on - timedelta(days=days), on),
        )
        .group_by(DataQualityIssue.instrument_id)
    ).all()
    return {int(i): int(n) for i, n in rows if i is not None}


def generate(
    s: Session,
    features_root: Path,
    models_root: Path,
    cfg: TradingConfig,
    horizon: int,
    *,
    model_id: str | None = None,
    shadow: bool = False,
    as_of: date | None = None,
    calendar_code: str = "XIDX",
) -> RunSummary:
    reg = ModelRegistry(s, models_root)
    mv = reg.production(horizon) if model_id is None else reg._get(model_id)
    if mv is None:
        raise SignalError(
            f"no approved model for H{horizon}: approve one (idx models approve) "
            "or pass --model-id with --shadow"
        )
    if mv.horizon != horizon:
        raise SignalError(f"{mv.model_id} is a H{mv.horizon} model, not H{horizon}")
    if mv.target != "excess":
        raise SignalError(f"{mv.model_id} predicts {mv.target}; signals need an excess-return model")
    calib = (mv.evaluation or {}).get("excess_calibration")
    if not calib:
        raise SignalError(
            f"{mv.model_id} has no out-of-sample excess calibration: run `idx models evaluate` "
            "and `idx models train` again"
        )
    if calib["slope"] <= 0:
        raise SignalError(f"{mv.model_id}: out-of-sample slope {calib['slope']:.3f} <= 0, no ranking skill")
    if mv.feature_hash != definition_hash():
        raise SignalError(f"{mv.model_id} was trained on other feature definitions")

    feats = read_features(features_root)
    feats["trade_date"] = pd.to_datetime(feats["trade_date"])
    latest = feats["trade_date"].max().date()
    signal_date = as_of or latest
    mode = SignalMode.PRODUCTION
    if shadow or mv.status != ModelStatus.APPROVED.value or signal_date != latest:
        if not shadow:
            raise SignalError(
                f"production signals need an approved model and the latest session ({latest}); "
                f"{mv.model_id} is {mv.status}, date {signal_date}: use --shadow"
            )
        mode = SignalMode.SHADOW

    done = s.scalar(
        select(SignalRun.run_id).where(
            SignalRun.signal_date == signal_date,
            SignalRun.horizon == horizon,
            SignalRun.model_id == mv.model_id,
            SignalRun.mode == mode.value,
        )
    )
    if done is not None:
        raise SignalError(
            f"run {done} already holds the {mode.value} signals for {signal_date} H{horizon} "
            f"{mv.model_id}; signals are immutable"
        )

    cal = CalendarService(s, calendar_code)
    ahead = cal.sessions(signal_date + timedelta(days=1), signal_date + timedelta(days=120))
    if len(ahead) < horizon:
        raise SignalError(f"calendar does not reach {horizon} sessions after {signal_date}")
    expiry = ahead[horizon - 1]

    u = cfg.universe
    hist = feats[feats["trade_date"] <= pd.Timestamp(signal_date)]
    last = hist.groupby("instrument_id")["trade_date"].max()
    day = hist[hist["trade_date"] == pd.Timestamp(signal_date)].copy()
    passing = day[(day["raw_close"] >= float(u.min_price_idr)) & (day["adv_20_idr"] >= float(u.min_adv_idr))]
    scores = pd.Series(dtype=float)
    if len(passing):
        model = reg.load(mv.model_id)
        x = rank_transform(passing)
        score, _ = model.predict(x)
        # expected excess from the out-of-sample line, not the raw (over-confident) score
        expected = calib["intercept"] + calib["slope"] * score
        scores = pd.Series(expected, index=passing["instrument_id"].to_numpy())
    day = day.set_index("instrument_id")

    svc = InstrumentService(s)
    errors = _open_errors(s, signal_date, cfg.signals.dq_lookback_days)
    rows: list[dict[str, Any]] = []
    for m in svc.collection_universe_on(signal_date):
        iid = m.instrument_id
        f = day.loc[iid] if iid in day.index else None
        rows.append(
            {
                "instrument_id": iid,
                "ticker": m.ticker,
                "eligible": m.eligible,
                "eligibility_reason": m.reason,
                "last_date": last.get(iid),
                "raw_close": None if f is None else float(f["raw_close"]),
                "adv_20_idr": None if f is None else float(f["adv_20_idr"]),
                "atr_14_pct": None if f is None else float(f["atr_14_pct"]),
                "open_errors": errors.get(iid, 0),
                "score": float(scores[iid]) if iid in scores.index else float("nan"),
            }
        )
    labels = pd.read_parquet(features_root / "labels_v1.parquet")
    drift = market_drift(labels, horizon, signal_date, cfg.signals.drift_sessions)
    ctx = DayContext(
        signal_date,
        expiry,
        drift,
        float(cfg.capital_idr),
        cfg.lot_size,
        float(u.min_price_idr),
        float(u.min_adv_idr),
    )
    decisions = decide(pd.DataFrame(rows), ctx, cfg.signals)
    counts = {a: sum(d.action == a for d in decisions) for a in ("BUY", "WATCH", "AVOID")}
    run = SignalRun(
        signal_date=signal_date,
        horizon=horizon,
        model_id=mv.model_id,
        mode=mode.value,
        expiry_date=expiry,
        drift=None if pd.isna(drift) else drift,
        cross_section=len(passing),
        counts=counts,
        config={"signals": cfg.signals.model_dump(), "universe": u.model_dump(mode="json")},
        inputs={
            "feature_hash": definition_hash(),
            "features_last_date": str(latest),
            "model_status": mv.status,
            "model_data_fingerprint": mv.data_fingerprint,
            "excess_calibration": calib,
        },
        code_version=__version__,
    )
    s.add(run)
    try:
        s.flush()
    except IntegrityError as exc:
        raise SignalError(
            f"signals for {signal_date} H{horizon} {mv.model_id} ({mode.value}) already exist; "
            "they are immutable"
        ) from exc
    for d in decisions:
        s.add(
            Signal(
                run_id=run.run_id,
                instrument_id=d.instrument_id,
                ticker=d.ticker,
                action=d.action,
                rank=d.rank,
                score=d.score,
                expected_net=d.expected_net,
                entry_ref=d.entry_ref,
                max_entry=d.max_entry,
                stop=d.stop,
                target=d.target,
                lots=d.lots,
                reasons="; ".join(d.reasons) or None,
            )
        )
    s.flush()
    return RunSummary(
        run.run_id, signal_date, mode.value, mv.model_id, counts, drift, len(passing), decisions
    )


__all__ = ["RunSummary", "SignalError", "generate"]
