"""Versioned, idempotent writes to `daily_prices` (bitemporal, never destructive)."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from idx_trading.db.base import utcnow
from idx_trading.db.enums import RecordOutcome, ValidationStatus
from idx_trading.db.models import DailyPrice
from idx_trading.ingestion.schemas import EodBar

VALUE_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "prev_close",
    "volume",
    "value",
    "frequency",
    "foreign_buy_volume",
    "foreign_sell_volume",
    "foreign_buy_value",
    "foreign_sell_value",
)


class RevisionPolicy(StrEnum):
    #: Keep the stored row; record a `revision_conflict` issue for review. (default)
    FLAG = "flag"
    #: Store the new values as a new version and supersede the old one.
    SUPERSEDE = "supersede"


def _canon(v: object) -> object:
    if isinstance(v, Decimal):
        # Normalize so 1500, 1500.0 and 1500.0000 hash identically.
        n = v.normalize()
        return format(n, "f")
    return v


def record_hash(values: dict[str, object]) -> str:
    payload = {k: _canon(values.get(k)) for k in VALUE_COLUMNS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def stored_values(row: DailyPrice) -> dict[str, object]:
    return {k: getattr(row, k) for k in VALUE_COLUMNS}


class PriceWriter:
    def __init__(self, session: Session, policy: RevisionPolicy = RevisionPolicy.FLAG) -> None:
        self.s = session
        self.policy = policy

    def current(self, instrument_id: int, bar: EodBar, source_id: str) -> DailyPrice | None:
        return self.s.scalar(
            select(DailyPrice).where(
                DailyPrice.instrument_id == instrument_id,
                DailyPrice.trade_date == bar.trade_date,
                DailyPrice.source_id == source_id,
                DailyPrice.is_current.is_(True),
            )
        )

    def write(
        self,
        *,
        instrument_id: int,
        bar: EodBar,
        source_id: str,
        validation_status: ValidationStatus,
        run_id: int,
        artifact_id: int | None,
        volume_basis: str | None = None,
    ) -> tuple[RecordOutcome, DailyPrice | None]:
        """Returns the outcome and, for CONFLICT, the existing row that was kept."""
        values = bar.value_fields()
        h = record_hash(values)
        existing = self.current(instrument_id, bar, source_id)

        if existing is not None and existing.record_hash == h:
            return RecordOutcome.UNCHANGED, None
        if existing is not None and self.policy == RevisionPolicy.FLAG:
            return RecordOutcome.CONFLICT, existing

        version = 1
        outcome = RecordOutcome.INSERTED
        if existing is not None:
            existing.is_current = False
            existing.superseded_at = utcnow()
            self.s.flush()  # release the partial unique index before inserting
            version = existing.version + 1
            outcome = RecordOutcome.SUPERSEDED

        self.s.add(
            DailyPrice(
                instrument_id=instrument_id,
                trade_date=bar.trade_date,
                source_id=source_id,
                validation_status=validation_status.value,
                record_hash=h,
                version=version,
                is_current=True,
                ingestion_run_id=run_id,
                artifact_id=artifact_id,
                volume_basis=volume_basis,
                **values,
            )
        )
        return outcome, None
