from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from idx_trading.ingestion.adapters import CsvEodAdapter
from idx_trading.ingestion.schemas import FetchRequest, RawPayload
from tests.conftest import FIXTURES


def _payload(text: str, **meta) -> RawPayload:
    return RawPayload(text.encode(), "text/csv", "inline.csv", datetime.now(tz=UTC), meta)


def test_standard_profile_parses_fixture():
    a = CsvEodAdapter()
    [p] = list(a.fetch(FetchRequest(paths=(str(FIXTURES / "eod_2026-11-20.csv"),))))
    r = a.parse(p)
    assert not r.errors
    bbca = next(b for b in r.bars if b.ticker == "BBCA")
    assert bbca.trade_date == date(2026, 11, 20)
    assert bbca.close == Decimal("9875") and bbca.volume == 85_000_000
    assert bbca.foreign_buy_volume == 30_000_000
    newx = next(b for b in r.bars if b.ticker == "NEWX")
    assert newx.foreign_buy_volume is None


def test_idx_summary_profile_with_file_date():
    a = CsvEodAdapter("idx_stock_summary")
    req = FetchRequest(paths=(str(FIXTURES / "idx_stock_summary.csv"),), options={"trade_date": "2026-11-20"})
    [p] = list(a.fetch(req))
    r = a.parse(p)
    assert not r.errors and len(r.bars) == 2
    assert all(b.trade_date == date(2026, 11, 20) for b in r.bars)
    assert r.bars[0].prev_close == Decimal("9800")


def test_bad_rows_are_reported_not_raised():
    r = CsvEodAdapter().parse(
        _payload(
            "ticker,date,close,volume\nBBCA,2026-11-20,abc,1\nTLKM,2026-13-40,10,1\n"
            'ASII,2026-11-20,10,1.5\nBR$X,2026-11-20,10,1\nUNVR,20/11/2026,"2,500",100\n'
        )
    )
    assert len(r.errors) == 4
    assert [b.ticker for b in r.bars] == ["UNVR"] and r.bars[0].close == Decimal("2500")


def test_missing_required_columns():
    r = CsvEodAdapter().parse(_payload("ticker,open\nBBCA,1\n"))
    assert not r.bars and "close" in r.errors[0].message and "trade_date" in r.errors[0].message


def test_comma_decimal_option():
    r = CsvEodAdapter().parse(_payload('ticker,date,close\nBBCA,2026-11-20,"9.875,5"\n', decimal=","))
    assert r.bars[0].close == Decimal("9875.5")


def test_unknown_profile():
    with pytest.raises(ValueError, match="unknown CSV profile"):
        CsvEodAdapter("nope")
