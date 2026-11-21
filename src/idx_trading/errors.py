"""Domain exceptions."""


class IdxTradingError(Exception):
    """Base class for all domain errors."""


class SourceNotPermittedError(IdxTradingError):
    """Raised when a data source is not approved for the requested use."""


class UnknownSourceError(IdxTradingError):
    """Raised when a source id is not present in the registry."""


class InstrumentResolutionError(IdxTradingError):
    """Raised when a ticker cannot be mapped to a unique instrument on a date."""


class TickerHistoryConflictError(IdxTradingError):
    """Raised when a ticker assignment would overlap an existing assignment."""


class SchemaValidationError(IdxTradingError):
    """Raised when an external payload does not match its explicit schema."""


class SourceHttpError(IdxTradingError):
    """A source answered with an unrecoverable HTTP error (URL is always redacted)."""

    def __init__(self, status: int, url: str, detail: str = "") -> None:
        super().__init__(f"HTTP {status} from {url}{': ' + detail if detail else ''}")
        self.status = status
        self.url = url


class QuotaExceededError(IdxTradingError):
    """Raised before a request that would exceed the configured API call budget."""
