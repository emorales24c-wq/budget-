"""Executor: the only agent that can move money.

It accepts exactly two message types: TradeTicket (from Analyst) and ExitOrder (from
Risk). A ticket is re-verified from scratch: Filter's HMAC seal, every required check
present and passed, score >= threshold, Risk has no veto. Size is hard-capped at
max_position_pct of the *current* SOL balance.
"""
from __future__ import annotations

import asyncio
import time

from ..config import Config
from ..models import ExitOrder, Position, Seal, TradeTicket
from ..portfolio import ClosedTrade, Portfolio
from .base import Agent

NETWORK_FEE_SOL = 0.000005
EXIT_SLIPPAGE_LADDER_BPS = (300, 800, 1500, 3000)   # escalate to get OUT; never used for entries


class PaperBroker:
    def __init__(self, priority_fee_sol: float):
        self.fee = NETWORK_FEE_SOL + priority_fee_sol

    async def buy(self, gw, mint: str, sol_in: float, quote) -> tuple[float, float] | None:
        return quote.out_amount, sol_in + self.fee          # tokens, total SOL spent

    async def sell(self, gw, mint: str, tokens: float, quote) -> float | None:
        return max(quote.out_amount - self.fee, 0.0)          # SOL received


class LiveBroker:
    def __init__(self, keypair):
        self.kp = keypair
        self.owner = str(keypair.pubkey())

    async def buy(self, gw, mint, sol_in, quote):
        before_sol = await gw.sol_balance(self.owner)
        before_tok = await gw.token_balance(self.owner, mint) or 0.0
        if before_sol is None or not await gw.swap(quote, self.kp):
            return None
        after_sol = await gw.sol_balance(self.owner)
        after_tok = await gw.token_balance(self.owner, mint)
        if after_sol is None or after_tok is None:
            return None
        return after_tok - before_tok, before_sol - after_sol

    async def sell(self, gw, mint, tokens, quote):
        before = await gw.sol_balance(self.owner)
        if before is None or not await gw.swap(quote, self.kp):
            return None
        after = await gw.sol_balance(self.owner)
        return (after - before) if after is not None else None


class Executor(Agent):
    name = "Executor"

    def __init__(self, log_q, gateway, cfg: Config, seal: Seal, portfolio: Portfolio, risk,
                 ticket_q: asyncio.Queue[TradeTicket], exit_q: asyncio.Queue[ExitOrder], broker):
        super().__init__(log_q)
        self.gw, self.cfg, self.seal, self.pf, self.risk = gateway, cfg, seal, portfolio, risk
        self.ticket_q, self.exit_q, self.broker = ticket_q, exit_q, broker
        self._lock = asyncio.Lock()

    async def run(self) -> None:
        self.status = "ready"
        await asyncio.gather(self._entries(), self._exits())

    async def _entries(self) -> None:
        while True:
            t = await self.ticket_q.get()
            async with self._lock:
                await self.enter(t)

    async def _exits(self) -> None:
        while True:
            o = await self.exit_q.get()
            async with self._lock:          # exits and entries never interleave on the balance
                await self.exit(o)

    # ------------------------------------------------------------------ guards
    def _verify(self, t: TradeTicket) -> str | None:
        fp = t.filter_pass
        if not self.seal.verify(fp):
            return "ticket seal invalid: did not come from Filter"
        by_name = {c.name: c for c in fp.checks}
        for req in Seal.REQUIRED_CHECKS:
            if req not in by_name:
                return f"ticket missing required check {req}"
            if not by_name[req].passed:
                return f"ticket has failed check {req}"
        if t.score < self.cfg.analyst.min_score:
            return f"score {t.score:.2f} < {self.cfg.analyst.min_score}"
        if len(self.pf.positions) >= self.cfg.execution.max_open_positions:
            return f"max open positions ({self.cfg.execution.max_open_positions}) reached"
        return self.risk.entry_veto(fp.candidate.mint)

    def size_for(self, cash: float) -> float:
        return cash * self.cfg.execution.max_position_pct

    # ------------------------------------------------------------------ entry
    async def enter(self, t: TradeTicket) -> None:
        c = t.filter_pass.candidate
        ex = self.cfg.execution
        veto = self._verify(t)
        if veto:
            self.emit("reject", c.mint, f"executor refused: {veto}", symbol=c.symbol)
            return
        size = self.size_for(self.pf.cash_sol)
        # leave room for fees inside the 8% so total spend never exceeds the cap
        sol_in = size - (NETWORK_FEE_SOL + ex.priority_fee_lamports / 1e9)
        if sol_in < ex.min_trade_sol:
            self.emit("reject", c.mint, f"executor refused: size {sol_in:.4f} SOL below minimum", symbol=c.symbol)
            return
        self.status = f"buying {c.symbol}"
        q = await self.gw.quote_buy(c.mint, sol_in, ex.max_slippage_bps)
        if not q or q.out_amount <= 0:
            self.emit("reject", c.mint, "executor refused: no buy route/quote", symbol=c.symbol)
            self.status = "ready"
            return
        if q.price_impact_pct > ex.max_price_impact_pct:
            self.emit("reject", c.mint, f"executor refused: price impact {q.price_impact_pct:.2f}% > "
                      f"{ex.max_price_impact_pct}%", symbol=c.symbol)
            self.status = "ready"
            return
        fill = await self.broker.buy(self.gw, c.mint, sol_in, q)
        self.status = "ready"
        if not fill or fill[0] <= 0:
            self.emit("error", c.mint, "buy failed / not confirmed; no position opened", symbol=c.symbol)
            return
        tokens, spent = fill
        if spent > size * 1.001:   # should be impossible; if live fill overspent, say so loudly
            self.emit("error", c.mint, f"spent {spent:.4f} SOL exceeds cap {size:.4f}", symbol=c.symbol)
        self.pf.cash_sol -= spent
        px = spent / tokens
        self.pf.positions[c.mint] = Position(
            mint=c.mint, symbol=c.symbol, entry_price_sol=px, tokens=tokens, cost_sol=spent,
            opened_at=time.time(), peak_price_sol=px, last_price_sol=px, entry_reason=t.reason)
        self.emit("buy", c.mint,
                  f"BUY {c.symbol} {spent:.4f} SOL ({spent / (self.pf.cash_sol + spent) * 100:.2f}% of balance) | "
                  f"{t.reason} | impact {q.price_impact_pct:.2f}%",
                  symbol=c.symbol, sol=spent, tokens=tokens, score=t.score, source=c.source,
                  checks=[(x.name, x.detail) for x in t.filter_pass.checks], features=t.score_breakdown)

    # ------------------------------------------------------------------ exit
    async def exit(self, o: ExitOrder) -> None:
        pos = self.pf.positions.get(o.mint)
        if not pos:
            return
        self.status = f"selling {pos.symbol}"
        proceeds = None
        for bps in EXIT_SLIPPAGE_LADDER_BPS:
            q = await self.gw.quote_sell(o.mint, pos.tokens, bps)
            if q and q.out_amount > 0:
                proceeds = await self.broker.sell(self.gw, o.mint, pos.tokens, q)
                if proceeds is not None:
                    break
            self.emit("error", o.mint, f"sell attempt at {bps}bps failed; escalating", symbol=pos.symbol)
        self.status = "ready"
        if proceeds is None:
            self.risk.on_exit_failed(o.mint)
            self.emit("error", o.mint, "EXIT FAILED at every slippage level; Risk will retry", symbol=pos.symbol)
            return
        del self.pf.positions[o.mint]
        self.pf.cash_sol += proceeds
        tr = ClosedTrade(pos.mint, pos.symbol, pos.cost_sol, proceeds, pos.opened_at, time.time(),
                         pos.entry_reason, o.reason)
        self.pf.closed.append(tr)
        self.pf.record_equity()
        self.emit("sell", o.mint, f"SELL {pos.symbol} {tr.pnl_pct:+.2f}% ({tr.pnl_sol:+.4f} SOL) | {o.reason}",
                  symbol=pos.symbol, pnl_sol=tr.pnl_sol, pnl_pct=tr.pnl_pct, cost=tr.cost_sol,
                  proceeds=proceeds, held_sec=tr.closed_at - tr.opened_at, exit_reason=o.reason)
        self.risk.on_closed(o.mint, tr.pnl_sol)
