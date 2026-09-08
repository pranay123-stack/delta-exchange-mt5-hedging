"""Account state value objects, shared by both venue families.

MT5 brokers and crypto exchanges use the same underlying accounting identity
even though they name things differently:

    equity        = balance + unrealised PnL
    free_margin   = equity - used_margin
    margin_level  = equity / used_margin * 100      (percent; inf when flat)

The broker's stop-out level and the exchange's liquidation threshold are both
expressed as a floor on ``margin_level``, so one model covers both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .market import utcnow
from .numeric import ZERO, safe_div

#: Sentinel margin level for a flat account (no margin in use).
INFINITE_MARGIN_LEVEL = Decimal("999999")


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Point-in-time account state for one venue."""

    venue: str
    currency: str
    balance: Decimal
    equity: Decimal
    used_margin: Decimal
    free_margin: Decimal
    margin_level: Decimal
    maintenance_margin: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal = ZERO
    funding_net: Decimal = ZERO
    open_positions: int = 0
    timestamp: datetime = field(default_factory=utcnow)
    is_connected: bool = True

    @classmethod
    def build(
        cls,
        *,
        venue: str,
        currency: str,
        balance: Decimal,
        used_margin: Decimal,
        maintenance_margin: Decimal,
        unrealized_pnl: Decimal,
        realized_pnl: Decimal = ZERO,
        fees_paid: Decimal = ZERO,
        funding_net: Decimal = ZERO,
        open_positions: int = 0,
        is_connected: bool = True,
    ) -> AccountSnapshot:
        equity = balance + unrealized_pnl
        free_margin = equity - used_margin
        margin_level = (
            INFINITE_MARGIN_LEVEL
            if used_margin <= ZERO
            else safe_div(equity, used_margin) * Decimal(100)
        )
        return cls(
            venue=venue,
            currency=currency,
            balance=balance,
            equity=equity,
            used_margin=used_margin,
            free_margin=free_margin,
            margin_level=margin_level,
            maintenance_margin=maintenance_margin,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=realized_pnl,
            fees_paid=fees_paid,
            funding_net=funding_net,
            open_positions=open_positions,
            is_connected=is_connected,
        )

    @property
    def is_flat(self) -> bool:
        return self.used_margin == ZERO

    @property
    def margin_utilization_pct(self) -> Decimal:
        return safe_div(self.used_margin, self.equity) * Decimal(100)

    def can_afford(self, additional_margin: Decimal) -> bool:
        return self.free_margin >= additional_margin
