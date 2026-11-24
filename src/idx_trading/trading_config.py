"""Trading assumptions (config/trading.yaml) with an explicit schema."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "trading.yaml"


class Costs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    buy: Decimal = Field(ge=0, lt=Decimal("0.05"))
    sell: Decimal = Field(ge=0, lt=Decimal("0.05"))
    reviewed_at: date
    evidence: str


class Slippage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticks_per_side: int = Field(ge=0, le=10)


class Universe(BaseModel):
    """Price/liquidity floor for research and BUY eligibility (ADR 0014, ADR 0015)."""

    model_config = ConfigDict(extra="forbid")
    min_price_idr: Decimal = Field(default=Decimal(200), ge=0)
    min_adv_idr: Decimal = Field(default=Decimal(1_000_000_000), ge=0)
    note: str | None = None


class TradingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    broker: str
    capital_idr: Decimal = Field(gt=0)
    lot_size: int = Field(gt=0)
    costs: Costs
    slippage: Slippage
    universe: Universe = Universe()

    @property
    def round_trip_cost(self) -> Decimal:
        return self.costs.buy + self.costs.sell


def load_trading_config(path: Path = DEFAULT_PATH) -> TradingConfig:
    return TradingConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
