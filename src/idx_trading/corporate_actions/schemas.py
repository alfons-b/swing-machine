"""Explicit schema for corporate actions crossing the system boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from idx_trading.db.enums import CorporateActionType
from idx_trading.ingestion.schemas import ParseError
from idx_trading.instruments.service import normalize_ticker

#: Events that change the share count; adjusted with factor old/new.
SHARE_COUNT_TYPES = frozenset(
    {
        CorporateActionType.STOCK_SPLIT,
        CorporateActionType.REVERSE_SPLIT,
        CorporateActionType.BONUS_SHARES,
        CorporateActionType.STOCK_DIVIDEND,
    }
)
#: Events with an implemented price-adjustment method.
ADJUSTABLE_TYPES = SHARE_COUNT_TYPES | {
    CorporateActionType.CASH_DIVIDEND,
    CorporateActionType.RIGHTS_ISSUE,
}


def family(action_type: str) -> str:
    """Events of one family on one ex-date are the same economic event.

    Sources label them differently (EODHD reports IDX bonus shares as a split),
    so de-duplication across sources works per family, not per exact type.
    """
    t = CorporateActionType(action_type)
    if t in SHARE_COUNT_TYPES:
        return "share_count"
    if t == CorporateActionType.CASH_DIVIDEND:
        return "cash_dividend"
    if t == CorporateActionType.RIGHTS_ISSUE:
        return "rights"
    return t.value


class CorporateActionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    action_type: CorporateActionType
    ex_date: date
    announcement_date: date | None = None
    record_date: date | None = None
    payment_date: date | None = None
    ratio_old: Decimal | None = None
    ratio_new: Decimal | None = None
    cash_amount: Decimal | None = None
    exercise_price: Decimal | None = None
    currency: str = "IDR"
    #: "reported" (default) or "cancelled": a manual "cancelled" row overrides a vendor
    #: row of the same family and ex-date, e.g. a superseded dividend schedule.
    status: str = "reported"
    notes: str | None = None

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"reported", "cancelled"}:
            raise ValueError("status must be 'reported' or 'cancelled'")
        return v

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, v: str) -> str:
        return normalize_ticker(v)

    @field_validator("currency")
    @classmethod
    def _ccy(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError(f"invalid currency {v!r}")
        return v

    @property
    def key(self) -> str:
        return f"{self.ticker}|{self.action_type.value}|{self.ex_date.isoformat()}"

    def value_fields(self) -> dict[str, Any]:
        d = self.model_dump(exclude={"ticker", "action_type", "ex_date"})
        return d


@dataclass
class ActionParseResult:
    actions: list[CorporateActionRecord] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
