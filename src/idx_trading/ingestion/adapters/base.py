"""Adapter contract.

An adapter knows *how* to talk to one provider and how to turn its raw bytes
into `EodBar`s. It must not write to the database, decide on data quality, or
skip the permission gate — the pipeline owns those responsibilities.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from idx_trading.db.enums import PermissionPurpose
from idx_trading.ingestion.schemas import FetchRequest, ParseResult, RawPayload


class SourceAdapter(ABC):
    #: Registry id; must exist in `config/sources.yaml`.
    source_id: str
    #: Dataset produced, e.g. "eod_prices".
    dataset: str = "eod_prices"
    #: Permission the pipeline must verify before calling `fetch`.
    purpose: PermissionPurpose = PermissionPurpose.AUTOMATED_INGESTION

    @abstractmethod
    def fetch(self, request: FetchRequest) -> Iterable[RawPayload]:
        """Retrieve raw payloads. Must respect the source's rate limits."""

    @abstractmethod
    def parse(self, payload: RawPayload) -> ParseResult:
        """Convert one raw payload into bars; never raise on a bad row — report it."""
