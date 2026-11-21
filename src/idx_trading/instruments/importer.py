"""CSV importers for the instrument master (explicit schemas, idempotent)."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from idx_trading.clock import exchange_today
from idx_trading.db.enums import InstrumentType, ListingEventType, PermissionPurpose
from idx_trading.db.models import Instrument
from idx_trading.errors import SchemaValidationError
from idx_trading.instruments.service import InstrumentService, normalize_ticker
from idx_trading.registry import require_permission


def _blank_to_none(v: object) -> object:
    return None if isinstance(v, str) and v.strip() == "" else v


class InstrumentRow(BaseModel):
    """Schema for `instruments.csv`.

    Columns: ticker, company_name, instrument_type, listing_date, delisting_date, board, isin
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    ticker: str
    company_name: str
    instrument_type: InstrumentType = InstrumentType.COMMON_STOCK
    listing_date: date | None = None
    delisting_date: date | None = None
    board: str | None = None
    isin: str | None = None

    _blank = field_validator(
        "listing_date", "delisting_date", "board", "isin", "instrument_type", mode="before"
    )(_blank_to_none)

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, v: str) -> str:
        return normalize_ticker(v)

    @field_validator("isin")
    @classmethod
    def _isin(cls, v: str | None) -> str | None:
        if v is not None and (len(v) != 12 or not v[:2].isalpha()):
            raise ValueError("ISIN must be 12 characters starting with a country code")
        return v.upper() if v else v


class TickerChangeRow(BaseModel):
    """Schema for `ticker_changes.csv`: old_ticker, new_ticker, effective_date, reason."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    old_ticker: str
    new_ticker: str
    effective_date: date
    reason: str | None = None

    _t = field_validator("old_ticker", "new_ticker")(lambda v: normalize_ticker(v))


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def import_instruments(
    svc: InstrumentService, path: Path, source_id: str, *, strict: bool = True
) -> ImportReport:
    """Create or update instruments from a CSV.

    Matching: by ISIN when present, otherwise by the ticker's holder on the
    listing date (or the delisting eve for delisted names, or today).
    With `strict`, any schema error aborts the import before writing.
    """
    require_permission(svc.s, source_id, PermissionPurpose.MANUAL_IMPORT)
    report = ImportReport()
    parsed: list[tuple[int, InstrumentRow]] = []
    for lineno, raw in enumerate(_read_rows(path), start=2):
        try:
            parsed.append((lineno, InstrumentRow.model_validate(raw)))
        except (ValidationError, ValueError) as exc:
            report.errors.append(f"line {lineno}: {exc}")
    if report.errors and strict:
        raise SchemaValidationError("; ".join(report.errors))

    for _lineno, row in parsed:
        inst: Instrument | None = None
        if row.isin:
            inst = svc.s.query(Instrument).filter_by(isin=row.isin).one_or_none()
        if inst is None:
            probe = row.listing_date or (row.delisting_date or exchange_today())
            if row.delisting_date and not row.listing_date:
                probe = date.fromordinal(row.delisting_date.toordinal() - 1)
            iid = svc.try_resolve(row.ticker, probe)
            inst = svc.s.get(Instrument, iid) if iid else None

        if inst is None:
            inst = svc.create_instrument(
                ticker=row.ticker,
                issuer_name=row.company_name,
                instrument_type=row.instrument_type,
                listing_date=row.listing_date,
                isin=row.isin,
                board=row.board,
                source_id=source_id,
            )
            report.created += 1
        else:
            changed = False
            for attr, val in (
                ("instrument_type", row.instrument_type.value),
                ("board", row.board),
                ("isin", row.isin),
            ):
                if val is not None and getattr(inst, attr) != val:
                    setattr(inst, attr, val)
                    changed = True
            if row.company_name and inst.issuer.name != row.company_name:
                inst.issuer.name = row.company_name
                changed = True
            if row.listing_date and inst.first_listing_date != row.listing_date:
                svc.add_listing_event(
                    inst.instrument_id,
                    ListingEventType.LISTED,
                    row.listing_date,
                    source_id=source_id,
                )
                changed = True
            report.updated += int(changed)
            report.unchanged += int(not changed)

        if row.delisting_date and inst.delisting_date != row.delisting_date:
            svc.add_listing_event(
                inst.instrument_id,
                ListingEventType.DELISTED,
                row.delisting_date,
                source_id=source_id,
            )
    svc.s.flush()
    return report


def import_ticker_changes(svc: InstrumentService, path: Path, source_id: str) -> ImportReport:
    require_permission(svc.s, source_id, PermissionPurpose.MANUAL_IMPORT)
    report = ImportReport()
    rows: list[TickerChangeRow] = []
    for lineno, raw in enumerate(_read_rows(path), start=2):
        try:
            rows.append(TickerChangeRow.model_validate(raw))
        except (ValidationError, ValueError) as exc:
            report.errors.append(f"line {lineno}: {exc}")
    if report.errors:
        raise SchemaValidationError("; ".join(report.errors))
    for row in sorted(rows, key=lambda r: r.effective_date):
        if svc.try_resolve(row.new_ticker, row.effective_date) is not None:
            report.unchanged += 1
            continue
        svc.change_ticker(
            row.old_ticker,
            row.new_ticker,
            row.effective_date,
            reason=row.reason,
            source_id=source_id,
        )
        report.updated += 1
    return report
