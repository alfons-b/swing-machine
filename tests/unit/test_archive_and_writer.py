from datetime import UTC, datetime
from decimal import Decimal

import pytest

from idx_trading.ingestion.archive import RawArchive
from idx_trading.ingestion.schemas import RawPayload
from idx_trading.ingestion.writer import record_hash


def _p(data: bytes) -> RawPayload:
    return RawPayload(data, "text/csv", "f.csv", datetime(2026, 11, 20, tzinfo=UTC), {"k": 1})


def test_archive_is_content_addressed_and_idempotent(tmp_path):
    arc = RawArchive(tmp_path)
    a = arc.store("src", _p(b"hello"))
    b = arc.store("src", _p(b"hello"))
    c = arc.store("src", _p(b"world"))
    assert a.newly_written and not b.newly_written
    assert a.relative_path == b.relative_path != c.relative_path
    assert arc.read(a.relative_path) == b"hello" and arc.verify(a.relative_path, a.sha256)
    assert (tmp_path / (a.relative_path + ".meta.json")).exists()
    assert not list(tmp_path.rglob(".tmp-*"))


def test_archive_detects_corruption(tmp_path):
    arc = RawArchive(tmp_path)
    a = arc.store("src", _p(b"hello"))
    (tmp_path / a.relative_path).write_bytes(b"tampered")
    assert not arc.verify(a.relative_path, a.sha256)
    with pytest.raises(OSError, match="corruption"):
        arc.store("src", _p(b"hello"))


def test_record_hash_ignores_decimal_representation():
    assert record_hash({"close": Decimal("1500")}) == record_hash({"close": Decimal("1500.0000")})
    assert record_hash({"close": Decimal("1500")}) != record_hash({"close": Decimal("1501")})
    assert record_hash({"close": Decimal("1500")}) != record_hash({"close": Decimal("1500"), "volume": 0})
