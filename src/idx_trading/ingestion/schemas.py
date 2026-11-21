"""Explicit schemas for data crossing the system boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from idx_trading.instruments.service import normalize_ticker


@dataclass(frozen=True)
class FetchRequest:
    """What an adapter is asked to fetch. Adapters use the fields they need."""

    start_date: date | None = None
    end_date: date | None = None
    paths: tuple[str, ...] = ()
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RawPayload:
    """Bytes exactly as received from a source, plus retrieval metadata."""

    content: bytes
    content_type: str
    origin: str
    retrieved_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


class EodBar(BaseModel):
    """One normalized end-of-day bar in raw (unadjusted) prices.

    Only structural typing is enforced here; business-rule checks
    (OHLC consistency, extremes, ...) live in `validation.py` so that they are
    reported as data-quality findings rather than parse failures.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    trade_date: date
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    prev_close: Decimal | None = None
    volume: int | None = None
    value: Decimal | None = None
    frequency: int | None = None
    foreign_buy_volume: int | None = None
    foreign_sell_volume: int | None = None
    foreign_buy_value: Decimal | None = None
    foreign_sell_value: Decimal | None = None

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, v: str) -> str:
        return normalize_ticker(v)

    @property
    def key(self) -> str:
        return f"{self.ticker}|{self.trade_date.isoformat()}"

    def value_fields(self) -> dict[str, Any]:
        return self.model_dump(exclude={"ticker", "trade_date"})


@dataclass(frozen=True)
class ParseError:
    record_key: str
    message: str


@dataclass
class ParseResult:
    bars: list[EodBar] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
