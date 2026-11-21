"""ORM models. Importing this package registers every table on `Base.metadata`."""

from idx_trading.db.models.corporate_actions import CorporateAction, PriceAdjustmentFactor
from idx_trading.db.models.fundamentals import FinancialFact, FinancialReport
from idx_trading.db.models.ingestion import (
    DataQualityIssue,
    IngestionRecord,
    IngestionRun,
    RawArtifact,
)
from idx_trading.db.models.market import DailyPrice
from idx_trading.db.models.reference import (
    Instrument,
    InstrumentClassification,
    Issuer,
    IssuerNameHistory,
    ListingHistory,
    Sector,
    TickerHistory,
    TradingCalendarDay,
)
from idx_trading.db.models.sources import DataSource, SourcePermission

__all__ = [
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
    "Issuer",
    "IssuerNameHistory",
    "ListingHistory",
    "PriceAdjustmentFactor",
    "RawArtifact",
    "Sector",
    "SourcePermission",
    "TickerHistory",
    "TradingCalendarDay",
]
