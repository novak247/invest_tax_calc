from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class Trade:
    source: str
    source_id: str
    action: str
    kind: str
    traded_at: datetime
    instrument_key: str
    ticker: str = ""
    isin: str = ""
    name: str = ""
    quantity: Decimal = Decimal("0")
    gross: Money = field(default_factory=lambda: Money(Decimal("0"), "CZK"))
    fees: tuple[Money, ...] = ()
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class TaxLot:
    source_id: str
    instrument_key: str
    ticker: str
    isin: str
    name: str
    acquired_at: datetime
    quantity_total: Decimal
    quantity_remaining: Decimal
    cost_czk_total: Decimal

    @property
    def cost_per_unit_czk(self) -> Decimal:
        if self.quantity_remaining == 0:
            return Decimal("0")
        return self.cost_czk_total / self.quantity_total
