"""Financial ratios as of a date (FR-007), from point-in-time facts only.

Definitions (consolidated basis):
  EPS_TTM   trailing-12-month reported EPS, restated for share-count events after period end
  PER       raw close on as_of / EPS_TTM                      (None if EPS_TTM <= 0)
  shares    net_income_parent_YTD / eps_basic_YTD  -> *implied*, EPS is printed rounded
  BVPS      total_equity / shares                  (total equity incl. NCI: the bank
            publication report does not split out parent equity)
  PBV       raw close / BVPS
  ROE       NI_parent_TTM / avg(total_equity at P, at P-1y)
  ROA       NI_TTM / avg(total_assets at P, at P-1y)
  DER       total_liabilities / total_equity at P
  LDR       loans / (current + savings + time deposits) at P
  NI growth NI_parent_YTD(P) / NI_parent_YTD(P-1y) - 1
A ratio whose inputs are missing is None and the reason is listed in `notes`
(graceful degradation, PRD §8.3) — it is never estimated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from idx_trading.corporate_actions.adjusted import Adjustment, adjusted_bars
from idx_trading.db.enums import AdjustmentType
from idx_trading.db.models import Instrument, PriceAdjustmentFactor
from idx_trading.fundamentals.pit import fact_as_of, latest_period, same_day_last_year, ttm


@dataclass
class Ratios:
    instrument_id: int
    as_of: date
    period_end: date | None = None
    price: Decimal | None = None
    price_date: date | None = None
    eps_ttm: Decimal | None = None
    per: Decimal | None = None
    shares_implied: Decimal | None = None
    shares_rel_uncertainty: Decimal | None = None
    bvps: Decimal | None = None
    pbv: Decimal | None = None
    roe: Decimal | None = None
    roa: Decimal | None = None
    der: Decimal | None = None
    ldr: Decimal | None = None
    ni_growth_yoy: Decimal | None = None
    notes: list[str] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        keys = (
            "period_end",
            "price",
            "price_date",
            "eps_ttm",
            "per",
            "shares_implied",
            "bvps",
            "pbv",
            "roe",
            "roa",
            "der",
            "ldr",
            "ni_growth_yoy",
        )
        return {k: getattr(self, k) for k in keys}


def _split_factor_between(s: Session, instrument_id: int, after: date, upto: date) -> Decimal:
    out = Decimal(1)
    for f in s.scalars(
        select(PriceAdjustmentFactor).where(
            PriceAdjustmentFactor.instrument_id == instrument_id,
            PriceAdjustmentFactor.is_current.is_(True),
            PriceAdjustmentFactor.adjustment_type == AdjustmentType.SPLIT.value,
            PriceAdjustmentFactor.ex_date > after,
            PriceAdjustmentFactor.ex_date <= upto,
        )
    ):
        out *= f.factor
    return out


def compute_ratios(s: Session, instrument_id: int, as_of: date) -> Ratios:
    inst = s.get(Instrument, instrument_id)
    if inst is None:
        raise LookupError(f"instrument {instrument_id} not found")
    r = Ratios(instrument_id, as_of)
    issuer = inst.issuer_id
    p = latest_period(s, issuer, as_of)
    if p is None:
        r.notes.append("no financial report available on this date")
        return r
    r.period_end = p
    p_ly = same_day_last_year(p)

    def inst_val(concept: str, end: date) -> Decimal | None:
        f = fact_as_of(s, issuer, concept, end, as_of)
        if f is not None:
            r.inputs[f"{concept}@{end}"] = {"value": str(f.value), "report_id": f.report_id}
        return f.value if f else None

    with localcontext() as ctx:
        ctx.prec = 34
        bars = adjusted_bars(s, instrument_id, Adjustment.RAW, end=as_of)
        if bars and bars[-1].close is not None:
            r.price, r.price_date = bars[-1].close, bars[-1].trade_date
        else:
            r.notes.append("no price on or before as_of")

        split = _split_factor_between(s, instrument_id, p, as_of)
        if split != 1:
            r.notes.append(f"EPS/shares restated for share-count events after {p} (factor {split})")

        eps = ttm(s, issuer, "eps_basic", p, as_of)
        if eps is None:
            r.notes.append("EPS TTM unavailable (needs FY and prior-year YTD reports)")
        else:
            r.eps_ttm = eps.value * split
            r.inputs["eps_ttm"] = {"method": eps.method, "reports": [f.report_id for f in eps.facts]}
            if r.price is not None:
                if r.eps_ttm > 0:
                    r.per = r.price / r.eps_ttm
                else:
                    r.notes.append("PER undefined: non-positive EPS TTM")

        ni_ytd, eps_ytd = inst_val("net_income_parent", p), inst_val("eps_basic", p)
        equity, assets = inst_val("total_equity", p), inst_val("total_assets", p)
        if ni_ytd and eps_ytd and eps_ytd > 0 and ni_ytd > 0:
            r.shares_implied = ni_ytd / eps_ytd / split
            r.shares_rel_uncertainty = Decimal("0.5") / eps_ytd  # EPS printed to Rp 1
            if equity is not None:
                r.bvps = equity / r.shares_implied
                if r.price is not None and r.bvps > 0:
                    r.pbv = r.price / r.bvps
        else:
            r.notes.append("shares outstanding not derivable (needs positive YTD profit and EPS)")

        ni_parent_ttm = ttm(s, issuer, "net_income_parent", p, as_of)
        ni_ttm = ttm(s, issuer, "net_income", p, as_of)
        eq_ly, as_ly = inst_val("total_equity", p_ly), inst_val("total_assets", p_ly)
        if ni_parent_ttm and equity is not None:
            avg = (equity + eq_ly) / 2 if eq_ly is not None else equity
            if eq_ly is None:
                r.notes.append("ROE uses period-end equity (no equity a year earlier)")
            r.roe = ni_parent_ttm.value / avg
        if ni_ttm and assets is not None:
            avg_a = (assets + as_ly) / 2 if as_ly is not None else assets
            if as_ly is None:
                r.notes.append("ROA uses period-end assets (no assets a year earlier)")
            r.roa = ni_ttm.value / avg_a

        liab = inst_val("total_liabilities", p)
        if liab is not None and equity:
            r.der = liab / equity
        loans = inst_val("loans", p)
        deps = [inst_val(c, p) for c in ("deposits_current", "deposits_savings", "deposits_time")]
        if loans is not None and all(d is not None for d in deps):
            total = sum(d for d in deps if d is not None)
            if total:
                r.ldr = loans / total
        ni_prev = inst_val("net_income_parent", p_ly)
        if ni_ytd is not None and ni_prev:
            r.ni_growth_yoy = ni_ytd / ni_prev - 1
    return r
