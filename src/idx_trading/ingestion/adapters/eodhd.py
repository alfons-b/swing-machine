"""EODHD end-of-day adapter (registry id `eodhd`, ADR 0008).

Endpoints used (verified against the official docs on 2026-11-21):
  * ``GET /eod/{TICKER}.{EX}?from&to&period=d&order=a&fmt=json`` — 1 API call per ticker,
    any date window. Available on the Free plan (past year only).
  * ``GET /eod-bulk-last-day/{EX}?date&fmt=json`` — whole exchange for one date,
    flat 100 API calls. **Paid plans only.**
  * ``GET /exchange-symbol-list/{EX}?delisted=0|1&fmt=json`` — ticker lists.

Data semantics (from the docs): open/high/low/close are raw as traded;
``adjusted_close`` is split+dividend adjusted; ``volume`` is *split-adjusted*.
``adjusted_close`` is not stored in `daily_prices` (raw table) but stays in the
raw archive; the volume basis is recorded on every payload (OI-17).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, field_validator

from idx_trading.db.enums import PermissionPurpose
from idx_trading.errors import QuotaExceededError, SourceHttpError
from idx_trading.ingestion.adapters.base import SourceAdapter
from idx_trading.ingestion.http import HttpClient
from idx_trading.ingestion.schemas import EodBar, FetchRequest, ParseError, ParseResult, RawPayload
from idx_trading.instruments.service import normalize_ticker

SOURCE_ID = "eodhd"
COST_EOD = 1
COST_BULK = 100
VOLUME_BASIS = "split_adjusted"
#: Statuses that mean "you are not allowed": never retried, never worked around.
_FATAL = {401, 402, 403}


class CallBudget:
    """Hard cap on API calls a run may spend. Checked *before* each request."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("budget must be >= 1")
        self.limit = limit
        self.used = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def spend(self, cost: int, what: str) -> None:
        if cost > self.remaining:
            raise QuotaExceededError(
                f"{what} costs {cost} API calls but only {self.remaining} of {self.limit} remain"
            )
        self.used += cost


def _num(v: object) -> object:
    # EODHD sends JSON numbers; treat explicit nulls/blank strings as missing.
    if v is None or (isinstance(v, str) and v.strip() in {"", "null", "NA"}):
        return None
    return v


class EodhdRow(BaseModel):
    """One row of `/eod` or `/eod-bulk-last-day` (explicit schema)."""

    model_config = ConfigDict(extra="allow")  # tolerate new vendor fields; archived anyway

    date: date
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    adjusted_close: Decimal | None = None
    volume: Decimal | None = None
    code: str | None = None  # bulk only
    exchange_short_name: str | None = None  # bulk only
    prev_close: Decimal | None = None  # bulk with `symbols` only

    _n = field_validator(
        "open", "high", "low", "close", "adjusted_close", "volume", "prev_close", mode="before"
    )(_num)


class EodhdSymbol(BaseModel):
    """One row of `/exchange-symbol-list` (explicit schema)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    Code: str
    Name: str | None = None
    Country: str | None = None
    Exchange: str | None = None
    Currency: str | None = None
    Type: str | None = None
    Isin: str | None = None


_ROWS = TypeAdapter(list[EodhdRow])
_SYMBOLS = TypeAdapter(list[EodhdSymbol])


def to_vendor_symbol(ticker: str, exchange: str) -> str:
    return f"{normalize_ticker(ticker)}.{exchange}"


@dataclass(frozen=True)
class TickerWindow:
    ticker: str
    start: date | None
    end: date | None


class EodhdEodAdapter(SourceAdapter):
    source_id = SOURCE_ID
    dataset = "eod_prices"
    purpose = PermissionPurpose.AUTOMATED_INGESTION

    def __init__(
        self,
        token: str,
        client: HttpClient,
        budget: CallBudget,
        *,
        exchange: str = "JK",
        base_url: str = "https://eodhd.com/api",
    ) -> None:
        if not token:
            raise ValueError("EODHD API token is required (IDX_EODHD_API_TOKEN)")
        self._token = token
        self.client = client
        self.budget = budget
        self.exchange = exchange.upper()
        self.base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------ fetch
    def fetch(self, request: FetchRequest) -> Iterable[RawPayload]:
        """`options.mode` = "per_ticker" (default) or "bulk".

        per_ticker: `options.windows` = list[TickerWindow], or `options.tickers`
        with `request.start_date`/`end_date` applied to all of them.
        bulk: one date (`request.end_date`), whole exchange.
        """
        mode = request.options.get("mode", "per_ticker")
        if mode == "bulk":
            return self._fetch_bulk(request)
        if mode == "per_ticker":
            return self._fetch_per_ticker(request)
        raise ValueError(f"unknown mode {mode!r}")

    def _get(self, path: str, params: dict[str, str]) -> tuple[int, bytes, str, str]:
        resp = self.client.get(f"{self.base_url}{path}", {**params, "api_token": self._token})
        if resp.status in _FATAL:
            raise SourceHttpError(resp.status, resp.url, "not authorised for this data on the current plan")
        return resp.status, resp.body, resp.content_type or "application/json", resp.url

    def _payload(self, status: int, body: bytes, ctype: str, url: str, **meta: object) -> RawPayload:
        return RawPayload(
            content=body,
            content_type="application/json" if "json" in ctype or not ctype else ctype,
            origin=url,  # already redacted by the HTTP client
            retrieved_at=datetime.now(tz=UTC),
            metadata={
                "http_status": status,
                "exchange": self.exchange,
                "volume_basis": VOLUME_BASIS,
                **meta,
            },
        )

    def _fetch_per_ticker(self, request: FetchRequest) -> Iterator[RawPayload]:
        windows: list[TickerWindow] = list(request.options.get("windows") or [])
        if not windows:
            windows = [
                TickerWindow(t, request.start_date, request.end_date)
                for t in request.options.get("tickers", [])
            ]
        if not windows:
            raise ValueError("per_ticker mode needs `windows` or `tickers`")
        for w in windows:
            symbol = to_vendor_symbol(w.ticker, self.exchange)
            self.budget.spend(COST_EOD, f"EOD {symbol}")
            params = {"fmt": "json", "period": "d", "order": "a"}
            if w.start:
                params["from"] = w.start.isoformat()
            if w.end:
                params["to"] = w.end.isoformat()
            status, body, ctype, url = self._get(f"/eod/{symbol}", params)
            yield self._payload(
                status, body, ctype, url, mode="per_ticker", ticker=normalize_ticker(w.ticker)
            )

    def _fetch_bulk(self, request: FetchRequest) -> Iterator[RawPayload]:
        if request.end_date is None:
            raise ValueError("bulk mode needs end_date (the session date)")
        self.budget.spend(COST_BULK, f"bulk {self.exchange} {request.end_date}")
        status, body, ctype, url = self._get(
            f"/eod-bulk-last-day/{self.exchange}",
            {"fmt": "json", "date": request.end_date.isoformat()},
        )
        yield self._payload(status, body, ctype, url, mode="bulk", date=request.end_date.isoformat())

    # ------------------------------------------------------------------ parse
    def parse(self, payload: RawPayload) -> ParseResult:
        result = ParseResult()
        meta = payload.metadata
        label = str(meta.get("ticker") or f"bulk:{meta.get('date')}")
        status = int(meta.get("http_status", 200))
        if status != 200:
            snippet = payload.content[:200].decode("utf-8", "replace").strip()
            result.errors.append(ParseError(label, f"HTTP {status} from source: {snippet}"))
            return result
        try:
            rows = _ROWS.validate_python(json.loads(payload.content or b"[]"))
        except (json.JSONDecodeError, ValidationError) as exc:
            result.errors.append(
                ParseError(label, f"response does not match EODHD EOD schema: {str(exc)[:300]}")
            )
            return result

        for i, row in enumerate(rows):
            key = f"{label}#{i}"
            try:
                if meta.get("mode") == "bulk":
                    if row.exchange_short_name and row.exchange_short_name.upper() != self.exchange:
                        raise ValueError(f"row for exchange {row.exchange_short_name}")
                    if not row.code:
                        raise ValueError("bulk row without code")
                    ticker = row.code
                else:
                    ticker = str(meta["ticker"])
                volume: int | None = None
                if row.volume is not None:
                    if row.volume != row.volume.to_integral_value():
                        raise ValueError(f"fractional volume {row.volume}")
                    volume = int(row.volume)
                result.bars.append(
                    EodBar(
                        ticker=ticker,
                        trade_date=row.date,
                        open=row.open,
                        high=row.high,
                        low=row.low,
                        close=row.close,
                        prev_close=row.prev_close,
                        volume=volume,
                    )
                )
            except (ValueError, ValidationError) as exc:
                result.errors.append(ParseError(key, str(exc).splitlines()[0]))
        return result


def fetch_symbol_list(
    token: str,
    client: HttpClient,
    budget: CallBudget,
    *,
    exchange: str = "JK",
    delisted: bool = False,
    base_url: str = "https://eodhd.com/api",
) -> list[EodhdSymbol]:
    """Ticker list for an exchange (active, or delisted with `delisted=True`)."""
    budget.spend(COST_EOD, f"symbol list {exchange}")
    resp = client.get(
        f"{base_url.rstrip('/')}/exchange-symbol-list/{exchange.upper()}",
        {"fmt": "json", "delisted": "1" if delisted else "0", "api_token": token},
    )
    if resp.status != 200:
        raise SourceHttpError(resp.status, resp.url)
    try:
        return _SYMBOLS.validate_python(json.loads(resp.body))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise SourceHttpError(200, resp.url, f"unexpected symbol-list schema: {exc}") from exc
