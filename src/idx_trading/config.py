"""Application settings, loaded from environment variables / `.env`.

Secrets are never hard-coded; every value can be overridden via `IDX_*` env vars.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IDX_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://idx:idx@localhost:5432/idx_trading"
    data_dir: Path = Path("./data")
    sources_file: Path = Path("./config/sources.yaml")
    log_level: str = "INFO"
    log_json: bool = False

    exchange_timezone: str = "Asia/Jakarta"
    calendar_code: str = "XIDX"
    eod_ready_buffer_minutes: int = Field(default=90, ge=0, le=24 * 60)
    extreme_move_threshold: float = Field(default=0.35, gt=0, lt=10)

    # EODHD (ADR 0008). The token is a secret: env/.env or a CI secret only.
    eodhd_api_token: SecretStr | None = None
    eodhd_base_url: str = "https://eodhd.com/api"
    eodhd_exchange: str = "JK"
    #: API calls the system may spend per day. Free plan = 20; paid plans = 100,000.
    eodhd_daily_call_budget: int = Field(default=20, ge=1)
    #: History reachable on the plan; the free plan serves the past year only.
    eodhd_history_days: int = Field(default=365, ge=1)

    @property
    def raw_archive_dir(self) -> Path:
        return self.data_dir / "raw"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
