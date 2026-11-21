"""Adjustment factors (ADR 0009).

A factor `f` belongs to one action with ex-date `E` and multiplies every price
on sessions strictly *before* `E`. Cumulative adjustment of a bar on date `t`
as of date `A` is the product of all current factors with  t < E <= A.

Formulas (all inputs in the *pre-event* share basis, raw prices):
  * share-count events (split, reverse split, bonus, stock dividend):
        f = old / new                    (1 share -> 5 shares: f = 0.2)
  * cash dividend D, reference close C = raw close of the last session before E:
        f = 1 - D / C                    (requires 0 < D < C)
  * rights issue, `new` rights per `old` shares at exercise price S:
        TERP = (C*old + S*new) / (old + new);   f = min(1, TERP / C)
        (out-of-the-money rights, S >= C, are not adjusted)

Share-count factors form the split-adjusted series; cash dividends and rights
are added on top for the total-return series. Volume is adjusted by share-count
factors only, inversely (volume / f).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from idx_trading.calendar import CalendarService
from idx_trading.corporate_actions.schemas import ADJUSTABLE_TYPES, SHARE_COUNT_TYPES, family
from idx_trading.corporate_actions.validation import CaRule
from idx_trading.db.base import utcnow
from idx_trading.db.enums import AdjustmentType, CorporateActionType, Severity, ValidationStatus
from idx_trading.db.models import CorporateAction, DailyPrice, DataQualityIssue, PriceAdjustmentFactor

METHOD_VERSION = "v1"
#: Highest priority first. Manual entries come from the owner reading official
#: IDX/KSEI announcements and override vendor data for the same event.
ACTION_SOURCE_PRIORITY = ("manual_csv", "eodhd")
PRICE_SOURCE_PRIORITY = ("manual_csv", "eodhd")
_Q = Decimal("1e-12")


# ------------------------------------------------------------------ formulas
def share_count_factor(ratio_old: Decimal, ratio_new: Decimal) -> Decimal:
    if ratio_old <= 0 or ratio_new <= 0:
        raise ValueError("ratios must be positive")
    with localcontext() as ctx:
        ctx.prec = 34
        return ratio_old / ratio_new


def cash_dividend_factor(dividend: Decimal, ref_close: Decimal) -> Decimal:
    if ref_close <= 0:
        raise ValueError("reference close must be positive")
    if dividend <= 0:
        raise ValueError("dividend must be positive")
    if dividend >= ref_close:
        raise ValueError(f"dividend {dividend} >= reference close {ref_close}")
    with localcontext() as ctx:
        ctx.prec = 34
        return 1 - dividend / ref_close


def rights_factor(
    ratio_old: Decimal, ratio_new: Decimal, exercise_price: Decimal, ref_close: Decimal
) -> Decimal:
    if min(ratio_old, ratio_new, exercise_price, ref_close) <= 0:
        raise ValueError("rights inputs must be positive")
    with localcontext() as ctx:
        ctx.prec = 34
        terp = (ref_close * ratio_old + exercise_price * ratio_new) / (ratio_old + ratio_new)
        return min(Decimal(1), terp / ref_close)


def adjustment_type_of(action_type: str) -> AdjustmentType:
    return (
        AdjustmentType.SPLIT
        if CorporateActionType(action_type) in SHARE_COUNT_TYPES
        else AdjustmentType.TOTAL_RETURN
    )


# ------------------------------------------------------------------- service
@dataclass
class FactorReport:
    inserted: int = 0
    unchanged: int = 0
    superseded: int = 0
    retired: int = 0
    unresolved: list[str] = field(default_factory=list)
    skipped_duplicates: int = 0


def choose_actions(actions: list[CorporateAction]) -> tuple[list[CorporateAction], int]:
    """One action per (instrument, family, ex_date): the highest-priority source wins."""
    rank = {sid: i for i, sid in enumerate(ACTION_SOURCE_PRIORITY)}
    best: dict[tuple[int, str, date], CorporateAction] = {}
    dropped = 0
    for a in actions:
        assert a.ex_date is not None
        k = (a.instrument_id, family(a.action_type), a.ex_date)
        cur = best.get(k)
        if cur is None:
            best[k] = a
            continue
        dropped += 1
        if rank.get(a.source_id or "", 99) < rank.get(cur.source_id or "", 99):
            best[k] = a
    return sorted(best.values(), key=lambda a: (a.instrument_id, a.ex_date, a.action_type)), dropped


def reference_close(
    session: Session, instrument_id: int, ex_date: date, calendar: CalendarService
) -> tuple[date, Decimal, str] | None:
    """Raw close on the last session before `ex_date`, from the best available source."""
    prev = calendar.previous_session(ex_date)
    if prev is None:
        return None
    rows = session.scalars(
        select(DailyPrice).where(
            DailyPrice.instrument_id == instrument_id,
            DailyPrice.trade_date == prev,
            DailyPrice.is_current.is_(True),
            DailyPrice.validation_status != ValidationStatus.INVALID.value,
            DailyPrice.close.is_not(None),
        )
    ).all()
    rank = {sid: i for i, sid in enumerate(PRICE_SOURCE_PRIORITY)}
    rows = sorted(rows, key=lambda r: rank.get(r.source_id, 99))
    if not rows:
        return None
    r = rows[0]
    assert r.close is not None
    return prev, r.close, r.source_id


class FactorService:
    def __init__(self, session: Session, calendar_code: str = "XIDX") -> None:
        self.s = session
        self.calendar = CalendarService(session, calendar_code)

    def _compute(self, a: CorporateAction) -> tuple[Decimal, str, dict[str, Any]]:
        t = CorporateActionType(a.action_type)
        base: dict[str, Any] = {
            "action_id": a.action_id,
            "source_id": a.source_id,
            "action_version": a.version,
            "action_type": t.value,
        }
        if t in SHARE_COUNT_TYPES:
            assert a.ratio_old is not None and a.ratio_new is not None
            f = share_count_factor(a.ratio_old, a.ratio_new)
            return (
                f,
                f"share_count:old/new:{METHOD_VERSION}",
                {**base, "ratio_old": str(a.ratio_old), "ratio_new": str(a.ratio_new)},
            )
        assert a.ex_date is not None
        ref = reference_close(self.s, a.instrument_id, a.ex_date, self.calendar)
        if ref is None:
            raise ValueError(f"no raw close on the session before {a.ex_date}")
        ref_date, close, price_source = ref
        inputs = {
            **base,
            "ref_date": ref_date.isoformat(),
            "ref_close": str(close),
            "ref_price_source": price_source,
        }
        if t == CorporateActionType.CASH_DIVIDEND:
            assert a.cash_amount is not None
            f = cash_dividend_factor(a.cash_amount, close)
            return (
                f,
                f"cash_dividend:1-D/C_prev_close:{METHOD_VERSION}",
                {**inputs, "dividend": str(a.cash_amount)},
            )
        if t == CorporateActionType.RIGHTS_ISSUE:
            assert a.ratio_old and a.ratio_new and a.exercise_price
            f = rights_factor(a.ratio_old, a.ratio_new, a.exercise_price, close)
            return (
                f,
                f"rights:TERP/C_capped:{METHOD_VERSION}",
                {
                    **inputs,
                    "ratio_old": str(a.ratio_old),
                    "ratio_new": str(a.ratio_new),
                    "exercise_price": str(a.exercise_price),
                },
            )
        raise ValueError(f"no adjustment method for {t.value}")  # pragma: no cover

    def recompute(self, instrument_ids: list[int] | None = None) -> FactorReport:
        """Bring `price_adjustment_factors` in line with the current corporate actions.

        Idempotent: unchanged factors are left alone; changed inputs/method create a
        new version; factors whose action is no longer chosen are retired (kept,
        `is_current=false`). Failures are recorded as `adjustment_unresolved` issues
        so that downstream stages can hold affected predictions (PRD §8.3).
        """
        report = FactorReport()
        q = select(CorporateAction).where(
            CorporateAction.is_current.is_(True),
            CorporateAction.validation_status != ValidationStatus.INVALID.value,
            CorporateAction.ex_date.is_not(None),
            CorporateAction.action_type.in_([t.value for t in ADJUSTABLE_TYPES]),
        )
        if instrument_ids is not None:
            q = q.where(CorporateAction.instrument_id.in_(instrument_ids))
        chosen, report.skipped_duplicates = choose_actions(list(self.s.scalars(q)))
        chosen_ids = {a.action_id for a in chosen}

        fq = select(PriceAdjustmentFactor).where(PriceAdjustmentFactor.is_current.is_(True))
        if instrument_ids is not None:
            fq = fq.where(PriceAdjustmentFactor.instrument_id.in_(instrument_ids))
        current = {(f.action_id, f.adjustment_type): f for f in self.s.scalars(fq)}
        for (action_id, _t), f in current.items():
            if action_id not in chosen_ids:
                f.is_current = False
                report.retired += 1

        now = utcnow()
        for a in chosen:
            atype = adjustment_type_of(a.action_type)
            try:
                value, method, inputs = self._compute(a)
            except (ValueError, ArithmeticError) as exc:
                report.unresolved.append(f"action {a.action_id} ({a.action_type} {a.ex_date}): {exc}")
                already_open = self.s.scalar(
                    select(DataQualityIssue.issue_id)
                    .where(
                        DataQualityIssue.rule_code == CaRule.ADJUSTMENT_UNRESOLVED,
                        DataQualityIssue.instrument_id == a.instrument_id,
                        DataQualityIssue.trade_date == a.ex_date,
                        DataQualityIssue.status == "open",
                    )
                    .limit(1)
                )
                if already_open is None:
                    self.s.add(
                        DataQualityIssue(
                            source_id=a.source_id,
                            dataset="corporate_actions",
                            instrument_id=a.instrument_id,
                            trade_date=a.ex_date,
                            rule_code=CaRule.ADJUSTMENT_UNRESOLVED,
                            severity=Severity.ERROR.value,
                            message=f"cannot compute adjustment factor: {exc}",
                            details={"action_id": a.action_id},
                        )
                    )
                old = current.get((a.action_id, atype.value))
                if old is not None and old.is_current:
                    old.is_current = False  # never keep a factor whose inputs are now unknown
                    report.retired += 1
                continue
            value = value.quantize(_Q)
            old = current.get((a.action_id, atype.value))
            if old is not None and old.factor == value and old.method == method:
                report.unchanged += 1
                continue
            version = 1
            if old is not None:
                old.is_current = False
                self.s.flush()
                version = old.factor_version + 1
                report.superseded += 1
            else:
                prior = self.s.scalar(
                    select(PriceAdjustmentFactor.factor_version)
                    .where(
                        PriceAdjustmentFactor.action_id == a.action_id,
                        PriceAdjustmentFactor.adjustment_type == atype.value,
                    )
                    .order_by(PriceAdjustmentFactor.factor_version.desc())
                    .limit(1)
                )
                version = (prior or 0) + 1
                report.inserted += 1
            assert a.ex_date is not None
            self.s.add(
                PriceAdjustmentFactor(
                    instrument_id=a.instrument_id,
                    ex_date=a.ex_date,
                    adjustment_type=atype.value,
                    factor=value,
                    action_id=a.action_id,
                    factor_version=version,
                    is_current=True,
                    method=method,
                    inputs=inputs,
                    computed_at=now,
                )
            )
        self.s.flush()
        return report
