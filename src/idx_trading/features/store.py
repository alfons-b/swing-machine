"""Build and read the `tech_v1` feature store (Parquet, data/features/; git-ignored)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from idx_trading.clock import exchange_today
from idx_trading.corporate_actions.adjusted import Adjustment, adjusted_bars
from idx_trading.features.technical import (
    DEFINITIONS,
    FEATURE_SET,
    MIN_BARS,
    NAMES,
    cross_sectional,
    definition_hash,
    instrument_features,
)
from idx_trading.instruments import InstrumentService


@dataclass
class FeatureManifest:
    feature_set: str
    definition_hash: str
    built_at: str
    as_of: str | None
    instruments: int
    rows: int
    first_date: str | None
    last_date: str | None
    skipped_short_history: int
    features: list[dict[str, object]]


def bars_frame(s: Session, instrument_id: int, as_of: date | None) -> pd.DataFrame:
    tr = adjusted_bars(s, instrument_id, Adjustment.TOTAL_RETURN, end=as_of, as_of=as_of)
    raw = {b.trade_date: b for b in adjusted_bars(s, instrument_id, Adjustment.RAW, end=as_of, as_of=as_of)}
    rows = []
    for b in tr:
        r = raw.get(b.trade_date)
        if (
            b.open is None
            or b.high is None
            or b.low is None
            or b.close is None
            or r is None
            or r.close is None
        ):
            continue  # no-trade days have no O/H/L; they do not enter the features
        rows.append(
            {
                "trade_date": b.trade_date,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(b.volume or 0),
                "raw_open": float(r.open) if r.open is not None else float("nan"),
                "raw_close": float(r.close),
                "raw_volume": float(r.volume or 0),
            }
        )
    return pd.DataFrame(rows)


def build_features(
    s: Session, as_of: date | None = None, min_bars: int = MIN_BARS
) -> tuple[pd.DataFrame, FeatureManifest]:
    """Features for every collected common stock with at least `min_bars` usable bars.

    `as_of` limits both the bars and the corporate-action adjustments (point in time).
    """
    svc = InstrumentService(s)
    on = as_of or exchange_today()
    frames, skipped = [], 0
    for m in svc.collection_universe_on(on):
        df = bars_frame(s, m.instrument_id, as_of)
        if len(df) < min_bars:
            skipped += 1
            continue
        df = df.sort_values("trade_date").reset_index(drop=True)
        f = instrument_features(df)
        f.insert(0, "raw_close", df["raw_close"])  # passthrough for price floors, not a feature
        f.insert(0, "trade_date", df["trade_date"])
        f.insert(0, "ticker", m.ticker)
        f.insert(0, "instrument_id", m.instrument_id)
        frames.append(f)
    if not frames:
        out = pd.DataFrame(columns=["instrument_id", "ticker", "trade_date", "raw_close", *NAMES])
    else:
        out = cross_sectional(pd.concat(frames, ignore_index=True))
        out = out[["instrument_id", "ticker", "trade_date", "raw_close", *NAMES]]
    man = FeatureManifest(
        FEATURE_SET,
        definition_hash(),
        datetime.now(tz=UTC).isoformat(),
        as_of.isoformat() if as_of else None,
        int(out["instrument_id"].nunique()) if len(out) else 0,
        len(out),
        str(out["trade_date"].min()) if len(out) else None,
        str(out["trade_date"].max()) if len(out) else None,
        skipped,
        [asdict(d) for d in DEFINITIONS],
    )
    return out, man


def write_features(df: pd.DataFrame, man: FeatureManifest, root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{man.feature_set}.parquet"
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)
    (root / f"{man.feature_set}.manifest.json").write_text(json.dumps(asdict(man), indent=2))
    return path


def read_features(root: Path, feature_set: str = FEATURE_SET) -> pd.DataFrame:
    man = json.loads((root / f"{feature_set}.manifest.json").read_text())
    if feature_set == FEATURE_SET and man["definition_hash"] != definition_hash():
        raise RuntimeError(f"{feature_set} was built with other definitions: rebuild it")
    return pd.read_parquet(root / f"{feature_set}.parquet")


def build_labels(
    s: Session, buy: float, sell: float, ticks_per_side: int = 1, min_bars: int = MIN_BARS
) -> tuple[pd.DataFrame, dict[str, object]]:
    """labels_v1 for every collected stock with enough history (uses all known data)."""
    from idx_trading.features.labels import HORIZONS, LABEL_COLUMNS, LABEL_SET, add_excess, instrument_labels

    svc = InstrumentService(s)
    frames = []
    for m in svc.collection_universe_on(exchange_today()):
        df = bars_frame(s, m.instrument_id, None)
        if len(df) < min_bars:
            continue
        df = df.sort_values("trade_date").reset_index(drop=True)
        lab = instrument_labels(df, buy, sell, ticks_per_side)
        lab.insert(0, "trade_date", df["trade_date"])
        lab.insert(0, "ticker", m.ticker)
        lab.insert(0, "instrument_id", m.instrument_id)
        frames.append(lab)
    out = (
        add_excess(pd.concat(frames, ignore_index=True))
        if frames
        else pd.DataFrame(columns=["instrument_id", "ticker", "trade_date", *LABEL_COLUMNS])
    )
    meta: dict[str, object] = {
        "label_set": LABEL_SET,
        "horizons": list(HORIZONS),
        "buy_cost": buy,
        "sell_cost": sell,
        "ticks_per_side": ticks_per_side,
        "rows": len(out),
        "instruments": int(out["instrument_id"].nunique()) if len(out) else 0,
        "built_at": datetime.now(tz=UTC).isoformat(),
        "labelled_rows_by_horizon": {h: int(out[f"net_{h}"].notna().sum()) for h in HORIZONS}
        if len(out)
        else {},
    }
    return out, meta
