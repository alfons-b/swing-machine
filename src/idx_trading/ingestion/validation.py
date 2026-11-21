"""Data-quality rules for EOD bars (PRD §8.2).

Each rule returns zero or more `Finding`s. A bar with any ERROR finding is
`invalid` and is rejected (never written to `daily_prices`); WARNING findings
are stored with the bar and surfaced on the Data Intelligence Monitor.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from idx_trading.db.enums import ListingStatus, Severity, ValidationStatus
from idx_trading.ingestion.schemas import EodBar


class Rule:
    PRICE_NON_POSITIVE = "price_non_positive"
    CLOSE_MISSING = "close_missing"
    OHLC_INCONSISTENT = "ohlc_inconsistent"
    NEGATIVE_QUANTITY = "negative_quantity"
    FUTURE_DATE = "future_date"
    UNKNOWN_TICKER = "unknown_ticker"
    NOT_TRADING_DAY = "not_trading_day"
    CALENDAR_UNKNOWN = "calendar_unknown"
    DUPLICATE_CONFLICT = "duplicate_conflict"
    DUPLICATE_IDENTICAL = "duplicate_identical"
    EXTREME_MOVE = "extreme_move"
    VOLUME_VALUE_MISMATCH = "volume_value_mismatch"
    INSTRUMENT_NOT_ACTIVE = "instrument_not_active"
    LISTING_STATUS_UNKNOWN = "listing_status_unknown"
    NO_TRADE = "no_trade"
    PARSE_ERROR = "parse_error"
    REVISION_CONFLICT = "revision_conflict"
    PRICE_NOT_INTEGER = "price_not_integer"
    OFF_TICK = "off_tick"
    VENDOR_FILLER = "vendor_holiday_filler"
    STALE_DATA = "stale_data"
    MISSING_OBSERVATIONS = "missing_observations"


@dataclass(frozen=True)
class Finding:
    rule_code: str
    severity: Severity
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BarContext:
    """Facts about the bar's environment, looked up by the pipeline."""

    today: date
    instrument_id: int | None
    listing_status: ListingStatus | None
    is_trading_day: bool | None  # None = calendar has no entry
    reference_close: Decimal | None  # bar.prev_close, or previous stored close
    extreme_move_threshold: float
    currency: str = "IDR"


def status_of(findings: list[Finding]) -> ValidationStatus:
    if any(f.severity == Severity.ERROR for f in findings):
        return ValidationStatus.INVALID
    if any(f.severity == Severity.WARNING for f in findings):
        return ValidationStatus.WARNING
    return ValidationStatus.VALID


def normalize_no_trade(bar: EodBar) -> tuple[EodBar, list[Finding]]:
    """Exchanges report O/H/L as 0 on days without trades; store them as NULL.

    The close (reference price) is kept. This is the only normalization done
    on raw prices; it is recorded as an INFO finding for traceability.
    """
    if bar.volume == 0 and any(v == 0 for v in (bar.open, bar.high, bar.low)):
        fixed = bar.model_copy(update={k: None for k in ("open", "high", "low") if getattr(bar, k) == 0})
        return fixed, [Finding(Rule.NO_TRADE, Severity.INFO, "no trades; zero O/H/L stored as NULL")]
    return bar, []


# ----------------------------------------------------------------- bar rules
def _price_positive(bar: EodBar, _: BarContext) -> list[Finding]:
    bad = {
        k: str(v)
        for k in ("open", "high", "low", "close", "prev_close")
        if (v := getattr(bar, k)) is not None and v <= 0
    }
    if bad:
        return [Finding(Rule.PRICE_NON_POSITIVE, Severity.ERROR, "non-positive price", bad)]
    return []


def _close_present(bar: EodBar, _: BarContext) -> list[Finding]:
    if bar.close is None:
        return [Finding(Rule.CLOSE_MISSING, Severity.ERROR, "close price missing")]
    return []


def _ohlc_consistent(bar: EodBar, _: BarContext) -> list[Finding]:
    o, h, lo, c = bar.open, bar.high, bar.low, bar.close
    problems: list[str] = []
    if h is not None and lo is not None:
        if h < lo:
            problems.append("high < low")
        for name, v in (("open", o), ("close", c)):
            if v is not None and not (lo <= v <= h):
                problems.append(f"{name} outside [low, high]")
    elif (h is None) != (lo is None):
        problems.append("only one of high/low present")
    if problems:
        return [
            Finding(
                Rule.OHLC_INCONSISTENT,
                Severity.ERROR,
                "; ".join(problems),
                {"open": str(o), "high": str(h), "low": str(lo), "close": str(c)},
            )
        ]
    return []


def _non_negative_quantities(bar: EodBar, _: BarContext) -> list[Finding]:
    fields = (
        "volume",
        "value",
        "frequency",
        "foreign_buy_volume",
        "foreign_sell_volume",
        "foreign_buy_value",
        "foreign_sell_value",
    )
    bad = {k: str(v) for k in fields if (v := getattr(bar, k)) is not None and v < 0}
    if bad:
        return [Finding(Rule.NEGATIVE_QUANTITY, Severity.ERROR, "negative quantity", bad)]
    return []


def _volume_value_agree(bar: EodBar, _: BarContext) -> list[Finding]:
    v, val = bar.volume, bar.value
    if v is None or val is None:
        return []
    if (v == 0) != (val == 0):
        return [
            Finding(
                Rule.VOLUME_VALUE_MISMATCH,
                Severity.WARNING,
                "volume and value disagree on whether trading occurred",
                {"volume": v, "value": str(val)},
            )
        ]
    if v > 0 and bar.high is not None and bar.low is not None:
        vwap = val / v
        # Allow a small tolerance for rounding and non-regular-market value.
        if not (bar.low * Decimal("0.95") <= vwap <= bar.high * Decimal("1.05")):
            return [
                Finding(
                    Rule.VOLUME_VALUE_MISMATCH,
                    Severity.WARNING,
                    "implied VWAP outside the day's range",
                    {"vwap": str(round(vwap, 4))},
                )
            ]
    return []


def _not_future(bar: EodBar, ctx: BarContext) -> list[Finding]:
    if bar.trade_date > ctx.today:
        return [Finding(Rule.FUTURE_DATE, Severity.ERROR, f"trade date after {ctx.today}")]
    return []


def _known_instrument(bar: EodBar, ctx: BarContext) -> list[Finding]:
    if ctx.instrument_id is None:
        return [
            Finding(
                Rule.UNKNOWN_TICKER,
                Severity.ERROR,
                f"ticker {bar.ticker} not in instrument master on {bar.trade_date}",
            )
        ]
    if ctx.listing_status == ListingStatus.UNKNOWN:
        return [
            Finding(
                Rule.LISTING_STATUS_UNKNOWN,
                Severity.INFO,
                "instrument has no listing history; status unknown",
            )
        ]
    if ctx.listing_status not in (None, ListingStatus.ACTIVE):
        return [
            Finding(
                Rule.INSTRUMENT_NOT_ACTIVE,
                Severity.WARNING,
                f"instrument status is {ctx.listing_status.value}",
            )
        ]
    return []


def _trading_day(bar: EodBar, ctx: BarContext) -> list[Finding]:
    if ctx.is_trading_day is None:
        return [Finding(Rule.CALENDAR_UNKNOWN, Severity.WARNING, "date not in trading calendar")]
    if not ctx.is_trading_day:
        if not bar.volume:
            # Vendors fill exchange holidays with zero-volume bars (EODHD: flat at the
            # previous close, sometimes 999999.9999). Not a session: dropped, not an error.
            return [Finding(Rule.VENDOR_FILLER, Severity.INFO, "zero-volume bar on a non-session; dropped")]
        return [Finding(Rule.NOT_TRADING_DAY, Severity.ERROR, "date is not an IDX session")]
    return []


def idx_tick(price: Decimal) -> int:
    """Current IDX price fraction for the regular market."""
    return 1 if price < 200 else 2 if price < 500 else 5 if price < 2000 else 10 if price < 5000 else 25


def _whole_rupiah(bar: EodBar, ctx: BarContext) -> list[Finding]:
    """IDX trades in whole rupiah: a fractional raw price is not a raw price."""
    if ctx.currency != "IDR":
        return []
    bad = {
        k: str(v)
        for k in ("open", "high", "low", "close")
        if (v := getattr(bar, k)) is not None and v != v.to_integral_value()
    }
    if bad:
        return [Finding(Rule.PRICE_NOT_INTEGER, Severity.ERROR, "fractional IDR price (not raw)", bad)]
    if bar.close is not None and bar.close > 0 and bar.close % idx_tick(bar.close) != 0:
        return [Finding(Rule.OFF_TICK, Severity.WARNING, f"close {bar.close} is off the current tick grid")]
    return []


def _extreme_move(bar: EodBar, ctx: BarContext) -> list[Finding]:
    ref, c = ctx.reference_close, bar.close
    if ref is None or c is None or ref <= 0:
        return []
    change = float(c / ref - 1)
    if abs(change) > ctx.extreme_move_threshold:
        return [
            Finding(
                Rule.EXTREME_MOVE,
                Severity.WARNING,
                f"close changed {change:+.1%} vs reference; check for corporate action",
                {"reference_close": str(ref), "close": str(c), "change": round(change, 6)},
            )
        ]
    return []


BAR_RULES: tuple[Callable[[EodBar, BarContext], list[Finding]], ...] = (
    _close_present,
    _price_positive,
    _ohlc_consistent,
    _non_negative_quantities,
    _volume_value_agree,
    _not_future,
    _known_instrument,
    _trading_day,
    _whole_rupiah,
    _extreme_move,
)


def validate_bar(bar: EodBar, ctx: BarContext) -> list[Finding]:
    findings: list[Finding] = []
    for rule in BAR_RULES:
        findings.extend(rule(bar, ctx))
    return findings


# --------------------------------------------------------------- batch rules
def dedupe_batch(bars: list[EodBar]) -> tuple[list[EodBar], dict[str, list[Finding]]]:
    """Collapse identical duplicates; mark conflicting duplicates (same key, other values).

    Returns the surviving bars (one per key, conflicting keys excluded) and the
    findings per key.
    """
    groups: dict[str, list[EodBar]] = {}
    for b in bars:
        groups.setdefault(b.key, []).append(b)
    survivors: list[EodBar] = []
    findings: dict[str, list[Finding]] = {}
    for key, group in groups.items():
        distinct = {tuple(sorted((k, str(v)) for k, v in g.value_fields().items())) for g in group}
        if len(distinct) > 1:
            findings[key] = [
                Finding(
                    Rule.DUPLICATE_CONFLICT,
                    Severity.ERROR,
                    f"{len(group)} rows for {key} with different values",
                )
            ]
            continue
        if len(group) > 1:
            findings[key] = [Finding(Rule.DUPLICATE_IDENTICAL, Severity.INFO, f"{len(group)} identical rows")]
        survivors.append(group[0])
    return survivors, findings
