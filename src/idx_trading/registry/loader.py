"""Load `config/sources.yaml` with an explicit schema and sync it into the database."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from idx_trading.db.enums import PermissionPurpose, PermissionStatus, SourceStatus
from idx_trading.db.models import DataSource, SourcePermission
from idx_trading.errors import SchemaValidationError


class PermissionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: PermissionPurpose
    status: PermissionStatus = PermissionStatus.PENDING_REVIEW
    reviewed_at: date | None = None
    reviewed_by: str | None = None
    evidence_url: str | None = None
    valid_until: date | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _approval_needs_evidence(self) -> PermissionSpec:
        if self.status == PermissionStatus.APPROVED and not (
            self.reviewed_at and self.reviewed_by and self.evidence_url
        ):
            raise ValueError(
                f"approved permission '{self.purpose}' requires reviewed_at, reviewed_by and evidence_url"
            )
        return self


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$", max_length=64)
    # Lengths mirror the `data_sources` columns so a bad file fails at load time.
    source_name: str = Field(max_length=200)
    source_type: str = Field(max_length=50)
    source_url: str | None = None
    access_method: str = Field(max_length=50)
    license_type: str = Field(max_length=100)
    terms_reference: str | None = None
    rate_limit: str | None = Field(default=None, max_length=100)
    rate_limit_per_minute: int | None = Field(default=None, gt=0)
    update_frequency: str | None = Field(default=None, max_length=50)
    reliability_score: float | None = Field(default=None, ge=0, le=1)
    source_status: SourceStatus = SourceStatus.PENDING_REVIEW
    notes: str | None = None
    permissions: list[PermissionSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_purposes(self) -> SourceSpec:
        purposes = [p.purpose for p in self.permissions]
        if len(purposes) != len(set(purposes)):
            raise ValueError(f"duplicate permission purpose in source '{self.source_id}'")
        return self


class RegistryFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[SourceSpec]

    @model_validator(mode="after")
    def _unique_ids(self) -> RegistryFile:
        ids = [s.source_id for s in self.sources]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate source_id(s): {sorted(dupes)}")
        return self


def load_registry(path: Path) -> RegistryFile:
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    try:
        return RegistryFile.model_validate(raw)
    except ValidationError as exc:
        raise SchemaValidationError(f"invalid source registry {path}: {exc}") from exc


@dataclass
class SyncReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    missing_from_file: list[str] = field(default_factory=list)


_SOURCE_FIELDS = (
    "source_name",
    "source_type",
    "source_url",
    "access_method",
    "license_type",
    "terms_reference",
    "rate_limit",
    "rate_limit_per_minute",
    "update_frequency",
    "reliability_score",
    "source_status",
    "notes",
)
_PERMISSION_FIELDS = ("status", "reviewed_at", "reviewed_by", "evidence_url", "valid_until", "notes")


def sync_registry(session: Session, registry: RegistryFile) -> SyncReport:
    """Idempotently mirror the registry file into `data_sources`/`source_permissions`.

    Operational fields (last_successful_ingestion, ...) are never touched.
    Sources present in the DB but absent from the file are reported, not deleted
    (they may be referenced by historical data); their permissions are revoked by
    marking the source `inactive`.
    """
    report = SyncReport()
    existing = {s.source_id: s for s in session.scalars(select(DataSource))}

    for spec in registry.sources:
        row = existing.get(spec.source_id)
        changed = False
        if row is None:
            row = DataSource(source_id=spec.source_id)
            session.add(row)
            report.created.append(spec.source_id)
        for name in _SOURCE_FIELDS:
            new = getattr(spec, name)
            new = new.value if hasattr(new, "value") else new
            if getattr(row, name, None) != new:
                setattr(row, name, new)
                changed = True

        perms = {p.purpose: p for p in row.permissions}
        wanted = {p.purpose.value: p for p in spec.permissions}
        for purpose, pspec in wanted.items():
            prow = perms.get(purpose)
            if prow is None:
                prow = SourcePermission(purpose=purpose)
                row.permissions.append(prow)
                changed = True
            for name in _PERMISSION_FIELDS:
                new = getattr(pspec, name)
                new = new.value if hasattr(new, "value") else new
                if getattr(prow, name, None) != new:
                    setattr(prow, name, new)
                    changed = True
        for purpose, prow in perms.items():
            if purpose not in wanted:
                row.permissions.remove(prow)
                changed = True

        if spec.source_id not in report.created:
            (report.updated if changed else report.unchanged).append(spec.source_id)

    file_ids = {s.source_id for s in registry.sources}
    for source_id, row in existing.items():
        if source_id not in file_ids:
            report.missing_from_file.append(source_id)
            if row.source_status != SourceStatus.INACTIVE:
                row.source_status = SourceStatus.INACTIVE
    session.flush()
    return report
