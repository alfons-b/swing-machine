"""Corporate-action source adapters: EODHD (dividends + splits) and manual CSV."""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from abc import abstractmethod
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, field_validator

from idx_trading.corporate_actions.schemas import ActionParseResult, CorporateActionRecord
from idx_trading.db.enums import CorporateActionType, PermissionPurpose
from idx_trading.ingestion.adapters.base import SourceAdapter
from idx_trading.ingestion.adapters.eodhd import (
    COST_EOD,
    CallBudget,
    EodhdEodAdapter,
    TickerWindow,
    to_vendor_symbol,
)
from idx_trading.ingestion.http import HttpClient
from idx_trading.ingestion.schemas import FetchRequest, ParseError, ParseResult, RawPayload
from idx_trading.instruments.service import normalize_ticker


class CorporateActionAdapter(SourceAdapter):
    dataset = "corporate_actions"

    def parse(self, payload: RawPayload) -> ParseResult:  # pragma: no cover - contract guard
        raise TypeError("corporate-action adapters are parsed with parse_actions()")

    @abstractmethod
    def parse_actions(self, payload: RawPayload) -> ActionParseResult:
        """Convert one payload into action records; report bad rows, never raise."""


# ---------------------------------------------------------------------- EODHD
def _blank(v: object) -> object:
    return None if v in ("", "0000-00-00") else v


class EodhdDividend(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: dt.date
    declarationDate: dt.date | None = None
    recordDate: dt.date | None = None
    paymentDate: dt.date | None = None
    period: str | None = None
    value: Decimal | None = None  # restated for later splits
    unadjustedValue: Decimal | None = None
    currency: str | None = None

    _b = field_validator("declarationDate", "recordDate", "paymentDate", mode="before")(_blank)


class EodhdSplit(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: dt.date
    split: str  # "new/old", e.g. "5.000000/1.000000"


_DIVS = TypeAdapter(list[EodhdDividend])
_SPLITS = TypeAdapter(list[EodhdSplit])


def parse_split_ratio(text: str) -> tuple[Decimal, Decimal]:
    """EODHD ratio string is new/old. Returns (old, new)."""
    try:
        new_s, old_s = text.split("/")
        new, old = Decimal(new_s), Decimal(old_s)
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(f"bad split ratio {text!r}") from exc
    if new <= 0 or old <= 0 or new == old:
        raise ValueError(f"bad split ratio {text!r}")
    return old, new


class EodhdCorporateActionAdapter(CorporateActionAdapter):
    """`/div/{T}.{EX}` and `/splits/{T}.{EX}`: 1 API call each (free plan: past year)."""

    source_id = "eodhd"
    purpose = PermissionPurpose.AUTOMATED_INGESTION
    KINDS = ("dividends", "splits")

    def __init__(
        self,
        token: str,
        client: HttpClient,
        budget: CallBudget,
        *,
        exchange: str = "JK",
        base_url: str = "https://eodhd.com/api",
    ) -> None:
        # Reuse the EOD adapter's authenticated GET, redaction and fatal-status handling.
        self._eod = EodhdEodAdapter(token, client, budget, exchange=exchange, base_url=base_url)
        self.budget = budget
        self.exchange = exchange.upper()

    def fetch(self, request: FetchRequest) -> Iterable[RawPayload]:
        return self._iter(request)

    def _iter(self, request: FetchRequest) -> Iterator[RawPayload]:
        windows: list[TickerWindow] = list(request.options.get("windows") or [])
        if not windows:
            windows = [
                TickerWindow(t, request.start_date, request.end_date)
                for t in request.options.get("tickers", [])
            ]
        if not windows:
            raise ValueError("needs `windows` or `tickers`")
        for w in windows:
            symbol = to_vendor_symbol(w.ticker, self.exchange)
            for kind, path in (("dividends", "div"), ("splits", "splits")):
                self.budget.spend(COST_EOD, f"{kind} {symbol}")
                params = {"fmt": "json"}
                if w.start:
                    params["from"] = w.start.isoformat()
                if w.end:
                    params["to"] = w.end.isoformat()
                status, body, _ctype, url = self._eod._get(f"/{path}/{symbol}", params)
                yield RawPayload(
                    content=body,
                    content_type="application/json",
                    origin=url,
                    retrieved_at=datetime.now(tz=UTC),
                    metadata={
                        "http_status": status,
                        "kind": kind,
                        "ticker": normalize_ticker(w.ticker),
                        "exchange": self.exchange,
                        "window_from": w.start.isoformat() if w.start else None,
                        "window_to": w.end.isoformat() if w.end else None,
                    },
                )

    def parse_actions(self, payload: RawPayload) -> ActionParseResult:
        out = ActionParseResult()
        meta = payload.metadata
        ticker, kind = str(meta["ticker"]), str(meta["kind"])
        label = f"{ticker}:{kind}"
        if int(meta.get("http_status", 200)) != 200:
            snippet = payload.content[:200].decode("utf-8", "replace").strip()
            out.errors.append(ParseError(label, f"HTTP {meta.get('http_status')} from source: {snippet}"))
            return out
        try:
            raw = json.loads(payload.content or b"[]")
            rows: list[EodhdDividend | EodhdSplit] = list(
                _DIVS.validate_python(raw) if kind == "dividends" else _SPLITS.validate_python(raw)
            )
        except (json.JSONDecodeError, ValidationError) as exc:
            msg = f"response does not match EODHD {kind} schema: {str(exc)[:300]}"
            out.errors.append(ParseError(label, msg))
            return out
        for i, row in enumerate(rows):
            key = f"{label}#{i}"
            try:
                if isinstance(row, EodhdDividend):
                    if row.unadjustedValue is None:
                        # `value` is restated for later splits; using it would be wrong.
                        raise ValueError("dividend without unadjustedValue")
                    out.actions.append(
                        CorporateActionRecord(
                            ticker=ticker,
                            action_type=CorporateActionType.CASH_DIVIDEND,
                            ex_date=row.date,
                            announcement_date=row.declarationDate,
                            record_date=row.recordDate,
                            payment_date=row.paymentDate,
                            cash_amount=row.unadjustedValue,
                            currency=row.currency or "IDR",
                        )
                    )
                else:
                    old, new = parse_split_ratio(row.split)
                    out.actions.append(
                        CorporateActionRecord(
                            ticker=ticker,
                            action_type=(
                                CorporateActionType.STOCK_SPLIT
                                if new > old
                                else CorporateActionType.REVERSE_SPLIT
                            ),
                            ex_date=row.date,
                            ratio_old=old,
                            ratio_new=new,
                        )
                    )
            except (ValueError, ValidationError) as exc:
                out.errors.append(ParseError(key, str(exc).splitlines()[0]))
        return out


# ------------------------------------------------------------------ manual CSV
CSV_COLUMNS = (
    "ticker",
    "action_type",
    "ex_date",
    "ratio_old",
    "ratio_new",
    "cash_amount",
    "exercise_price",
    "currency",
    "announcement_date",
    "record_date",
    "payment_date",
)


class CsvCorporateActionAdapter(CorporateActionAdapter):
    """User-entered actions, e.g. from IDX/KSEI announcements (rights issues, bonus shares).

    Columns: ticker, action_type, ex_date, [ratio_old, ratio_new, cash_amount,
    exercise_price, currency, announcement_date, record_date, payment_date].
    Dates YYYY-MM-DD; numbers with '.' as the decimal separator.
    """

    source_id = "manual_csv"
    purpose = PermissionPurpose.MANUAL_IMPORT

    def fetch(self, request: FetchRequest) -> Iterable[RawPayload]:
        if not request.paths:
            raise ValueError("needs at least one path")
        for p in request.paths:
            path = Path(p)
            yield RawPayload(
                path.read_bytes(),
                "text/csv",
                path.name,
                datetime.now(tz=UTC),
                {"kind": "corporate_actions_csv"},
            )

    def parse_actions(self, payload: RawPayload) -> ActionParseResult:
        out = ActionParseResult()
        reader = csv.DictReader(io.StringIO(payload.content.decode("utf-8-sig")))
        headers = {h.strip().lower() for h in (reader.fieldnames or []) if h}
        missing = {"ticker", "action_type", "ex_date"} - headers
        if missing:
            out.errors.append(ParseError(payload.origin, f"missing required column(s): {sorted(missing)}"))
            return out
        unknown = headers - set(CSV_COLUMNS)
        if unknown:
            out.errors.append(ParseError(payload.origin, f"unknown column(s): {sorted(unknown)}"))
            return out
        for lineno, row in enumerate(reader, start=2):
            clean = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            values = {k: v for k, v in clean.items() if v != ""}
            try:
                out.actions.append(CorporateActionRecord.model_validate(values))
            except (ValidationError, ValueError) as exc:
                out.errors.append(ParseError(f"{payload.origin}:{lineno}", str(exc).splitlines()[0]))
        return out


class ArchiveReplayActionAdapter(CorporateActionAdapter):
    """Replay archived corporate-action payloads through the original parser."""

    def __init__(self, parser: CorporateActionAdapter, archive_root: Path) -> None:
        from idx_trading.ingestion.adapters.replay import (
            CORPORATE_ACTION_KINDS,
            ArchiveReplayAdapter,
        )

        self._replay = ArchiveReplayAdapter(parser, archive_root, CORPORATE_ACTION_KINDS)
        self.parser = parser
        self.source_id = parser.source_id
        self.purpose = PermissionPurpose.RAW_ARCHIVE

    def fetch(self, request: FetchRequest) -> Iterable[RawPayload]:
        return self._replay.fetch(request)

    def parse_actions(self, payload: RawPayload) -> ActionParseResult:
        return self.parser.parse_actions(payload)
