"""FIFO investment calculator. See spec §5 step 9."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class FifoLot:
    transaction_date: Any
    units: Decimal
    unit_price: Decimal

    @property
    def cost(self) -> Decimal:
        return self.units * self.unit_price


@dataclass
class FifoResult:
    units: Decimal
    total_cost: Decimal
    lots: list[FifoLot] = field(default_factory=list)

    @property
    def cost_per_unit(self) -> Decimal:
        if self.units <= 0:
            return Decimal("0")
        return self.total_cost / self.units


def compute_fifo(transactions: list[dict[str, Any]]) -> FifoResult:
    """Run FIFO over a list of (buy/sell) transactions in chronological order.

    Each transaction must include: action ('buy' | 'sell'),
    transaction_date, units, unit_price.
    """
    lots: list[FifoLot] = []

    for txn in transactions:
        action = txn["action"]
        units = Decimal(txn["units"])
        unit_price = Decimal(txn["unit_price"])

        if action == "buy":
            lots.append(FifoLot(txn["transaction_date"], units, unit_price))
        elif action == "sell":
            remaining = units
            while remaining > 0 and lots:
                lot = lots[0]
                if lot.units <= remaining:
                    remaining -= lot.units
                    lots.pop(0)
                else:
                    lot.units -= remaining
                    remaining = Decimal("0")

    total_units = sum((lot.units for lot in lots), Decimal("0"))
    total_cost = sum((lot.cost for lot in lots), Decimal("0"))
    return FifoResult(units=total_units, total_cost=total_cost, lots=lots)
