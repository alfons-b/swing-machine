"""Financial-report ingestion with point-in-time timestamps (ADR 0010).

Reuses the EOD pipeline's guarantees: permission gate, raw archive + fetch log,
atomic data writes, a recorded run even on failure.

Point in time: `published_at` = publication date 00:00 WIB (time of day unknown);
`available_at` = the next day 00:00 WIB, so a report published at any time on day
D is never visible to an EOD signal computed for D, only from D+1 on. A re-published file is a new
revision; earlier revisions stay, and as-of queries pick by `available_at`.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from idx_trading.db.enums import RecordOutcome, RunStatus, Severity, ValidationStatus
from idx_trading.db.models import (
    FinancialFact,
    FinancialReport,
    IngestionRecord,
    Instrument,
    RawArtifact,
)
from idx_trading.fundamentals.adapters import FinancialReportAdapter
from idx_trading.fundamentals.bank_publication import ParsedPublication
from idx_trading.fundamentals.concepts import CONCEPTS
from idx_trading.fundamentals.full_statements import parse_document
from idx_trading.fundamentals.pdf import pdf_pages
from idx_trading.ingestion.adapters.base import SourceAdapter
from idx_trading.ingestion.pipeline import EodIngestionPipeline, _Counts
from idx_trading.ingestion.schemas import FetchRequest, RawPayload
from idx_trading.ingestion.validation import Finding
from idx_trading.ingestion.writer import RevisionPolicy
from idx_trading.instruments import InstrumentService

WIB = ZoneInfo("Asia/Jakarta")
_FISCAL = {3: "Q1", 6: "Q2", 9: "Q3", 12: "FY"}


class FinRule:
    UNREADABLE = "fin_unreadable_document"
    PARSE = "fin_parse_error"
    IDENTITY = "fin_identity_failed"
    PUBLISHED_BEFORE_PERIOD = "fin_published_before_period_end"
    UNKNOWN_TICKER = "fin_unknown_ticker"
    RESTATED = "fin_comparative_restated"
    EPS_SHARES = "fin_eps_share_mismatch"


def _unit(concept: str, currency: str) -> str:
    unit = CONCEPTS[concept].unit
    return unit.replace("IDR", currency) if currency != "IDR" else unit


def availability(published_on: date) -> tuple[datetime, datetime]:
    start = datetime.combine(published_on, time(0, 0), WIB)
    return start, start + timedelta(days=1)


class FinancialReportPipeline(EodIngestionPipeline):
    @staticmethod
    def _final_status(c: _Counts) -> RunStatus:
        # Reports appear a few times a year: "nothing new" is normal, not a failure.
        if c.received == 0:
            return RunStatus.SKIPPED
        return EodIngestionPipeline._final_status(c)

    def _process(
        self,
        s: Session,
        adapter: SourceAdapter,
        request: FetchRequest,
        run_id: int,
        policy: RevisionPolicy,
        c: _Counts,
    ) -> None:
        if not isinstance(adapter, FinancialReportAdapter):
            raise TypeError("FinancialReportPipeline needs a FinancialReportAdapter")
        for payload in adapter.fetch(request):
            artifact_id = self._archive(s, adapter.source_id, payload, run_id)
            if payload.metadata.get("kind") == "ir_index":
                continue
            c.received += 1
            outcome, vstatus, msg, iid = self._one(s, adapter.source_id, payload, artifact_id, run_id)
            c.bump(outcome)
            if vstatus == ValidationStatus.WARNING:
                c.warned += 1
            s.add(
                IngestionRecord(
                    run_id=run_id,
                    artifact_id=artifact_id,
                    record_key=f"{payload.metadata.get('ticker')}|{payload.metadata.get('title')}"[:100],
                    instrument_id=iid,
                    outcome=outcome.value,
                    validation_status=vstatus.value,
                    message=msg,
                )
            )
        s.flush()

    def _reject(
        self, s: Session, src: str, run_id: int, rule: str, msg: str, iid: int | None = None
    ) -> tuple[RecordOutcome, ValidationStatus, str, int | None]:
        self._issue(s, run_id, src, "financial_reports", Finding(rule, Severity.ERROR, msg), iid, None)
        return RecordOutcome.REJECTED, ValidationStatus.INVALID, msg, iid

    def _one(
        self, s: Session, src: str, payload: RawPayload, artifact_id: int, run_id: int
    ) -> tuple[RecordOutcome, ValidationStatus, str | None, int | None]:
        meta = payload.metadata
        existing = s.scalar(select(FinancialReport).where(FinancialReport.artifact_id == artifact_id))
        if existing is not None:
            return RecordOutcome.UNCHANGED, ValidationStatus.VALID, None, None
        # The same document (identical bytes) obtained through another channel is not a revision.
        sha = s.scalar(select(RawArtifact.sha256).where(RawArtifact.artifact_id == artifact_id))
        same = s.scalar(
            select(FinancialReport.report_id)
            .join(RawArtifact, RawArtifact.artifact_id == FinancialReport.artifact_id)
            .where(RawArtifact.sha256 == sha)
            .limit(1)
        )
        if same is not None:
            return RecordOutcome.UNCHANGED, ValidationStatus.VALID, f"same document as report {same}", None
        try:
            pages = pdf_pages(payload.content)
        except ValueError as exc:
            return self._reject(s, src, run_id, FinRule.UNREADABLE, str(exc))
        parsed, problems, warnings = parse_document(pages)
        if parsed.errors:
            return self._reject(s, src, run_id, FinRule.PARSE, "; ".join(parsed.errors))
        if problems:
            return self._reject(s, src, run_id, FinRule.IDENTITY, "; ".join(problems))

        published_on = date.fromisoformat(str(meta["published_on"]))
        if published_on < parsed.period_end:
            return self._reject(
                s,
                src,
                run_id,
                FinRule.PUBLISHED_BEFORE_PERIOD,
                f"published {published_on} before period end {parsed.period_end}",
            )
        svc = InstrumentService(s)
        iid = svc.try_resolve(str(meta["ticker"]), parsed.period_end)
        inst = s.get(Instrument, iid) if iid else None
        if inst is None:
            return self._reject(
                s,
                src,
                run_id,
                FinRule.UNKNOWN_TICKER,
                f"{meta['ticker']} not in instrument master on {parsed.period_end}",
            )

        fiscal_period = _FISCAL.get(parsed.period_end.month)
        if fiscal_period is None:
            return self._reject(
                s, src, run_id, FinRule.PARSE, f"period end {parsed.period_end} is not a quarter end", iid
            )
        doc_type = parsed.document_type
        prior_rev = s.scalar(
            select(func.max(FinancialReport.revision_id)).where(
                FinancialReport.issuer_id == inst.issuer_id,
                FinancialReport.fiscal_year == parsed.period_end.year,
                FinancialReport.fiscal_period == fiscal_period,
                FinancialReport.document_type == doc_type,
            )
        )
        published_at, available_at = availability(published_on)
        report = FinancialReport(
            issuer_id=inst.issuer_id,
            fiscal_year=parsed.period_end.year,
            fiscal_period=fiscal_period,
            period_end=parsed.period_end,
            consolidated=True,
            audited=parsed.audited,
            currency="IDR",
            published_at=published_at,
            available_at=available_at,
            retrieved_at=payload.retrieved_at,
            source_id=src,
            revision_id=(prior_rev or 0) + 1,
            artifact_id=artifact_id,
            document_type=doc_type,
            title=str(meta.get("title") or "")[:500] or None,
            published_at_basis=str(meta.get("published_at_basis") or "unknown"),
            ingestion_run_id=run_id,
        )
        s.add(report)
        s.flush()
        for f in parsed.facts:
            s.add(
                FinancialFact(
                    report_id=report.report_id,
                    concept=f.concept,
                    value=f.value,
                    unit=CONCEPTS[f.concept].unit,
                    source_label=f.source_label[:300],
                    basis=f.basis,
                    period_start=f.period_start,
                    period_end=f.period_end,
                    scale=f.scale,
                    statement=f.statement,
                )
            )
        s.flush()
        restated = self._check_comparatives(s, src, run_id, report, parsed, iid)
        for w in warnings:
            restated += 1
            self._issue(
                s,
                run_id,
                src,
                "financial_reports",
                Finding(FinRule.EPS_SHARES, Severity.WARNING, w),
                iid,
                parsed.period_end,
            )
        outcome = RecordOutcome.SUPERSEDED if prior_rev else RecordOutcome.INSERTED
        if restated:
            return (
                outcome,
                ValidationStatus.WARNING,
                f"{restated} comparative value(s) differ from earlier reports",
                iid,
            )
        return outcome, ValidationStatus.VALID, None, iid

    def _check_comparatives(
        self,
        s: Session,
        src: str,
        run_id: int,
        report: FinancialReport,
        parsed: ParsedPublication,
        iid: int | None,
    ) -> int:
        """Values this report gives for *earlier* periods vs what earlier reports said."""
        n = 0
        for f in parsed.facts:
            if f.period_end >= report.period_end:
                continue
            q = (
                select(FinancialFact.value, FinancialReport.report_id)
                .join(FinancialReport, FinancialReport.report_id == FinancialFact.report_id)
                .where(
                    FinancialReport.issuer_id == report.issuer_id,
                    FinancialReport.report_id != report.report_id,
                    FinancialFact.concept == f.concept,
                    FinancialFact.basis == f.basis,
                    FinancialFact.period_end == f.period_end,
                )
            )
            for old_value, old_report in s.execute(q).all():
                if old_value is not None and Decimal(old_value) != f.value:
                    n += 1
                    self._issue(
                        s,
                        run_id,
                        src,
                        "financial_reports",
                        Finding(
                            FinRule.RESTATED,
                            Severity.WARNING,
                            f"{f.concept} ({f.basis}, {f.period_end}) was {old_value}, now {f.value}",
                            {"earlier_report_id": old_report, "report_id": report.report_id},
                        ),
                        iid,
                        f.period_end,
                    )
        return n
