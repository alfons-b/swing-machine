"""Controlled vocabularies used across the schema."""

from enum import StrEnum


class InstrumentType(StrEnum):
    COMMON_STOCK = "common_stock"
    PREFERRED_STOCK = "preferred_stock"
    WARRANT = "warrant"
    RIGHTS = "rights"
    ETF = "etf"
    REIT = "reit"
    DIRE = "dire"  # Dana Investasi Real Estat
    DERIVATIVE = "derivative"
    OTHER = "other"


class ListingEventType(StrEnum):
    LISTED = "listed"
    SUSPENDED = "suspended"
    SUSPENSION_LIFTED = "suspension_lifted"
    DELISTED = "delisted"
    RELISTED = "relisted"
    BOARD_CHANGE = "board_change"
    SPECIAL_MONITORING_IN = "special_monitoring_in"
    SPECIAL_MONITORING_OUT = "special_monitoring_out"


class ListingStatus(StrEnum):
    UNKNOWN = "unknown"  # no listing events recorded (e.g. listing date not yet sourced)
    NOT_LISTED = "not_listed"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELISTED = "delisted"


class SourceStatus(StrEnum):
    ACTIVE = "active"
    PENDING_REVIEW = "pending_review"
    INACTIVE = "inactive"
    BLOCKED = "blocked"


class PermissionPurpose(StrEnum):
    AUTOMATED_INGESTION = "automated_ingestion"
    MANUAL_IMPORT = "manual_import"
    RAW_ARCHIVE = "raw_archive"
    REDISTRIBUTION = "redistribution"


class PermissionStatus(StrEnum):
    APPROVED = "approved"
    PENDING_REVIEW = "pending_review"
    DENIED = "denied"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class ValidationStatus(StrEnum):
    VALID = "valid"
    WARNING = "warning"
    INVALID = "invalid"


class RecordOutcome(StrEnum):
    INSERTED = "inserted"
    UNCHANGED = "unchanged"
    CONFLICT = "conflict"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class IssueStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    IGNORED = "ignored"


class CorporateActionType(StrEnum):
    CASH_DIVIDEND = "cash_dividend"
    STOCK_DIVIDEND = "stock_dividend"
    BONUS_SHARES = "bonus_shares"
    STOCK_SPLIT = "stock_split"
    REVERSE_SPLIT = "reverse_split"
    RIGHTS_ISSUE = "rights_issue"
    PRIVATE_PLACEMENT = "private_placement"
    MERGER = "merger"
    TICKER_CHANGE = "ticker_change"
    OTHER = "other"


class AdjustmentType(StrEnum):
    SPLIT = "split"
    TOTAL_RETURN = "total_return"


class FiscalPeriod(StrEnum):
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q4 = "Q4"
    H1 = "H1"
    FY = "FY"


class VolumeBasis(StrEnum):
    RAW = "raw"
    #: Adjusted by the vendor for splits known at retrieval time (EODHD).
    SPLIT_ADJUSTED_AT_RETRIEVAL = "split_adjusted_at_retrieval"


class ReconciliationStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
