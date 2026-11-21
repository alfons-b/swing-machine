from datetime import date

import pytest

from idx_trading.db.enums import PermissionPurpose, SourceStatus
from idx_trading.db.models import DataSource
from idx_trading.db.session import session_scope
from idx_trading.errors import SchemaValidationError, SourceNotPermittedError, UnknownSourceError
from idx_trading.registry import is_permitted, load_registry, require_permission, sync_registry
from idx_trading.registry.loader import RegistryFile
from tests.conftest import ROOT, test_registry

P = PermissionPurpose


def test_shipped_registry_reflects_source_review():
    reg = load_registry(ROOT / "config" / "sources.yaml")
    by_id = {s.source_id: s for s in reg.sources}

    def perm(sid, purpose):
        return next((p.status for p in by_id[sid].permissions if p.purpose == purpose), None)

    # Only EODHD (ADR 0008) and the calendar library may be used automatically.
    auto = {s.source_id for s in reg.sources if perm(s.source_id, "automated_ingestion") == "approved"}
    assert auto == {"eodhd", "exchange_calendars_lib", "bca_ir"}
    assert perm("bca_ir", "redistribution") == "denied"
    assert perm("yahoo_finance", "automated_ingestion") == "denied"
    assert by_id["yahoo_finance"].source_status == SourceStatus.BLOCKED
    assert perm("idx_official", "automated_ingestion") == "denied"
    assert perm("eodhd", "redistribution") == "denied"
    for sid in ("ksei", "ojk", "bps", "bank_indonesia"):
        assert by_id[sid].source_status == SourceStatus.PENDING_REVIEW
    required = {
        "source_id",
        "source_name",
        "source_type",
        "source_url",
        "access_method",
        "license_type",
        "terms_reference",
        "rate_limit",
        "update_frequency",
        "reliability_score",
        "source_status",
    }
    assert required <= set(type(reg.sources[0]).model_fields)


def test_approval_without_evidence_is_rejected(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(
        "sources:\n- source_id: x_src\n  source_name: X\n  source_type: t\n"
        "  access_method: api\n  license_type: l\n"
        "  permissions:\n  - purpose: automated_ingestion\n    status: approved\n"
    )
    with pytest.raises(SchemaValidationError, match="evidence_url"):
        load_registry(p)


def test_duplicate_source_ids_rejected():
    spec = test_registry().sources[0].model_dump()
    with pytest.raises(ValueError, match="duplicate source_id"):
        RegistryFile.model_validate({"sources": [spec, spec]})


def test_sync_is_idempotent_and_deactivates_removed_sources(factory):
    reg = test_registry()
    with session_scope(factory) as s:
        r1 = sync_registry(s, reg)
    with session_scope(factory) as s:
        r2 = sync_registry(s, reg)
    assert "manual_csv" in r1.created and not r2.created and not r2.updated

    smaller = RegistryFile(sources=[x for x in reg.sources if x.source_id != "test_feed"])
    with session_scope(factory) as s:
        r3 = sync_registry(s, smaller)
        assert r3.missing_from_file == ["test_feed"]
        assert s.get(DataSource, "test_feed").source_status == SourceStatus.INACTIVE
        assert not is_permitted(s, "test_feed", P.AUTOMATED_INGESTION)


def test_permission_gate(factory):
    with session_scope(factory) as s:
        sync_registry(s, test_registry())
    with session_scope(factory) as s:
        require_permission(s, "manual_csv", P.MANUAL_IMPORT)
        require_permission(s, "test_feed", P.AUTOMATED_INGESTION)
        with pytest.raises(SourceNotPermittedError, match="no permission record"):
            require_permission(s, "manual_csv", P.AUTOMATED_INGESTION)
        with pytest.raises(SourceNotPermittedError, match="pending_review"):
            require_permission(s, "idx_official", P.AUTOMATED_INGESTION)
        with pytest.raises(SourceNotPermittedError):
            require_permission(s, "yahoo_finance", P.AUTOMATED_INGESTION)
        with pytest.raises(UnknownSourceError):
            require_permission(s, "nope", P.AUTOMATED_INGESTION)

        perm = next(
            p for p in s.get(DataSource, "test_feed").permissions if p.purpose == "automated_ingestion"
        )
        perm.valid_until = date(2026, 1, 1)
        s.flush()
        with pytest.raises(SourceNotPermittedError, match="expired"):
            require_permission(s, "test_feed", P.AUTOMATED_INGESTION, on=date(2026, 6, 1))


def test_registry_field_lengths_match_database_columns():
    from enum import StrEnum

    from idx_trading.db.models import DataSource
    from idx_trading.registry.loader import SourceSpec

    for name, field in SourceSpec.model_fields.items():
        if isinstance(field.annotation, type) and issubclass(field.annotation, StrEnum):
            continue  # bounded by the enum (and a CHECK constraint)
        col = DataSource.__table__.columns.get(name)
        length = getattr(getattr(col, "type", None), "length", None)
        if length:
            limits = [m.max_length for m in field.metadata if hasattr(m, "max_length")]
            assert limits == [length], f"{name}: schema {limits} vs column {length}"
