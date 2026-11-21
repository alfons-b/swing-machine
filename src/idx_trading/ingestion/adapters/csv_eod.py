"""Adapter for user-supplied EOD CSV files (registry id `manual_csv`).

Column names are mapped through a *profile* so the same adapter can read
files exported from different places. Profiles are explicit schemas: unknown
required columns fail loudly instead of being guessed.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError

from idx_trading.db.enums import PermissionPurpose
from idx_trading.ingestion.adapters.base import SourceAdapter
from idx_trading.ingestion.schemas import (
    EodBar,
    FetchRequest,
    ParseError,
    ParseResult,
    RawPayload,
)

# canonical field -> accepted header names (case-insensitive)
COLUMN_PROFILES: dict[str, dict[str, tuple[str, ...]]] = {
    # Simple long format: one row per ticker per day.
    "standard": {
        "ticker": ("ticker", "symbol", "code"),
        "trade_date": ("date", "trade_date"),
        "open": ("open",),
        "high": ("high",),
        "low": ("low",),
        "close": ("close",),
        "prev_close": ("prev_close", "previous"),
        "volume": ("volume",),
        "value": ("value", "turnover"),
        "frequency": ("frequency", "trades"),
        "foreign_buy_volume": ("foreign_buy_volume", "foreign_buy"),
        "foreign_sell_volume": ("foreign_sell_volume", "foreign_sell"),
        "foreign_buy_value": ("foreign_buy_value",),
        "foreign_sell_value": ("foreign_sell_value",),
    },
    # Daily stock-summary layout as exported manually from the IDX website.
    # Header names must be re-verified against a real export before relying on
    # it (OPEN-ISSUE #1); the trade date may be given per file via options.
    "idx_stock_summary": {
        "ticker": ("kode saham", "stock code"),
        "trade_date": ("tanggal perdagangan terakhir", "last trading date", "date"),
        "prev_close": ("sebelumnya", "previous"),
        "open": ("open price",),
        "high": ("tertinggi", "high"),
        "low": ("terendah", "low"),
        "close": ("penutupan", "close"),
        "volume": ("volume",),
        "value": ("nilai", "value"),
        "frequency": ("frekuensi", "frequency"),
        "foreign_buy_volume": ("foreign buy",),
        "foreign_sell_volume": ("foreign sell",),
    },
}
REQUIRED = ("ticker", "close")
_INT_FIELDS = {"volume", "frequency", "foreign_buy_volume", "foreign_sell_volume"}
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%Y%m%d")


def _parse_number(raw: str) -> Decimal | None:
    s = raw.strip().replace(" ", "")
    if s in {"", "-", "null", "NaN", "nan"}:
        return None
    # Accept "1,234,567.5" (comma thousands). Indonesian "1.234.567,5" must be
    # declared explicitly with options={"decimal": ","} to avoid ambiguity.
    s = s.replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {raw!r}") from exc


def _parse_date(raw: str) -> date:
    s = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()  # noqa: DTZ007 - pure date
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {raw!r}")


class CsvEodAdapter(SourceAdapter):
    source_id = "manual_csv"
    dataset = "eod_prices"
    purpose = PermissionPurpose.MANUAL_IMPORT

    def __init__(self, profile: str = "standard", source_id: str | None = None) -> None:
        if profile not in COLUMN_PROFILES:
            raise ValueError(f"unknown CSV profile {profile!r}; choose {sorted(COLUMN_PROFILES)}")
        self.profile = profile
        if source_id:
            self.source_id = source_id

    def fetch(self, request: FetchRequest) -> Iterable[RawPayload]:
        if not request.paths:
            raise ValueError("CsvEodAdapter needs at least one path")
        for p in request.paths:
            path = Path(p)
            yield RawPayload(
                content=path.read_bytes(),
                content_type="text/csv",
                origin=path.name,
                retrieved_at=datetime.now(tz=UTC),
                metadata={
                    "profile": self.profile,
                    "trade_date": request.options.get("trade_date"),
                    "decimal": request.options.get("decimal", "."),
                    "provider": request.options.get("provider"),
                },
            )

    def _header_map(self, headers: list[str]) -> dict[str, str]:
        lowered = {h.strip().lower(): h for h in headers if h}
        mapping: dict[str, str] = {}
        for canon, aliases in COLUMN_PROFILES[self.profile].items():
            for alias in aliases:
                if alias in lowered:
                    mapping[canon] = lowered[alias]
                    break
        return mapping

    def parse(self, payload: RawPayload) -> ParseResult:
        result = ParseResult()
        text = payload.content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        mapping = self._header_map(list(reader.fieldnames or []))
        file_date = payload.metadata.get("trade_date")
        missing = [c for c in REQUIRED if c not in mapping]
        if "trade_date" not in mapping and not file_date:
            missing.append("trade_date")
        if missing:
            result.errors.append(ParseError(payload.origin, f"missing required column(s): {missing}"))
            return result
        comma_decimal = payload.metadata.get("decimal") == ","

        for lineno, row in enumerate(reader, start=2):
            key = f"{payload.origin}:{lineno}"
            try:
                values: dict[str, object] = {}
                for canon, header in mapping.items():
                    raw = (row.get(header) or "").strip()
                    if canon == "ticker":
                        values[canon] = raw
                    elif canon == "trade_date":
                        values[canon] = _parse_date(raw)
                    else:
                        if comma_decimal:
                            raw = raw.replace(".", "").replace(",", ".")
                        num = _parse_number(raw)
                        if num is not None and canon in _INT_FIELDS:
                            if num != num.to_integral_value():
                                raise ValueError(f"{canon} must be an integer: {raw!r}")
                            values[canon] = int(num)
                        else:
                            values[canon] = num
                if "trade_date" not in values:
                    values["trade_date"] = _parse_date(str(file_date))
                result.bars.append(EodBar.model_validate(values))
            except (ValueError, ValidationError) as exc:
                result.errors.append(ParseError(key, str(exc).splitlines()[0]))
        return result
