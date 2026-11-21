"""Hand-computed checks of the adjustment formulas (ADR 0009)."""

from datetime import date
from decimal import Decimal as Dec

import pytest

from idx_trading.corporate_actions.adapters import parse_split_ratio
from idx_trading.corporate_actions.adjusted import _Factor, cumulative
from idx_trading.corporate_actions.factors import (
    cash_dividend_factor,
    rights_factor,
    share_count_factor,
)
from idx_trading.corporate_actions.schemas import family


def test_share_count_factor():
    assert share_count_factor(Dec(1), Dec(5)) == Dec("0.2")  # 1:5 split
    assert share_count_factor(Dec(10), Dec(1)) == Dec(10)  # 10:1 reverse split
    q = Dec("1e-12")  # storage precision
    assert share_count_factor(Dec(10), Dec(11)).quantize(q) == Dec("0.909090909091")  # 10% bonus
    with pytest.raises(ValueError):
        share_count_factor(Dec(0), Dec(1))


def test_cash_dividend_factor():
    # Rp 250 dividend, previous close Rp 10,000 -> prices before ex-date x 0.975
    assert cash_dividend_factor(Dec(250), Dec(10000)) == Dec("0.975")
    for bad in ((Dec(0), Dec(100)), (Dec(100), Dec(100)), (Dec(1), Dec(0))):
        with pytest.raises(ValueError):
            cash_dividend_factor(*bad)


def test_rights_factor_terp():
    # 4 old : 1 new right at Rp 800, close Rp 1,000 -> TERP = (4000 + 800) / 5 = 960
    assert rights_factor(Dec(4), Dec(1), Dec(800), Dec(1000)) == Dec("0.96")
    # Out of the money (exercise above close): no adjustment.
    assert rights_factor(Dec(4), Dec(1), Dec(1200), Dec(1000)) == Dec(1)


def test_parse_split_ratio_is_new_over_old():
    assert parse_split_ratio("5.000000/1.000000") == (Dec(1), Dec(5))
    assert parse_split_ratio("1.000000/10.000000") == (Dec(10), Dec(1))
    for bad in ("5", "a/b", "1/1", "0/1", "-2/1"):
        with pytest.raises(ValueError):
            parse_split_ratio(bad)


def test_cumulative_applies_only_to_bars_before_ex_date_and_respects_as_of():
    fs = [
        _Factor(date(2026, 3, 10), "split", Dec("0.2")),
        _Factor(date(2026, 6, 1), "total_return", Dec("0.975")),
    ]
    both = {"split", "total_return"}
    assert cumulative(fs, date(2026, 1, 5), both) == Dec("0.195")
    assert cumulative(fs, date(2026, 3, 9), both) == Dec("0.195")
    assert cumulative(fs, date(2026, 3, 10), both) == Dec("0.975")  # ex-date itself: not adjusted
    assert cumulative(fs, date(2026, 6, 1), both) == Dec(1)
    assert cumulative(fs, date(2026, 1, 5), {"split"}) == Dec("0.2")
    # Point in time: on 2026-04-01 the June dividend is not known yet.
    assert cumulative(fs, date(2026, 1, 5), both, upto=date(2026, 4, 1)) == Dec("0.2")


def test_family_groups_vendor_labels():
    assert family("stock_split") == family("bonus_shares") == family("reverse_split") == "share_count"
    assert family("cash_dividend") == "cash_dividend"
    assert family("rights_issue") == "rights"
