"""Versioned, idempotent writes to `corporate_actions` (same policy as prices, ADR 0003)."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from idx_trading.corporate_actions.schemas import CorporateActionRecord
from idx_trading.db.base import utcnow
from idx_trading.db.enums import RecordOutcome, ValidationStatus
from idx_trading.db.models import CorporateAction
from idx_trading.ingestion.writer import RevisionPolicy, _canon


def action_hash(rec: CorporateActionRecord) -> str:
    payload = {k: _canon(v) for k, v in sorted(rec.value_fields().items())}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class ActionWriter:
    def __init__(self, session: Session, policy: RevisionPolicy = RevisionPolicy.FLAG) -> None:
        self.s = session
        self.policy = policy

    def current(
        self, source_id: str, instrument_id: int, rec: CorporateActionRecord
    ) -> CorporateAction | None:
        return self.s.scalar(
            select(CorporateAction).where(
                CorporateAction.source_id == source_id,
                CorporateAction.instrument_id == instrument_id,
                CorporateAction.action_type == rec.action_type.value,
                CorporateAction.ex_date == rec.ex_date,
                CorporateAction.is_current.is_(True),
            )
        )

    def write(
        self,
        *,
        source_id: str,
        instrument_id: int,
        rec: CorporateActionRecord,
        validation_status: ValidationStatus,
        run_id: int,
        artifact_id: int | None,
        available_at: object,
    ) -> tuple[RecordOutcome, CorporateAction | None]:
        h = action_hash(rec)
        existing = self.current(source_id, instrument_id, rec)
        if existing is not None and existing.record_hash == h:
            return RecordOutcome.UNCHANGED, None
        if existing is not None and self.policy == RevisionPolicy.FLAG:
            return RecordOutcome.CONFLICT, existing
        version, outcome = 1, RecordOutcome.INSERTED
        if existing is not None:
            existing.is_current = False
            existing.superseded_at = utcnow()
            self.s.flush()
            version, outcome = existing.version + 1, RecordOutcome.SUPERSEDED
        self.s.add(
            CorporateAction(
                instrument_id=instrument_id,
                action_type=rec.action_type.value,
                ex_date=rec.ex_date,
                announcement_date=rec.announcement_date,
                record_date=rec.record_date,
                payment_date=rec.payment_date,
                ratio_old=rec.ratio_old,
                ratio_new=rec.ratio_new,
                cash_amount=rec.cash_amount,
                exercise_price=rec.exercise_price,
                currency=rec.currency,
                status=rec.status,
                notes=rec.notes,
                source_id=source_id,
                artifact_id=artifact_id,
                available_at=available_at,
                validation_status=validation_status.value,
                record_hash=h,
                version=version,
                is_current=True,
                ingestion_run_id=run_id,
            )
        )
        return outcome, None
