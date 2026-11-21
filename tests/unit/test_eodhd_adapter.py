from datetime import date
from decimal import Decimal

import pytest

from idx_trading.errors import QuotaExceededError, SourceHttpError
from idx_trading.ingestion.adapters.eodhd import (
    CallBudget,
    EodhdEodAdapter,
    TickerWindow,
    fetch_symbol_list,
)
from idx_trading.ingestion.http import HttpClient
from idx_trading.ingestion.schemas import FetchRequest
from tests.fakes import FakeEodhd


@pytest.fixture
def fake():
    f = FakeEodhd()
    f.add("BBCA.JK", "2026-11-19", 9800, 9900, 9750, 9850, 80_000_000, adj=9700.5)
    f.add("BBCA.JK", "2026-11-20", 9850, 9900, 9800, 9875, 85_000_000)
    f.add("TLKM.JK", "2026-11-20", 3150, 3200, 3120, 3180, 120_000_000)
    return f


def adapter(fake, limit=20, token="test-token"):
    client = HttpClient(transport=fake, sleep=lambda _s: None)
    return EodhdEodAdapter(token, client, CallBudget(limit), base_url="https://eodhd.test/api")


def parse_all(a, req):
    payloads = list(a.fetch(req))
    results = [a.parse(p) for p in payloads]
    return payloads, [b for r in results for b in r.bars], [e for r in results for e in r.errors]


def test_per_ticker_fetch_and_parse(fake):
    a = adapter(fake)
    req = FetchRequest(
        options={
            "windows": [
                TickerWindow("BBCA", date(2026, 11, 1), date(2026, 11, 20)),
                TickerWindow("tlkm", None, date(2026, 11, 20)),
            ]
        }
    )
    payloads, bars, errors = parse_all(a, req)
    assert not errors and len(bars) == 3 and a.budget.used == 2
    bbca = [b for b in bars if b.ticker == "BBCA"]
    assert bbca[0].close == Decimal("9850") and bbca[0].volume == 80_000_000
    assert fake.calls == ["/api/eod/BBCA.JK", "/api/eod/TLKM.JK"]
    for p in payloads:
        assert "test-token" not in p.origin and "api_token=***" in p.origin
        assert p.metadata["volume_basis"] == "split_adjusted_at_retrieval"
        assert b"adjusted_close" in p.content  # kept in the raw archive


def test_window_is_sent_as_from_to(fake):
    a = adapter(fake)
    _, bars, _ = parse_all(
        a, FetchRequest(options={"windows": [TickerWindow("BBCA", date(2026, 11, 20), date(2026, 11, 20))]})
    )
    assert [b.trade_date for b in bars] == [date(2026, 11, 20)]


def test_budget_is_enforced_before_requests(fake):
    a = adapter(fake, limit=1)
    gen = a.fetch(FetchRequest(options={"tickers": ["BBCA", "TLKM"]}))
    next(gen)
    with pytest.raises(QuotaExceededError, match="only 0 of 1"):
        next(gen)
    assert len(fake.calls) == 1


def test_bulk_mode(fake):
    a = adapter(fake, limit=100)
    _, bars, errors = parse_all(a, FetchRequest(end_date=date(2026, 11, 20), options={"mode": "bulk"}))
    assert not errors and {b.ticker for b in bars} == {"BBCA", "TLKM"} and a.budget.used == 100


def test_bulk_refused_on_free_budget(fake):
    with pytest.raises(QuotaExceededError):
        list(
            adapter(fake, limit=20).fetch(FetchRequest(end_date=date(2026, 11, 20), options={"mode": "bulk"}))
        )
    assert fake.calls == []


def test_unknown_ticker_is_a_parse_error_not_a_crash(fake):
    fake.status_override["ZZZZ.JK"] = 404
    _, bars, errors = parse_all(adapter(fake), FetchRequest(options={"tickers": ["ZZZZ", "TLKM"]}))
    assert len(bars) == 1 and "HTTP 404" in errors[0].message


def test_auth_failure_is_fatal(fake):
    with pytest.raises(SourceHttpError, match="401") as exc:
        list(adapter(fake, token="wrong").fetch(FetchRequest(options={"tickers": ["BBCA"]})))
    assert "wrong" not in str(exc.value)


def test_schema_violations_are_reported(fake):
    fake.bars["BBCA.JK"] = [{"date": "2026-13-45", "close": 1}]
    fake.bars["TLKM.JK"] = [{"date": "2026-11-20", "close": 1, "volume": 1.5}]
    _, bars, errors = parse_all(adapter(fake), FetchRequest(options={"tickers": ["BBCA", "TLKM"]}))
    assert not bars and len(errors) == 2
    assert "schema" in errors[0].message and "fractional volume" in errors[1].message


def test_transient_errors_are_retried(fake):
    fake.fail_first = 2
    _, bars, _ = parse_all(adapter(fake), FetchRequest(options={"tickers": ["TLKM"]}))
    assert len(bars) == 1 and len(fake.calls) == 3


def test_symbol_list(fake):
    fake.symbols = [
        {
            "Code": "BBCA",
            "Name": "Bank Central Asia Tbk",
            "Country": "Indonesia",
            "Exchange": "JK",
            "Currency": "IDR",
            "Type": "Common Stock",
            "Isin": "ID1000109507",
        }
    ]
    client = HttpClient(transport=fake, sleep=lambda _s: None)
    out = fetch_symbol_list("test-token", client, CallBudget(5), base_url="https://eodhd.test/api")
    assert out[0].Code == "BBCA" and out[0].Isin == "ID1000109507"


def test_requires_token(fake):
    with pytest.raises(ValueError, match="token"):
        adapter(fake, token="")
