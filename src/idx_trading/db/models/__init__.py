"""ORM models. Importing this package registers every table on `Base.metadata`."""

from idx_trading.db.models.corporate_actions import CorporateAction, PriceAdjustmentFactor
from idx_trading.db.models.fundamentals import FinancialFact, FinancialReport
from idx_trading.db.models.ingestion import (
    ArtifactFetch,
    DataQualityIssue,
    IngestionRecord,
    IngestionRun,
    RawArtifact,
    ReconciliationResult,
)
from idx_trading.db.models.market import DailyPrice
from idx_trading.db.models.reference import (
    Instrument,
    InstrumentClassification,
    InstrumentShareCount,
    Issuer,
    IssuerNameHistory,
    ListingHistory,
    Sector,
    TickerHistory,
    TradingCalendarDay,
)
from idx_trading.db.models.sources import DataSource, SourcePermission

__all__ = [
    "ArtifactFetch",
    "CorporateAction",
    "DailyPrice",
    "DataQualityIssue",
    "DataSource",
    "FinancialFact",
    "FinancialReport",
    "IngestionRecord",
    "IngestionRun",
    "Instrument",
    "InstrumentClassification",
    "InstrumentShareCount",
    "Issuer",
    "IssuerNameHistory",
    "ListingHistory",
    "PriceAdjustmentFactor",
    "RawArtifact",
    "ReconciliationResult",
    "Sector",
    "SourcePermission",
    "TickerHistory",
    "TradingCalendarDay",
]
