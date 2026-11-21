"""Corporate-action ingestion: same guarantees as the EOD pipeline (gate, archive, atomic, idempotent)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from idx_trading.calendar import CalendarService
from idx_trading.corporate_actions.adapters import CorporateActionAdapter
from idx_trading.corporate_actions.schemas import CorporateActionRecord
from idx_trading.corporate_actions.validation import ActionContext, CaRule, validate_action
from idx_trading.corporate_actions.writer import ActionWriter
from idx_trading.db.enums import RecordOutcome, Severity, ValidationStatus
from idx_trading.db.models import IngestionRecord, Instrument
from idx_trading.ingestion.adapters.base import SourceAdapter
from idx_trading.ingestion.pipeline import EodIngestionPipeline, _Counts
from idx_trading.ingestion.schemas import FetchRequest
from idx_trading.ingestion.validation import Finding, Rule, status_of
from idx_trading.ingestion.writer import RevisionPolicy
from idx_trading.instruments import InstrumentService


class CorporateActionPipeline(EodIngestionPipeline):
    def _process(
        self,
        s: Session,
        adapter: SourceAdapter,
        request: FetchRequest,
        run_id: int,
        policy: RevisionPolicy,
        c: _Counts,
    ) -> None:
        if not isinstance(adapter, CorporateActionAdapter):
            raise TypeError("CorporateActionPipeline needs a CorporateActionAdapter")
        src, ds = adapter.source_id, adapter.dataset
        instruments = InstrumentService(s)
        calendar = CalendarService(s, self.calendar_code)
        writer = ActionWriter(s, policy)

        batch: list[tuple[CorporateActionRecord, int, object]] = []
        for payload in adapter.fetch(request):
            artifact_id = self._archive(s, src, payload, run_id)
            parsed = adapter.parse_actions(payload)
            for err in parsed.errors:
                c.received += 1
                c.rejected += 1
                f = Finding(Rule.PARSE_ERROR, Severity.ERROR, err.message)
                self._issue(s, run_id, src, ds, f, None, None)
                s.add(
                    IngestionRecord(
                        run_id=run_id,
                        artifact_id=artifact_id,
                        record_key=err.record_key[:100],
                        outcome=RecordOutcome.REJECTED.value,
                        validation_status=ValidationStatus.INVALID.value,
                        message=err.message,
                    )
                )
            batch.extend((a, artifact_id, payload.retrieved_at) for a in parsed.actions)

        c.received += len(batch)
        seen: dict[str, CorporateActionRecord] = {}
        for rec, artifact_id, available_at in sorted(batch, key=lambda b: (b[0].ex_date, b[0].key)):
            findings: list[Finding] = []
            if rec.key in seen:
                if seen[rec.key] == rec:
                    c.unchanged += 1
                    continue
                findings.append(
                    Finding(
                        Rule.DUPLICATE_CONFLICT,
                        Severity.ERROR,
                        f"two different {rec.action_type.value} rows for {rec.key}",
                    )
                )
            seen[rec.key] = rec
            iid = instruments.try_resolve(rec.ticker, rec.ex_date)
            inst = s.get(Instrument, iid) if iid else None
            ctx = ActionContext(
                instrument_id=iid,
                instrument_currency=inst.currency if inst else None,
                is_trading_day=(
                    calendar.is_trading_day(rec.ex_date) if calendar.is_known(rec.ex_date) else None
                ),
            )
            findings += validate_action(rec, ctx)
            vstatus = status_of(findings)
            if vstatus == ValidationStatus.INVALID or iid is None:
                outcome = RecordOutcome.REJECTED
            else:
                outcome, kept = writer.write(
                    source_id=src,
                    instrument_id=iid,
                    rec=rec,
                    validation_status=vstatus,
                    run_id=run_id,
                    artifact_id=artifact_id,
                    available_at=available_at,
                )
                if outcome == RecordOutcome.CONFLICT and kept is not None:
                    findings.append(
                        Finding(
                            CaRule.REVISION_CONFLICT,
                            Severity.WARNING,
                            "source now reports different values; stored version kept",
                            {
                                "stored_version": kept.version,
                                "incoming": {k: str(v) for k, v in rec.value_fields().items()},
                            },
                        )
                    )
                if vstatus == ValidationStatus.WARNING and outcome != RecordOutcome.UNCHANGED:
                    c.warned += 1
            c.bump(outcome)
            if outcome != RecordOutcome.UNCHANGED:
                for f in findings:
                    self._issue(s, run_id, src, ds, f, iid, rec.ex_date)
            s.add(
                IngestionRecord(
                    run_id=run_id,
                    artifact_id=artifact_id,
                    record_key=rec.key[:100],
                    instrument_id=iid,
                    trade_date=rec.ex_date,
                    outcome=outcome.value,
                    validation_status=vstatus.value,
                    message="; ".join(f"{f.rule_code}: {f.message}" for f in findings) or None,
                )
            )
        s.flush()
