"""Import the IDX stock list ("Daftar Saham", downloaded by the owner from idx.co.id).

Columns: No, Kode, Nama Perusahaan, Tanggal Pencatatan ("09 Des 1997"), Saham
("1.924.688.333"), Papan Pencatatan (Utama, Pengembangan, Akselerasi, Ekonomi Baru,
Pemantauan Khusus).

Effects (idempotent):
* listing date -> a `listed` event and `first_listing_date`; for instruments created
  without one (EODHD import, "listing date unknown") the first ticker interval now starts
  on the listing date;
* board -> `instruments.board` (special-monitoring stocks are not BUY-eligible);
* listed shares -> `instrument_share_counts` as of the list's date;
* missing instruments are created. Instruments absent from the list are only reported:
  delisting needs a date and is never inferred.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select

from idx_trading.db.enums import InstrumentType, ListingEventType, PermissionPurpose
from idx_trading.db.models import Instrument, InstrumentShareCount, RawArtifact, TickerHistory
from idx_trading.errors import SchemaValidationError
from idx_trading.fundamentals.bank_publication import parse_date
from idx_trading.ingestion.archive import RawArchive
from idx_trading.ingestion.schemas import RawPayload
from idx_trading.instruments.service import InstrumentService, normalize_ticker
from idx_trading.registry import require_permission

UNKNOWN_START = date(1977, 8, 10)
HEADERS = ("Kode", "Nama Perusahaan", "Tanggal Pencatatan", "Saham", "Papan Pencatatan")


@dataclass
class IdxListReport:
    rows: int = 0
    created: int = 0
    dated: int = 0  # listing date added to an instrument that had none
    updated: int = 0
    unchanged: int = 0
    share_counts: int = 0
    errors: list[str] = field(default_factory=list)
    not_in_list: list[str] = field(default_factory=list)
    boards: dict[str, int] = field(default_factory=dict)


def _rows(path: Path) -> list[dict[str, object]]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        it = wb.worksheets[0].iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(it)]
        missing = [h for h in HEADERS if h not in header]
        if missing:
            raise SchemaValidationError(f"IDX stock list: missing column(s) {missing}")
        return [dict(zip(header, r, strict=False)) for r in it if any(v is not None for v in r)]
    finally:
        wb.close()


def import_idx_list(
    svc: InstrumentService, path: Path, as_of: date, archive_root: Path, source_id: str = "manual_csv"
) -> IdxListReport:
    s = svc.s
    require_permission(s, source_id, PermissionPurpose.MANUAL_IMPORT)
    content = path.read_bytes()
    stored = RawArchive(archive_root).store(
        source_id,
        RawPayload(
            content,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            path.name,
            datetime.now(tz=UTC),
            {"kind": "idx_stock_list", "as_of": as_of.isoformat()},
        ),
    )
    art = s.scalar(
        select(RawArtifact).where(RawArtifact.source_id == source_id, RawArtifact.sha256 == stored.sha256)
    )
    if art is None:
        art = RawArtifact(
            source_id=source_id,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            storage_path=stored.relative_path,
            origin=path.name,
            retrieved_at=datetime.now(tz=UTC),
            extra={"kind": "idx_stock_list", "as_of": as_of.isoformat()},
        )
        s.add(art)
        s.flush()

    rep = IdxListReport()
    seen: set[int] = set()
    for i, row in enumerate(_rows(path), start=2):
        rep.rows += 1
        try:
            ticker = normalize_ticker(str(row["Kode"]))
            name = str(row["Nama Perusahaan"]).strip()
            listed = parse_date(str(row["Tanggal Pencatatan"]))
            shares = int(str(row["Saham"]).replace(".", "").replace(",", "").strip())
            board = str(row["Papan Pencatatan"]).strip() or None
        except (ValueError, KeyError) as exc:
            rep.errors.append(f"row {i}: {exc}")
            continue
        rep.boards[board or "?"] = rep.boards.get(board or "?", 0) + 1
        iid = svc.try_resolve(ticker, as_of)
        if iid is None:
            inst = svc.create_instrument(
                ticker=ticker,
                issuer_name=name,
                instrument_type=InstrumentType.COMMON_STOCK,
                listing_date=listed,
                source_id=source_id,
            )
            inst.board = board
            rep.created += 1
        else:
            found = s.get(Instrument, iid)
            assert found is not None
            inst = found
            changed = False
            if inst.first_listing_date is None:
                first = s.scalar(
                    select(TickerHistory)
                    .where(TickerHistory.instrument_id == iid)
                    .order_by(TickerHistory.valid_from)
                    .limit(1)
                )
                if first is not None and first.valid_from == UNKNOWN_START and first.ticker == ticker:
                    first.valid_from = listed
                    first.reason = "initial listing (date from IDX stock list)"
                rep.dated += 1
                changed = True
            if not any(
                e.event_type == ListingEventType.LISTED.value and e.effective_date == listed
                for e in inst.listing_events
            ):
                svc.add_listing_event(iid, ListingEventType.LISTED, listed, source_id=source_id)
                changed = True
            if (
                inst.first_listing_date != listed
                and inst.first_listing_date is not None
                and listed < inst.first_listing_date
            ):
                inst.first_listing_date = listed
                changed = True
            if board and inst.board != board:
                inst.board = board
                changed = True
            if name and inst.issuer.name != name:
                inst.issuer.name = name
                changed = True
            rep.updated += int(changed)
            rep.unchanged += int(not changed)
        seen.add(inst.instrument_id)
        exists = s.scalar(
            select(InstrumentShareCount).where(
                InstrumentShareCount.instrument_id == inst.instrument_id,
                InstrumentShareCount.as_of == as_of,
                InstrumentShareCount.source_id == source_id,
            )
        )
        if exists is None:
            s.add(
                InstrumentShareCount(
                    instrument_id=inst.instrument_id,
                    as_of=as_of,
                    shares=shares,
                    source_id=source_id,
                    artifact_id=art.artifact_id,
                )
            )
            rep.share_counts += 1
    for m in svc.collection_universe_on(as_of):
        if m.instrument_id not in seen:
            rep.not_in_list.append(m.ticker)
    s.flush()
    return rep
