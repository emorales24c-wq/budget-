from __future__ import annotations

import time
from dataclasses import dataclass

from .models import Position


@dataclass
class ClosedTrade:
    mint: str
    symbol: str
    cost_sol: float
    proceeds_sol: float
    opened_at: float
    closed_at: float
    entry_reason: str
    exit_reason: str

    @property
    def pnl_sol(self) -> float:
        return self.proceeds_sol - self.cost_sol

    @property
    def pnl_pct(self) -> float:
        return (self.proceeds_sol / self.cost_sol - 1) * 100 if self.cost_sol else 0.0


class Portfolio:
    """Single source of truth for balances. Only the Executor mutates it."""

    def __init__(self, cash_sol: float):
        self.cash_sol = cash_sol
        self.start_sol = cash_sol
        self.positions: dict[str, Position] = {}
        self.closed: list[ClosedTrade] = []
        self.equity_curve: list[tuple[float, float]] = [(time.time(), cash_sol)]

    def equity(self) -> float:
        return self.cash_sol + sum(p.value_sol for p in self.positions.values())

    def record_equity(self) -> None:
        self.equity_curve.append((time.time(), self.equity()))
        if len(self.equity_curve) > 20_000:
            self.equity_curve = self.equity_curve[::2]
