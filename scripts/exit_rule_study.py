"""Exit-rule study (OI-47): same walk-forward signals, different stop/target widths.

Research only: it prints a table and changes no configuration. Choosing the best row of this
table on the same period would be in-sample selection, so any change must be re-tested on
later data before it is adopted.

    uv run python scripts/exit_rule_study.py [horizon] [model]
"""

from __future__ import annotations

import sys
import warnings

import pandas as pd

from idx_trading.backtest.engine import Costs, equal_weight_benchmark, metrics, simulate
from idx_trading.backtest.run import build_orders
from idx_trading.config import get_settings
from idx_trading.db.session import make_engine, make_session_factory
from idx_trading.features.store import bars_frame
from idx_trading.models.dataset import Floor, load_dataset
from idx_trading.models.evaluate import EvalConfig, walk_forward
from idx_trading.trading_config import load_trading_config

warnings.filterwarnings("ignore")
H = int(sys.argv[1]) if len(sys.argv) > 1 else 5
MODEL = sys.argv[2] if len(sys.argv) > 2 else "lgbm_reg"
GRID = [(s, r) for s in (1.0, 1.5, 2.5, 4.0, 50.0) for r in (1.0, 2.0, 3.0, 50.0)]  # 50 = effectively none

st, cfg = get_settings(), load_trading_config()
root = st.data_dir / "features"
u = cfg.universe
data, info = load_dataset(root, H, Floor(float(u.min_price_idr), float(u.min_adv_idr)))
_, oos = walk_forward(data, EvalConfig(H, (MODEL,)), info)
labels = pd.read_parquet(root / "labels_v1.parquet")
sessions = sorted(
    pd.to_datetime(
        pd.read_parquet(root / "tech_v1.parquet", columns=["trade_date"])["trade_date"]
    ).dt.date.unique()
)
start = pd.Timestamp(oos[oos["fold"] == 1]["trade_date"].min()).date()
sim = [d for d in sessions if d >= start]
engine = make_engine()
factory = make_session_factory(engine)
bars: dict[int, pd.DataFrame] = {}
with factory() as s:
    for iid in sorted(data["instrument_id"].unique()):
        df = bars_frame(s, int(iid), None)
        if len(df):
            bars[int(iid)] = df.set_index("trade_date")
engine.dispose()
costs = Costs(float(cfg.costs.buy), float(cfg.costs.sell), cfg.slippage.ticks_per_side, cfg.lot_size)
universe = sorted(data[data["trade_date"] == pd.Timestamp(start)]["instrument_id"].unique())
bench = equal_weight_benchmark({i: bars[i] for i in universe if i in bars}, sim, float(cfg.capital_idr))
print(
    f"H{H} {MODEL} {sim[0]}..{sim[-1]}  "
    f"equal-weight benchmark {bench.iloc[-1] / float(cfg.capital_idr) - 1:+.1%}"
)
print("| stop ATR | reward:risk | hurdle | trades | hit | mean trade | total | max DD | stop/target/expiry |")
print("|---|---|---|---|---|---|---|---|---|")
for ignore in (False, True):
    for stop, rr in GRID:
        orders, _ = build_orders(
            data,
            oos,
            labels,
            cfg,
            H,
            sessions,
            ignore_hurdle=ignore,
            overrides={"stop_atr": stop, "reward_risk": rr},
        )
        m = metrics(simulate(orders, bars, sim, float(cfg.capital_idr), costs))
        ex = m["exits"]
        mix = "/".join(
            str(sum(v for k, v in ex.items() if k.startswith(w))) for w in ("stop", "target", "expiry")
        )
        mt = "-" if m["trades"] == 0 else f"{m['mean_trade_return']:+.2%}"
        hit = "-" if m["trades"] == 0 else f"{m['hit_rate']:.0%}"
        print(
            f"| {'none' if stop >= 50 else stop} | {'none' if rr >= 50 else rr} "
            f"| {'off' if ignore else 'on'} "
            f"| {m['trades']} | {hit} | {mt} | {m['total_return']:+.1%} | {m['max_drawdown']:+.1%} | {mix} |"
        )
