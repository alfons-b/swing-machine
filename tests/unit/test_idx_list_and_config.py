from datetime import date
from decimal import Decimal as Dec

import openpyxl
import pytest
from sqlalchemy import select

from idx_trading.db.enums import InstrumentType, ListingStatus
from idx_trading.db.models import InstrumentShareCount, TickerHistory
from idx_trading.db.session import session_scope
from idx_trading.errors import SchemaValidationError
from idx_trading.fundamentals.bank_publication import parse_date
from idx_trading.instruments import InstrumentService
from idx_trading.instruments.idx_list import import_idx_list
from idx_trading.trading_config import load_trading_config


def _xlsx(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["No", "Kode", "Nama Perusahaan", "Tanggal Pencatatan", "Saham", "Papan Pencatatan"])
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def test_indonesian_month_abbreviations():
    assert parse_date("13 Agt 2008") == date(2008, 8, 13)
    assert parse_date("09 Des 1997") == date(1997, 12, 9)
    assert parse_date("01 Mei 2020") == date(2020, 5, 1)


def test_idx_list_import(seeded, tmp_path):
    with session_scope(seeded) as s:
        svc = InstrumentService(s)
        unk = svc.create_instrument(
            ticker="UNKN",
            issuer_name="PT Unknown",
            instrument_type=InstrumentType.COMMON_STOCK,
            listing_date=None,
        )
        unk_id = unk.instrument_id
    f = _xlsx(
        tmp_path / "list.xlsx",
        [
            [1, "UNKN", "PT Unknown Tbk.", "13 Agt 2008", "1.000.000.000", "Pengembangan"],
            [2, "BBCA", "Bank Central Asia Tbk.", "31 Mei 2000", "123.275.050.000", "Utama"],
            [3, "FCAX", "PT Full Call Tbk.", "02 Jan 2015", "500.000.000", "Pemantauan Khusus"],
            [4, "BAD", "Bad row", "31 Foo 2000", "1", "Utama"],
        ],
    )
    with session_scope(seeded) as s:
        r = import_idx_list(InstrumentService(s), f, date(2026, 11, 22), tmp_path / "raw")
        assert (r.created, r.dated, r.share_counts, len(r.errors)) == (1, 1, 3, 1)
        assert "TLKM" in r.not_in_list  # collected but absent: reported, never delisted
        svc = InstrumentService(s)
        assert svc.status_on(unk_id, date(2026, 11, 20)) == ListingStatus.ACTIVE
        first = s.scalar(select(TickerHistory).where(TickerHistory.instrument_id == unk_id))
        assert first.valid_from == date(2008, 8, 13)  # placeholder start replaced
        m = {x.ticker: x for x in svc.universe_on(date(2026, 11, 20))}
        assert m["UNKN"].eligible
        assert not m["FCAX"].eligible and "special monitoring" in m["FCAX"].reason
        assert (
            s.scalar(
                select(InstrumentShareCount.shares).where(
                    InstrumentShareCount.instrument_id == m["BBCA"].instrument_id
                )
            )
            == 123_275_050_000
        )
    with session_scope(seeded) as s:  # idempotent
        r = import_idx_list(InstrumentService(s), f, date(2026, 11, 22), tmp_path / "raw")
        assert (r.created, r.dated, r.updated, r.share_counts) == (0, 0, 0, 0)


def test_idx_list_requires_columns(seeded, tmp_path):
    wb = openpyxl.Workbook()
    wb.active.append(["Kode", "Nama"])
    wb.save(tmp_path / "x.xlsx")
    with session_scope(seeded) as s, pytest.raises(SchemaValidationError, match="missing column"):
        import_idx_list(InstrumentService(s), tmp_path / "x.xlsx", date(2026, 11, 22), tmp_path / "raw")


def test_trading_config():
    c = load_trading_config()
    assert c.broker == "stockbit" and c.capital_idr == Dec(100_000_000)
    assert c.round_trip_cost == Dec("0.004026")
