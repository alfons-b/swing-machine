"""Re-process payloads from the raw archive instead of calling the source again.

Uses: rebuilding the database after loss (PRD §18.2), re-validating after a rule
change, and saving scarce API quota. Each archived file is checked against its
SHA-256 (the file name) before use, and parsed by the *original* adapter's
`parse`, so results match a live run exactly.

Permission: replaying is processing data we already hold, so the gate checks
`raw_archive` for the original source, not `automated_ingestion`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from pathlib import Path

from idx_trading.db.enums import PermissionPurpose
from idx_trading.ingestion.adapters.base import SourceAdapter
from idx_trading.ingestion.archive import RawArchive
from idx_trading.ingestion.schemas import FetchRequest, ParseResult, RawPayload


class ArchiveReplayAdapter(SourceAdapter):
    purpose = PermissionPurpose.RAW_ARCHIVE

    def __init__(
        self, parser: SourceAdapter, archive_root: Path, kinds: frozenset[str | None] | None = None
    ) -> None:
        """`kinds`: replay only payloads whose metadata `kind`/`mode` is in this set
        (one source folder holds several datasets, e.g. EODHD prices and dividends)."""
        self.parser = parser
        self.source_id = parser.source_id
        self.dataset = parser.dataset
        self.root = Path(archive_root) / parser.source_id
        self.kinds = kinds

    def _entries(self) -> list[tuple[datetime, Path, dict[str, object]]]:
        out = []
        for meta_path in self.root.glob("*/*.meta.json"):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            data_path = meta_path.with_name(meta_path.name.removesuffix(".meta.json"))
            out.append((datetime.fromisoformat(str(meta["retrieved_at"])), data_path, meta))
        out.sort(key=lambda e: (e[0], e[1].name))
        return out

    def fetch(self, request: FetchRequest) -> Iterable[RawPayload]:
        return self._iter(request)

    def _iter(self, request: FetchRequest) -> Iterator[RawPayload]:
        since: date | None = request.start_date
        until: date | None = request.end_date
        for retrieved_at, path, meta in self._entries():
            day = retrieved_at.date()
            if (since and day < since) or (until and day > until):
                continue
            content = path.read_bytes()
            if RawArchive.digest(content) != meta["sha256"]:
                raise OSError(f"archive file {path} does not match its checksum")
            extra = meta.get("metadata")
            extra = dict(extra) if isinstance(extra, dict) else {}
            if self.kinds is not None and (extra.get("kind") or extra.get("mode")) not in self.kinds:
                continue
            yield RawPayload(
                content=content,
                content_type=str(meta["content_type"]),
                origin=str(meta["origin"]),
                retrieved_at=retrieved_at,
                metadata=extra,
            )

    def parse(self, payload: RawPayload) -> ParseResult:
        return self.parser.parse(payload)


#: Payload kinds per dataset, for sources that archive several datasets together.
EOD_KINDS: frozenset[str | None] = frozenset({"per_ticker", "bulk", None})
CORPORATE_ACTION_KINDS: frozenset[str | None] = frozenset({"dividends", "splits", "corporate_actions_csv"})
