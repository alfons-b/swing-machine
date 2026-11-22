from datetime import date, timedelta
from decimal import Decimal as Dec

import pytest

from idx_trading.calendar import seed_from_exchange_calendars
from idx_trading.db.models import DailyPrice
from idx_trading.db.session import session_scope
from idx_trading.features import technical
from idx_trading.features.store import build_features, read_features, write_features
from idx_trading.instruments import InstrumentService


def _bars(s, iid, start, n, base):
    cal = [start + timedelta(days=i) for i in range(n * 2)]
    days = [d for d in cal if d.weekday() < 5][:n]
    for i, d in enumerate(days):
        c = Dec(base + (i % 7) * 5)
        s.add(
            DailyPrice(
                instrument_id=iid,
                trade_date=d,
                source_id="eodhd",
                open=c,
                high=c + 10,
                low=c - 10,
                close=c,
                volume=1000 + i,
                validation_status="valid",
                record_hash=f"{iid}{d}",
                version=1,
                is_current=True,
            )
        )
    return days


def test_build_write_read(seeded, tmp_path, monkeypatch):
    with session_scope(seeded) as s:
        seed_from_exchange_calendars(s, date(2026, 1, 1), date(2026, 12, 31))
        svc = InstrumentService(s)
        bbca, tlkm = svc.resolve("BBCA", date(2026, 11, 20)), svc.resolve("TLKM", date(2026, 11, 20))
        _bars(s, bbca, date(2026, 6, 1), 80, 9000)
        _bars(s, tlkm, date(2026, 10, 1), 30, 3000)  # too short: skipped
    with session_scope(seeded) as s:
        df, man = build_features(s, as_of=date(2026, 11, 20))
    assert man.instruments == 1 and man.skipped_short_history >= 1 and len(df) == 80
    assert df["ticker"].unique().tolist() == ["BBCA"]
    write_features(df, man, tmp_path)
    back = read_features(tmp_path)
    assert len(back) == 80 and back["rsi_14"].notna().sum() > 50
    monkeypatch.setattr(technical, "definition_hash", lambda: "changed")
    import idx_trading.features.store as store

    monkeypatch.setattr(store, "definition_hash", lambda: "changed")
    with pytest.raises(RuntimeError, match="rebuild"):
        read_features(tmp_path)
