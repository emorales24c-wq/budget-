"""Risk: owns every exit and every reason not to enter.

* Hard stop at -6% of cost basis, measured on the *executable* sell quote (what you'd
  actually get back after impact and fees), not on a chart price.
* Trailing stop arms only after +12%, then exits if value falls trail_distance off the peak.
* No averaging down: Executor asks Risk before every buy; any existing/closed mint = no.
* No revenge trades: a losing exit bans that mint for the session and freezes all new
  entries for a cooldown. N consecutive losses or a session drawdown halts entries.
"""
from __future__ import annotations

import asyncio
import time

from ..config import RiskRules
from ..models import ExitOrder, Position
from .base import Agent


class Risk(Agent):
    name = "Risk"

    def __init__(self, log_q, gateway, rules: RiskRules, portfolio, exit_q: asyncio.Queue[ExitOrder],
                 slippage_bps: int):
        super().__init__(log_q)
        self.gw, self.rules, self.pf, self.exit_q = gateway, rules, portfolio, exit_q
        self.slippage_bps = slippage_bps
        self.banned: set[str] = set()
        self.cooldown_until = 0.0
        self.consecutive_losses = 0
        self.halted_reason: str | None = None
        self._pending_exit: set[str] = set()
        self._quote_misses: dict[str, int] = {}
        self.session_start_equity: float | None = None

    # ---------------------------------------------------------------- entry gate
    def entry_veto(self, mint: str) -> str | None:
        if self.halted_reason:
            return f"entries halted: {self.halted_reason}"
        if mint in self.pf.positions:
            return "already holding this mint (no averaging down / no adding)"
        if mint in self.banned:
            return "mint previously traded this session (no re-entry, no revenge)"
        left = self.cooldown_until - time.time()
        if left > 0:
            return f"post-loss cooldown, {left/60:.1f} min left (no revenge trades)"
        return None

    # ---------------------------------------------------------------- outcomes
    def on_closed(self, mint: str, pnl_sol: float) -> None:
        self.banned.add(mint)
        self._pending_exit.discard(mint)
        if pnl_sol < 0:
            self.consecutive_losses += 1
            self.cooldown_until = time.time() + self.rules.loss_cooldown_sec
            self.emit("risk", mint, f"loss recorded; entries frozen {self.rules.loss_cooldown_sec//60} min; "
                      f"{self.consecutive_losses} consecutive", consecutive=self.consecutive_losses)
            if self.consecutive_losses >= self.rules.max_consecutive_losses:
                self._halt(f"{self.consecutive_losses} consecutive losses")
        else:
            self.consecutive_losses = 0

    def on_exit_failed(self, mint: str) -> None:
        self._pending_exit.discard(mint)   # let the next poll re-issue it

    def _halt(self, why: str) -> None:
        if not self.halted_reason:
            self.halted_reason = why
            self.status = f"HALTED: {why}"
            self.emit("risk", None, f"HALT new entries: {why}. Open positions still managed.")

    # ---------------------------------------------------------------- monitor
    async def run(self) -> None:
        self.status = "monitoring"
        while True:
            await self.check_all()
            await asyncio.sleep(self.rules.poll_interval_sec)

    async def check_all(self) -> None:
        for pos in list(self.pf.positions.values()):
            await self._check(pos)
        eq = self.pf.equity()
        if self.session_start_equity is None:
            self.session_start_equity = eq
        dd = (1 - eq / self.session_start_equity) * 100 if self.session_start_equity else 0.0
        if dd >= self.rules.daily_max_drawdown_pct:
            self._halt(f"session drawdown {dd:.1f}% >= {self.rules.daily_max_drawdown_pct}%")
        self.pf.record_equity()
        if not self.halted_reason:
            self.status = f"monitoring {len(self.pf.positions)} position(s)"

    async def _check(self, pos: Position) -> None:
        if pos.mint in self._pending_exit:
            return
        q = await self.gw.quote_sell(pos.mint, pos.tokens, self.slippage_bps)
        if not q or q.out_amount <= 0:
            n = self._quote_misses.get(pos.mint, 0) + 1
            self._quote_misses[pos.mint] = n
            if n >= 3:  # no route / liquidity pulled: try to get out at any price
                self._exit(pos, f"no executable sell quote {n}x in a row (liquidity pulled?) - emergency exit",
                           pos.last_price_sol)
            return
        self._quote_misses[pos.mint] = 0
        pos.last_price_sol = q.out_amount / pos.tokens
        pos.peak_price_sol = max(pos.peak_price_sol, pos.last_price_sol)
        pnl = pos.pnl_pct
        peak_pnl = (pos.peak_price_sol / pos.entry_price_sol - 1) * 100

        if pnl <= self.rules.hard_stop_pct:
            self._exit(pos, f"HARD STOP: {pnl:+.2f}% <= {self.rules.hard_stop_pct}%", pos.last_price_sol)
            return
        if not pos.trail_armed and peak_pnl >= self.rules.trail_activate_pct:
            pos.trail_armed = True
            self.emit("risk", pos.mint, f"trailing stop ARMED at {peak_pnl:+.2f}% "
                      f"(trail {self.rules.trail_distance_pct}% off peak)", symbol=pos.symbol)
        if pos.trail_armed:
            off_peak = (1 - pos.last_price_sol / pos.peak_price_sol) * 100
            if off_peak >= self.rules.trail_distance_pct:
                self._exit(pos, f"TRAILING STOP: {off_peak:.2f}% off peak {peak_pnl:+.2f}%, locking {pnl:+.2f}%",
                           pos.last_price_sol)

    def _exit(self, pos: Position, reason: str, px: float) -> None:
        self._pending_exit.add(pos.mint)
        self.emit("risk", pos.mint, reason, symbol=pos.symbol, pnl_pct=round(pos.pnl_pct, 2))
        self.exit_q.put_nowait(ExitOrder(pos.mint, reason, px))
