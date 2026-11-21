"""Data-quality rules for corporate actions (PRD §8.2 "corporate action inconsistencies")."""

from __future__ import annotations

from dataclasses import dataclass

from idx_trading.corporate_actions.schemas import (
    ADJUSTABLE_TYPES,
    SHARE_COUNT_TYPES,
    CorporateActionRecord,
)
from idx_trading.db.enums import CorporateActionType as T
from idx_trading.db.enums import Severity
from idx_trading.ingestion.validation import Finding


class CaRule:
    UNKNOWN_TICKER = "ca_unknown_ticker"
    RATIO_INVALID = "ca_ratio_invalid"
    CASH_INVALID = "ca_cash_invalid"
    CURRENCY_MISMATCH = "ca_currency_mismatch"
    RIGHTS_INVALID = "ca_rights_invalid"
    EX_DATE_NOT_SESSION = "ca_ex_date_not_session"
    NO_ADJUSTMENT_METHOD = "ca_no_adjustment_method"
    ADJUSTMENT_UNRESOLVED = "adjustment_unresolved"
    SPLIT_PRICE_INCONSISTENT = "corporate_action_inconsistency"
    REVISION_CONFLICT = "revision_conflict"


@dataclass(frozen=True)
class ActionContext:
    instrument_id: int | None
    instrument_currency: str | None
    is_trading_day: bool | None


def validate_action(a: CorporateActionRecord, ctx: ActionContext) -> list[Finding]:
    f: list[Finding] = []
    err, warn, info = Severity.ERROR, Severity.WARNING, Severity.INFO
    if ctx.instrument_id is None:
        msg = f"{a.ticker} not in instrument master on {a.ex_date}"
        f.append(Finding(CaRule.UNKNOWN_TICKER, err, msg))

    if a.action_type in SHARE_COUNT_TYPES or a.action_type == T.RIGHTS_ISSUE:
        old, new = a.ratio_old, a.ratio_new
        if old is None or new is None or old <= 0 or new <= 0:
            f.append(Finding(CaRule.RATIO_INVALID, err, "ratio_old and ratio_new must be positive"))
        elif a.action_type == T.REVERSE_SPLIT and not new < old:
            f.append(Finding(CaRule.RATIO_INVALID, err, f"reverse split needs new < old ({new}:{old})"))
        elif a.action_type in SHARE_COUNT_TYPES - {T.REVERSE_SPLIT} and not new > old:
            f.append(
                Finding(CaRule.RATIO_INVALID, err, f"{a.action_type.value} needs new > old ({new}:{old})")
            )

    if a.action_type == T.CASH_DIVIDEND and (a.cash_amount is None or a.cash_amount <= 0):
        f.append(Finding(CaRule.CASH_INVALID, err, "cash dividend needs a positive amount"))

    if a.action_type == T.RIGHTS_ISSUE and (a.exercise_price is None or a.exercise_price <= 0):
        f.append(Finding(CaRule.RIGHTS_INVALID, err, "rights issue needs a positive exercise price"))

    money = a.cash_amount is not None or a.exercise_price is not None
    if money and ctx.instrument_currency and a.currency != ctx.instrument_currency:
        f.append(
            Finding(
                CaRule.CURRENCY_MISMATCH,
                err,
                f"amount in {a.currency}, instrument trades in {ctx.instrument_currency}",
                {"currency": a.currency},
            )
        )

    if ctx.is_trading_day is False:
        f.append(Finding(CaRule.EX_DATE_NOT_SESSION, warn, f"ex-date {a.ex_date} is not an IDX session"))

    if a.action_type not in ADJUSTABLE_TYPES:
        f.append(
            Finding(CaRule.NO_ADJUSTMENT_METHOD, info, f"{a.action_type.value}: stored, no price adjustment")
        )
    return f
