"""EOD ingestion pipeline (PRD §8.1 steps 3-9, §8.2, §18.1).

Guarantees:
  * Permission gate is checked before the adapter is called.
  * Every payload is archived (when permitted) before it is parsed.
  * A run is recorded even when it fails (separate transaction).
  * All data writes of a run are atomic: committed together or not at all.
  * Re-running the same input is a no-op (content hashing + record hashing).
"""

from __future__ import annotations

import logging
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from idx_trading import __version__
from idx_trading.calendar import CalendarService
from idx_trading.clock import exchange_today
from idx_trading.db.base import utcnow
from idx_trading.db.enums import (
    PermissionPurpose,
    RecordOutcome,
    RunStatus,
    Severity,
    ValidationStatus,
    VolumeBasis,
)
from idx_trading.db.models import (
    ArtifactFetch,
    DailyPrice,
    DataQualityIssue,
    DataSource,
    IngestionRecord,
    IngestionRun,
    RawArtifact,
)
from idx_trading.db.session import session_scope
from idx_trading.ingestion.adapters.base import SourceAdapter
from idx_trading.ingestion.archive import RawArchive
from idx_trading.ingestion.schemas import EodBar, FetchRequest, RawPayload
from idx_trading.ingestion.validation import (
    BarContext,
    Finding,
    Rule,
    dedupe_batch,
    normalize_no_trade,
    status_of,
    validate_bar,
)
from idx_trading.ingestion.writer import PriceWriter, RevisionPolicy, stored_values
from idx_trading.instruments import InstrumentService
from idx_trading.registry import is_permitted, require_permission

log = logging.getLogger(__name__)
#: Labels written by earlier versions, kept so old archives replay cleanly.
_LEGACY_VOLUME_BASIS = {"split_adjusted": VolumeBasis.SPLIT_ADJUSTED_AT_RETRIEVAL.value}


def _jsonable(value: object) -> object:
    """Run params are stored as JSON; dataclasses/dates become plain values."""
    import dataclasses
    import json

    def default(o: object) -> object:
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return dataclasses.asdict(o)
        return str(o)

    return json.loads(json.dumps(value, default=default))


@dataclass
class RunSummary:
    run_id: int
    run_uuid: str
    status: RunStatus
    counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None


@dataclass
class _Counts:
    received: int = 0
    inserted: int = 0
    unchanged: int = 0
    conflicted: int = 0
    superseded: int = 0
    rejected: int = 0
    warned: int = 0

    def bump(self, outcome: RecordOutcome) -> None:
        name = {
            RecordOutcome.INSERTED: "inserted",
            RecordOutcome.UNCHANGED: "unchanged",
            RecordOutcome.CONFLICT: "conflicted",
            RecordOutcome.SUPERSEDED: "superseded",
            RecordOutcome.REJECTED: "rejected",
        }[outcome]
        setattr(self, name, getattr(self, name) + 1)


class EodIngestionPipeline:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        archive: RawArchive,
        *,
        extreme_move_threshold: float = 0.35,
        calendar_code: str = "XIDX",
        today: date | None = None,
    ) -> None:
        self.factory = session_factory
        self.archive = archive
        self.threshold = extreme_move_threshold
        self.calendar_code = calendar_code
        self._today = today

    @property
    def today(self) -> date:
        return self._today or exchange_today()

    # ------------------------------------------------------------------ public
    def run(
        self,
        adapter: SourceAdapter,
        request: FetchRequest,
        *,
        job_name: str = "eod_ingest",
        policy: RevisionPolicy = RevisionPolicy.FLAG,
    ) -> RunSummary:
        with session_scope(self.factory) as s:
            # Raises before any run is created if the source is unknown/not permitted.
            require_permission(s, adapter.source_id, adapter.purpose, self.today)
            run = IngestionRun(
                run_uuid=str(uuid.uuid4()),
                source_id=adapter.source_id,
                dataset=adapter.dataset,
                job_name=job_name,
                params={
                    "start_date": str(request.start_date) if request.start_date else None,
                    "end_date": str(request.end_date) if request.end_date else None,
                    "paths": list(request.paths),
                    "options": _jsonable(request.options),
                    "policy": policy.value,
                },
                status=RunStatus.RUNNING,
                code_version=__version__,
            )
            s.add(run)
            s.flush()
            run_id, run_uuid = run.run_id, run.run_uuid

        counts = _Counts()
        try:
            with session_scope(self.factory) as s:
                self._process(s, adapter, request, run_id, policy, counts)
            status = self._final_status(counts)
            error = None if counts.received else "adapter returned no records"
        except Exception as exc:  # recorded, then re-raised for the caller's visibility
            log.exception("ingestion run %s failed", run_uuid)
            status, error = RunStatus.FAILED, f"{type(exc).__name__}: {exc}"
            self._finish(run_id, adapter.source_id, status, _Counts(), error, traceback.format_exc())
            raise
        self._finish(run_id, adapter.source_id, status, counts, error, None)
        return RunSummary(run_id, run_uuid, status, vars(counts).copy(), error)

    # ---------------------------------------------------------------- internals
    @staticmethod
    def _final_status(c: _Counts) -> RunStatus:
        if c.received == 0:
            return RunStatus.FAILED
        accepted = c.inserted + c.unchanged + c.superseded + c.conflicted
        if c.rejected == 0:
            return RunStatus.SUCCEEDED
        return RunStatus.PARTIAL if accepted else RunStatus.FAILED

    def _finish(
        self,
        run_id: int,
        source_id: str,
        status: RunStatus,
        c: _Counts,
        error: str | None,
        tb: str | None,
    ) -> None:
        with session_scope(self.factory) as s:
            run = s.get(IngestionRun, run_id)
            assert run is not None
            run.status = status.value
            run.finished_at = utcnow()
            run.records_received = c.received
            run.records_inserted = c.inserted
            run.records_unchanged = c.unchanged
            run.records_conflicted = c.conflicted
            run.records_superseded = c.superseded
            run.records_rejected = c.rejected
            run.records_warned = c.warned
            run.error_message = error if tb is None else f"{error}\n{tb[-4000:]}"
            src = s.get(DataSource, source_id)
            if src is not None:
                if status in (RunStatus.SUCCEEDED, RunStatus.PARTIAL):
                    src.last_successful_ingestion = run.finished_at
                else:
                    src.last_failed_ingestion = run.finished_at

    def _archive(self, s: Session, source_id: str, payload: RawPayload, run_id: int) -> int:
        artifact_id = self._store_artifact(s, source_id, payload, run_id)
        meta = payload.metadata
        kind = meta.get("kind") or meta.get("mode")
        s.add(
            ArtifactFetch(
                artifact_id=artifact_id,
                run_id=run_id,
                source_id=source_id,
                origin=payload.origin,
                retrieved_at=payload.retrieved_at,
                kind=str(kind) if kind else None,
                ticker=str(meta["ticker"]) if meta.get("ticker") else None,
                extra=_jsonable(meta),
            )
        )
        s.flush()
        return artifact_id

    def _store_artifact(self, s: Session, source_id: str, payload: RawPayload, run_id: int) -> int:
        sha = RawArchive.digest(payload.content)
        existing = s.scalar(
            select(RawArtifact).where(RawArtifact.source_id == source_id, RawArtifact.sha256 == sha)
        )
        if existing is not None:
            return existing.artifact_id
        storage_path: str | None = None
        if is_permitted(s, source_id, PermissionPurpose.RAW_ARCHIVE, self.today):
            storage_path = self.archive.store(source_id, payload).relative_path
        art = RawArtifact(
            source_id=source_id,
            sha256=sha,
            size_bytes=len(payload.content),
            content_type=payload.content_type,
            storage_path=storage_path,
            origin=payload.origin,
            retrieved_at=payload.retrieved_at,
            first_run_id=run_id,
            extra=payload.metadata,
        )
        s.add(art)
        s.flush()
        return art.artifact_id

    def _issue(
        self,
        s: Session,
        run_id: int,
        source_id: str,
        dataset: str,
        f: Finding,
        instrument_id: int | None,
        trade_date: date | None,
    ) -> None:
        if f.severity == Severity.INFO:
            return  # INFO findings live on the ingestion record only
        s.add(
            DataQualityIssue(
                run_id=run_id,
                source_id=source_id,
                dataset=dataset,
                instrument_id=instrument_id,
                trade_date=trade_date,
                rule_code=f.rule_code,
                severity=f.severity.value,
                message=f.message,
                details=f.details,
            )
        )

    def _reference_close(self, s: Session, bar: EodBar, instrument_id: int, source_id: str) -> Decimal | None:
        if bar.prev_close is not None:
            return bar.prev_close
        return s.scalar(
            select(DailyPrice.close)
            .where(
                DailyPrice.instrument_id == instrument_id,
                DailyPrice.source_id == source_id,
                DailyPrice.is_current.is_(True),
                DailyPrice.trade_date < bar.trade_date,
            )
            .order_by(DailyPrice.trade_date.desc())
            .limit(1)
        )

    def _process(
        self,
        s: Session,
        adapter: SourceAdapter,
        request: FetchRequest,
        run_id: int,
        policy: RevisionPolicy,
        c: _Counts,
    ) -> None:
        src, ds = adapter.source_id, adapter.dataset
        instruments = InstrumentService(s)
        calendar = CalendarService(s, self.calendar_code)
        writer = PriceWriter(s, policy)

        bars_with_artifact: list[tuple[EodBar, int]] = []
        basis_of_artifact: dict[int, str | None] = {}
        for payload in adapter.fetch(request):
            artifact_id = self._archive(s, src, payload, run_id)
            vb = payload.metadata.get("volume_basis")
            vb = _LEGACY_VOLUME_BASIS.get(str(vb), vb) if vb else None
            if vb is not None and vb not in {v.value for v in VolumeBasis}:
                raise ValueError(f"unknown volume_basis {vb!r} in payload metadata")
            basis_of_artifact[artifact_id] = str(vb) if vb else None
            parsed = adapter.parse(payload)
            for err in parsed.errors:
                c.received += 1
                c.rejected += 1
                f = Finding(Rule.PARSE_ERROR, Severity.ERROR, err.message)
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
                self._issue(s, run_id, src, ds, f, None, None)
            bars_with_artifact.extend((b, artifact_id) for b in parsed.bars)

        c.received += len(bars_with_artifact)
        artifact_of = {b.key: a for b, a in bars_with_artifact}
        bars, dup_findings = dedupe_batch([b for b, _ in bars_with_artifact])
        # Identical duplicates beyond the first are counted as unchanged.
        for key, fs in dup_findings.items():
            n = sum(1 for b, _ in bars_with_artifact if b.key == key)
            if fs[0].rule_code == Rule.DUPLICATE_CONFLICT:
                c.rejected += n
                ticker, d = key.split("|")
                iid = instruments.try_resolve(ticker, date.fromisoformat(d))
                for f in fs:
                    self._issue(s, run_id, src, ds, f, iid, date.fromisoformat(d))
                s.add(
                    IngestionRecord(
                        run_id=run_id,
                        artifact_id=artifact_of[key],
                        record_key=key,
                        instrument_id=iid,
                        trade_date=date.fromisoformat(d),
                        outcome=RecordOutcome.REJECTED.value,
                        validation_status=ValidationStatus.INVALID.value,
                        message=fs[0].message,
                    )
                )
            else:
                c.unchanged += n - 1

        for raw_bar in sorted(bars, key=lambda b: (b.trade_date, b.ticker)):
            bar, findings = normalize_no_trade(raw_bar)
            findings += dup_findings.get(bar.key, [])
            iid = instruments.try_resolve(bar.ticker, bar.trade_date)
            ctx = BarContext(
                today=self.today,
                instrument_id=iid,
                listing_status=instruments.status_on(iid, bar.trade_date) if iid else None,
                is_trading_day=(
                    calendar.is_trading_day(bar.trade_date) if calendar.is_known(bar.trade_date) else None
                ),
                reference_close=self._reference_close(s, bar, iid, src) if iid else None,
                extreme_move_threshold=self.threshold,
            )
            findings += validate_bar(bar, ctx)
            vstatus = status_of(findings)

            kept: DailyPrice | None = None
            if vstatus == ValidationStatus.INVALID or iid is None:
                outcome = RecordOutcome.REJECTED
            else:
                outcome, kept = writer.write(
                    instrument_id=iid,
                    bar=bar,
                    source_id=src,
                    validation_status=vstatus,
                    run_id=run_id,
                    artifact_id=artifact_of[bar.key],
                    volume_basis=basis_of_artifact.get(artifact_of[bar.key]),
                )
                if vstatus == ValidationStatus.WARNING and outcome != RecordOutcome.UNCHANGED:
                    c.warned += 1
            if outcome == RecordOutcome.CONFLICT and kept is not None:
                findings.append(
                    Finding(
                        Rule.REVISION_CONFLICT,
                        Severity.WARNING,
                        "source now reports different values; stored version kept",
                        {
                            "stored_version": kept.version,
                            "stored": {k: str(v) for k, v in stored_values(kept).items()},
                            "incoming": {k: str(v) for k, v in bar.value_fields().items()},
                        },
                    )
                )
            c.bump(outcome)

            if outcome != RecordOutcome.UNCHANGED:
                for f in findings:
                    self._issue(s, run_id, src, ds, f, iid, bar.trade_date)
            s.add(
                IngestionRecord(
                    run_id=run_id,
                    artifact_id=artifact_of[bar.key],
                    record_key=bar.key,
                    instrument_id=iid,
                    trade_date=bar.trade_date,
                    outcome=outcome.value,
                    validation_status=vstatus.value,
                    message="; ".join(f"{f.rule_code}: {f.message}" for f in findings) or None,
                )
            )
        s.flush()
