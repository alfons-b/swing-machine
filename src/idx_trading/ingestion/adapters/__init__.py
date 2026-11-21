"""Source adapters. One adapter per provider (PRD §6.1)."""

from idx_trading.ingestion.adapters.base import SourceAdapter
from idx_trading.ingestion.adapters.csv_eod import COLUMN_PROFILES, CsvEodAdapter

__all__ = ["COLUMN_PROFILES", "CsvEodAdapter", "SourceAdapter"]
