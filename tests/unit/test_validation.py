from datetime import date
from decimal import Decimal

import pytest

from idx_trading.db.enums import ListingStatus, Severity, ValidationStatus
from idx_trading.ingestion.schemas import EodBar
from idx_trading.ingestion.validation import (
    BarContext,
    Rule,
    dedupe_batch,
    normalize_no_trade,
    status_of,
    validate_bar,
)

D = date(2026, 11, 20)


def bar(**kw) -> EodBar:
    base = dict(
        ticker="BBCA",
        trade_date=D,
        open=100,
        high=110,
        low=95,
        close=105,
        volume=1000,
        value=102000,
        frequency=10,
    )
    base.update(kw)
    return EodBar.model_validate(base)


def ctx(**kw) -> BarContext:
    base = dict(
        today=D,
        instrument_id=1,
        listing_status=ListingStatus.ACTIVE,
        is_trading_day=True,
        reference_close=Decimal(100),
        extreme_move_threshold=0.35,
    )
    base.update(kw)
    return BarContext(**base)


def codes(b, c=None):
    return {f.rule_code for f in validate_bar(b, c or ctx())}


def test_clean_bar_is_valid():
    assert validate_bar(bar(), ctx()) == []


@pytest.mark.parametrize(
    ("kw", "rule"),
    [
        ({"close": None}, Rule.CLOSE_MISSING),
        ({"low": 0}, Rule.PRICE_NON_POSITIVE),
        ({"high": 90}, Rule.OHLC_INCONSISTENT),  # high < low
        ({"open": 120}, Rule.OHLC_INCONSISTENT),  # open above high
        ({"close": 94}, Rule.OHLC_INCONSISTENT),  # close below low
        ({"high": None}, Rule.OHLC_INCONSISTENT),  # only one of high/low
        ({"volume": -1}, Rule.NEGATIVE_QUANTITY),
        ({"trade_date": date(2026, 11, 23)}, Rule.FUTURE_DATE),
    ],
)
def test_error_rules(kw, rule):
    findings = validate_bar(bar(**kw), ctx())
    assert rule in {f.rule_code for f in findings}
    assert status_of(findings) == ValidationStatus.INVALID


def test_context_rules():
    assert Rule.UNKNOWN_TICKER in codes(bar(), ctx(instrument_id=None))
    assert Rule.NOT_TRADING_DAY in codes(bar(), ctx(is_trading_day=False))
    f = validate_bar(bar(), ctx(is_trading_day=None))
    assert {x.rule_code for x in f} == {Rule.CALENDAR_UNKNOWN}
    assert status_of(f) == ValidationStatus.WARNING
    assert Rule.INSTRUMENT_NOT_ACTIVE in codes(bar(), ctx(listing_status=ListingStatus.SUSPENDED))


def test_unknown_listing_status_is_info_only():
    f = validate_bar(bar(), ctx(listing_status=ListingStatus.UNKNOWN))
    assert [x.rule_code for x in f] == [Rule.LISTING_STATUS_UNKNOWN]
    assert status_of(f) == ValidationStatus.VALID


def test_extreme_move_is_warning():
    f = validate_bar(bar(high=150, close=140), ctx())
    assert [x.rule_code for x in f] == [Rule.EXTREME_MOVE]
    assert f[0].severity == Severity.WARNING and f[0].details["change"] == pytest.approx(0.4)
    assert validate_bar(bar(close=105), ctx(reference_close=None)) == []


def test_volume_value_mismatch():
    assert Rule.VOLUME_VALUE_MISMATCH in codes(bar(value=0))
    assert Rule.VOLUME_VALUE_MISMATCH in codes(bar(value=500_000))  # vwap 500 >> high


def test_no_trade_normalization():
    b, f = normalize_no_trade(bar(open=0, high=0, low=0, close=100, volume=0, value=0))
    assert (b.open, b.high, b.low, b.close) == (None, None, None, Decimal(100))
    assert f[0].rule_code == Rule.NO_TRADE and f[0].severity == Severity.INFO
    assert validate_bar(b, ctx()) == []
    untouched, f2 = normalize_no_trade(bar())
    assert untouched == bar() and f2 == []


def test_dedupe_batch():
    a, b_same, b_diff = bar(), bar(), bar(close=106)
    survivors, findings = dedupe_batch([a, b_same])
    assert survivors == [a] and findings[a.key][0].rule_code == Rule.DUPLICATE_IDENTICAL
    survivors, findings = dedupe_batch([a, b_diff, bar(ticker="TLKM")])
    assert [s.ticker for s in survivors] == ["TLKM"]
    assert findings[a.key][0].rule_code == Rule.DUPLICATE_CONFLICT


def test_whole_rupiah_and_tick_grid():
    f = validate_bar(bar(close=Decimal("105.5")), ctx())
    assert Rule.PRICE_NOT_INTEGER in {x.rule_code for x in f} and status_of(f) == ValidationStatus.INVALID
    sentinel = bar(high=Decimal("999999.9999"), close=Decimal("999999.9999"))
    assert Rule.PRICE_NOT_INTEGER in codes(sentinel, ctx(reference_close=None))
    # 2,003 is off the grid (tick 5 for 500-2,000? no: 2,003 >= 2,000 -> tick 10)
    off = bar(open=2000, high=2010, low=1995, close=2003, value=2_003_000)
    assert [x.rule_code for x in validate_bar(off, ctx(reference_close=None))] == [Rule.OFF_TICK]
    usd = bar(close=Decimal("105.5"))
    assert Rule.PRICE_NOT_INTEGER not in codes(usd, ctx(currency="USD"))


def test_vendor_holiday_filler_is_info_not_error():
    filler = bar(open=100, high=100, low=100, close=100, volume=0, value=0)
    f = validate_bar(filler, ctx(is_trading_day=False))
    assert Rule.VENDOR_FILLER in {x.rule_code for x in f}
    assert Rule.NOT_TRADING_DAY not in {x.rule_code for x in f}
    traded = validate_bar(bar(), ctx(is_trading_day=False))
    assert Rule.NOT_TRADING_DAY in {x.rule_code for x in traded}
