"""Canonical financial concepts. Every fact stored maps to exactly one of these."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    INSTANT = "instant"  # balance sheet: value at period_end
    DURATION = "duration"  # income statement: year-to-date from period_start


@dataclass(frozen=True)
class Concept:
    name: str
    kind: Kind
    unit: str
    description: str


CONCEPTS: dict[str, Concept] = {
    c.name: c
    for c in (
        Concept("total_assets", Kind.INSTANT, "IDR", "Total assets"),
        Concept("current_assets", Kind.INSTANT, "IDR", "Total current assets"),
        Concept("current_liabilities", Kind.INSTANT, "IDR", "Total current liabilities"),
        Concept("cash", Kind.INSTANT, "IDR", "Cash and cash equivalents"),
        Concept("equity_parent", Kind.INSTANT, "IDR", "Equity attributable to owners of the parent"),
        Concept("equity_nci", Kind.INSTANT, "IDR", "Non-controlling interests in equity"),
        Concept("shares_issued", Kind.INSTANT, "shares", "Issued and fully paid ordinary shares (caption)"),
        Concept("revenue", Kind.DURATION, "IDR", "Revenue / net sales"),
        Concept("gross_profit", Kind.DURATION, "IDR", "Gross profit"),
        Concept("loans", Kind.INSTANT, "IDR", "Loans and financing extended (gross of allowance)"),
        Concept("deposits_current", Kind.INSTANT, "IDR", "Current accounts (giro)"),
        Concept("deposits_savings", Kind.INSTANT, "IDR", "Savings accounts (tabungan)"),
        Concept("deposits_time", Kind.INSTANT, "IDR", "Time deposits (deposito)"),
        Concept("total_liabilities", Kind.INSTANT, "IDR", "Total liabilities"),
        Concept("total_equity", Kind.INSTANT, "IDR", "Total equity"),
        Concept("total_liabilities_and_equity", Kind.INSTANT, "IDR", "Total liabilities and equity"),
        Concept("net_interest_income", Kind.DURATION, "IDR", "Net interest income"),
        Concept("operating_profit", Kind.DURATION, "IDR", "Operating profit"),
        Concept("profit_before_tax", Kind.DURATION, "IDR", "Profit before tax"),
        Concept("income_tax", Kind.DURATION, "IDR", "Income tax expense (positive = expense)"),
        Concept("net_income", Kind.DURATION, "IDR", "Net profit for the period"),
        Concept("net_income_parent", Kind.DURATION, "IDR", "Net profit attributable to owners of the parent"),
        Concept(
            "net_income_nci", Kind.DURATION, "IDR", "Net profit attributable to non-controlling interests"
        ),
        Concept("eps_basic", Kind.DURATION, "IDR/share", "Basic earnings per share, as reported (rounded)"),
    )
}
