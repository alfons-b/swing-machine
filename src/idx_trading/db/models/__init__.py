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
from idx_trading.db.models.modelling import ModelVersion
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
from idx_trading.db.models.signals import ImmutableRecordError, Signal, SignalRun
from idx_trading.db.models.sources import DataSource, SourcePermission

__all__ = [
    "ArtifactFetch",
    "CorporateAction",
    "DailyPrice",
    "DataQualityIssue",
    "DataSource",
    "FinancialFact",
    "FinancialReport",
    "ImmutableRecordError",
    "IngestionRecord",
    "IngestionRun",
    "Instrument",
    "InstrumentClassification",
    "InstrumentShareCount",
    "Issuer",
    "IssuerNameHistory",
    "ListingHistory",
    "ModelVersion",
    "PriceAdjustmentFactor",
    "RawArtifact",
    "ReconciliationResult",
    "Sector",
    "Signal",
    "SignalRun",
    "SourcePermission",
    "TickerHistory",
    "TradingCalendarDay",
]
