"""Permission gate: every adapter must pass through here before touching a source."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from idx_trading.clock import exchange_today
from idx_trading.db.enums import PermissionPurpose, PermissionStatus, SourceStatus
from idx_trading.db.models import DataSource
from idx_trading.errors import SourceNotPermittedError, UnknownSourceError

_USABLE_STATUSES = {SourceStatus.ACTIVE.value}


def _check(session: Session, source_id: str, purpose: PermissionPurpose, on: date | None) -> str | None:
    """Return None when permitted, otherwise the reason it is not."""
    source = session.get(DataSource, source_id)
    if source is None:
        raise UnknownSourceError(f"source '{source_id}' is not in the registry")
    if source.source_status not in _USABLE_STATUSES:
        return f"source status is '{source.source_status}'"
    perm = next((p for p in source.permissions if p.purpose == purpose.value), None)
    if perm is None:
        return f"no permission record for purpose '{purpose.value}'"
    if perm.status != PermissionStatus.APPROVED.value:
        return f"permission for '{purpose.value}' is '{perm.status}'"
    today = on or exchange_today()
    if perm.valid_until is not None and perm.valid_until < today:
        return f"permission for '{purpose.value}' expired on {perm.valid_until.isoformat()}"
    return None


def is_permitted(
    session: Session, source_id: str, purpose: PermissionPurpose, on: date | None = None
) -> bool:
    return _check(session, source_id, purpose, on) is None


def require_permission(
    session: Session, source_id: str, purpose: PermissionPurpose, on: date | None = None
) -> None:
    reason = _check(session, source_id, purpose, on)
    if reason is not None:
        raise SourceNotPermittedError(f"source '{source_id}' may not be used for '{purpose.value}': {reason}")
