"""M2.4: recommendation rules, levels, sizing, drift, and the immutable registry (ADR 0016)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.orm import Session, sessionmaker

from idx_trading.db.models import ImmutableRecordError, SignalRun
from idx_trading.signals.engine import Action, DayContext, decide, market_drift, round_down, round_up, tick
from idx_trading.signals.service import SignalError, generate
from idx_trading.trading_config import Signals, load_trading_config

D = date(2026, 11, 20)
CTX = DayContext(D, date(2026, 11, 27), drift=-0.01, capital=100e6, lot_size=100, min_price=200, min_adv=1e9)
CFG = Signals(min_cross_section=10, top_fraction=0.2, buy_min_expected_net=0.005)


def cand(i: int, score: float, **kw: object) -> dict[str, object]:
    row: dict[str, object] = {
        "instrument_id": i,
        "ticker": f"T{i:03d}",
        "eligible": True,
        "eligibility_reason": None,
        "last_date": pd.Timestamp(D),
        "raw_close": 1000.0,
        "adv_20_idr": 5e9,
        "atr_14_pct": 0.04,
        "open_errors": 0,
        "score": score,
    }
    row.update(kw)
    return row


def run(rows: list[dict[str, object]], ctx: DayContext = CTX, cfg: Signals = CFG) -> dict[str, object]:
    return {d.ticker: d for d in decide(pd.DataFrame(rows), ctx, cfg)}


def test_tick_rounding():
    assert [tick(p) for p in (150, 300, 1500, 3000, 9000)] == [1, 2, 5, 10, 25]
    assert round_down(1043) == 1040 and round_up(1041) == 1045
    assert round_down(9_987) == 9_975 and round_up(9_976) == 10_000


def test_levels_and_size_by_hand():
    rows = [cand(i, 0.0) for i in range(1, 10)] + [cand(0, 0.03)]
    d = run(rows)["T000"]
    assert d.action == Action.BUY and d.rank == 1
    # close 1000, ATR 40: max entry 1000+20, stop 1000-60, target 1000+2x60
    assert (d.entry_ref, d.max_entry, d.stop, d.target) == (1000, 1020, 940, 1120)
    # risk: 1m / 60 = 16,666 sh; position cap 20m/1000 = 20,000; ADV cap 100m/1000 = 100,000 -> 166 lots
    assert d.lots == 166
    assert d.expected_net == pytest.approx(0.02)


def test_avoid_gates_in_order_with_reasons():
    rows = [
        cand(1, 0.05, eligible=False, eligibility_reason="special monitoring board"),
        cand(2, 0.05, last_date=pd.Timestamp("2026-11-19")),
        cand(3, 0.05, open_errors=2),
        cand(4, 0.05, raw_close=150.0),
        cand(5, 0.05, adv_20_idr=5e8),
        cand(6, 0.05, atr_14_pct=float("nan")),
        cand(7, float("nan")),
    ]
    out = run(rows)
    assert all(d.action == Action.AVOID for d in out.values())
    assert "not eligible" in out["T001"].reasons[0]
    assert "no price for 2026-11-20" in out["T002"].reasons[0]
    assert "data-quality" in out["T003"].reasons[0]
    assert "below floor" in out["T004"].reasons[0] and "below floor" in out["T005"].reasons[0]
    assert "ATR" in out["T006"].reasons[0]
    assert "could not score" in out["T007"].reasons[0]


def test_only_the_top_fraction_can_be_bought_and_the_hurdle_applies():
    rows = [cand(i, 0.001 * i) for i in range(1, 21)]  # expected = 0.001 i - 0.01
    out = run(rows)
    top = sorted((d for d in out.values() if d.rank), key=lambda d: d.rank or 0)
    assert [d.action for d in top[:4]] == [Action.BUY] * 4  # 20 x 20% = 4; exp >= +0.6%
    assert all(d.action == Action.AVOID and "outside the top" in d.reasons[0] for d in top[4:])
    low = run([cand(i, 0.001 * i) for i in range(1, 21)], ctx=DayContext(**{**CTX.__dict__, "drift": -0.02}))
    assert all(d.action != Action.BUY for d in low.values())  # best expected -0.0% < +0.5%
    assert any("hurdle" in r for d in low.values() for r in d.reasons)


def test_small_cross_section_unknown_drift_and_zero_lots_are_watch_not_buy():
    few = run([cand(i, 0.05) for i in range(5)])
    assert {d.action for d in few.values()} <= {Action.WATCH, Action.AVOID}
    assert any("cross-section" in r for d in few.values() for r in d.reasons)
    nan_ctx = DayContext(**{**CTX.__dict__, "drift": float("nan")})
    nodrift = run([cand(i, 0.05) for i in range(10)], ctx=nan_ctx)
    assert not any(d.action == Action.BUY for d in nodrift.values())
    tiny = DayContext(**{**CTX.__dict__, "capital": 1e6})  # 10k risk / 60 = 166 sh -> 1 lot
    assert run([cand(i, 0.05) for i in range(10)], ctx=tiny)["T000"].lots == 1
    broke = DayContext(**{**CTX.__dict__, "capital": 2e5})
    d = run([cand(i, 0.05) for i in range(10)], ctx=broke)["T000"]
    assert d.action == Action.WATCH and "zero lots" in d.reasons[0]


def test_market_drift_uses_only_labels_closed_by_the_signal_date():
    days = pd.bdate_range("2026-10-01", periods=40)
    rows = []
    for i, d in enumerate(days):
        for k in range(5):
            rows.append({"trade_date": d, "net_5": -0.01 * k, "end_5": days[min(i + 5, 39)]})
    labels = pd.DataFrame(rows)
    sig = days[20].date()
    drift = market_drift(labels, 5, sig, 10)
    assert drift == pytest.approx(-0.02)
    # a label still open on the signal date never counts
    labels.loc[labels["end_5"] > pd.Timestamp(sig), "net_5"] = 99.0
    assert market_drift(labels, 5, sig, 10) == pytest.approx(-0.02)
    assert np.isnan(market_drift(labels, 5, days[0].date(), 10))


def test_shipped_signal_config_loads():
    cfg = load_trading_config()
    assert cfg.signals.buy_min_expected_net >= 0 and cfg.signals.min_cross_section >= 10


# --- registry and service refusals -------------------------------------------------------------


@pytest.fixture
def db(factory: sessionmaker[Session], tmp_path):
    from idx_trading.models.registry import ModelRegistry
    from tests.unit.test_models import synthetic

    with factory() as s:
        reg = ModelRegistry(s, tmp_path / "models")
        yield s, reg, synthetic(80, 10), tmp_path


def test_signal_rows_cannot_be_changed_or_deleted(db):
    s, reg, data, _ = db
    mv = reg.train_and_register("momentum", 5, data, None)
    run_ = SignalRun(
        signal_date=D,
        horizon=5,
        model_id=mv.model_id,
        mode="shadow",
        expiry_date=date(2026, 11, 27),
        drift=None,
        cross_section=0,
        counts={},
        config={},
        inputs={},
        code_version="t",
    )
    s.add(run_)
    s.commit()
    run_.cross_section = 5
    with pytest.raises(ImmutableRecordError):
        s.flush()
    s.rollback()
    s.delete(s.get(SignalRun, run_.run_id))
    with pytest.raises(ImmutableRecordError):
        s.flush()
    s.rollback()
    assert s.get(SignalRun, run_.run_id).cross_section == 0


def test_generate_refuses_without_an_approved_calibrated_regression_model(db):
    s, reg, data, tmp = db
    cfg = load_trading_config()
    with pytest.raises(SignalError, match="no approved model"):
        generate(s, tmp, reg.root, cfg, 5)
    cls = reg.train_and_register("logistic", 5, data, {"overall": {}})
    with pytest.raises(SignalError, match="excess-return model"):
        generate(s, tmp, reg.root, cfg, 5, model_id=cls.model_id, shadow=True)
    nocal = reg.train_and_register("ridge", 5, data, {"overall": {}})
    with pytest.raises(SignalError, match="calibration"):
        generate(s, tmp, reg.root, cfg, 5, model_id=nocal.model_id, shadow=True)
    h10 = reg.train_and_register("lgbm_reg", 10, data, None)
    with pytest.raises(SignalError, match="H10 model"):
        generate(s, tmp, reg.root, cfg, 5, model_id=h10.model_id, shadow=True)
