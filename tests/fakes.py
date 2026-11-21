"""A fake EODHD server for tests. Response shapes copied from the official docs."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping


class FakeEodhd:
    def __init__(self) -> None:
        self.bars: dict[str, list[dict]] = {}  # "BBCA.JK" -> rows (ascending)
        self.calls: list[str] = []
        self.status_override: dict[str, int] = {}  # path suffix -> status
        self.fail_first: int = 0  # transient 503s before succeeding
        self.symbols: list[dict] = []
        self.delisted: list[dict] = []
        self.dividends: dict[str, list[dict]] = {}  # "BBCA.JK" -> /div rows
        self.splits: dict[str, list[dict]] = {}  # "BBCA.JK" -> /splits rows

    def add(self, symbol: str, date: str, o, h, lo, c, v, adj=None) -> None:
        self.bars.setdefault(symbol, []).append(
            {
                "date": date,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "adjusted_close": adj if adj is not None else c,
                "volume": v,
            }
        )

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float):
        parsed = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append(parsed.path)
        assert headers["User-Agent"].startswith("idx-trading/")
        if q.get("api_token") != "test-token":
            return 401, {}, b"Unauthenticated"
        if self.fail_first:
            self.fail_first -= 1
            return 503, {"Retry-After": "1"}, b"busy"
        for suffix, status in self.status_override.items():
            if parsed.path.endswith(suffix):
                return status, {}, b"Ticker Not Found."
        ctype = {"Content-Type": "application/json; charset=utf-8"}
        if "/eod-bulk-last-day/" in parsed.path:
            ex = parsed.path.rsplit("/", 1)[-1]
            rows = [
                {"code": sym.split(".")[0], "exchange_short_name": ex, **r}
                for sym, rs in self.bars.items()
                if sym.endswith("." + ex)
                for r in rs
                if r["date"] == q["date"]
            ]
            return 200, ctype, json.dumps(rows).encode()
        if "/exchange-symbol-list/" in parsed.path:
            data = self.delisted if q.get("delisted") == "1" else self.symbols
            return 200, ctype, json.dumps(data).encode()
        for prefix, store in (("/div/", self.dividends), ("/splits/", self.splits)):
            if prefix in parsed.path:
                sym = parsed.path.rsplit("/", 1)[-1]
                rows = [
                    r for r in store.get(sym, []) if q.get("from", "0000") <= r["date"] <= q.get("to", "9999")
                ]
                return 200, ctype, json.dumps(rows).encode()
        if "/eod/" in parsed.path:
            sym = parsed.path.rsplit("/", 1)[-1]
            rows = [
                r for r in self.bars.get(sym, []) if q.get("from", "0000") <= r["date"] <= q.get("to", "9999")
            ]
            return 200, ctype, json.dumps(rows).encode()
        return 404, {}, b"not found"
