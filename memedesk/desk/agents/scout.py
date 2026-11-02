"""Scout: watches Pump.fun and Raydium for new pairs and hands Candidates to Filter.

Tokens that fail only *transient* checks (volume, LP lock, concentration) are
re-submitted on a schedule, since a 2-minute-old token can't have $50k/h volume yet.
Tokens that fail a *permanent* check (authorities, dev history, rugged) are never
looked at again.
"""
from __future__ import annotations

import asyncio
import time

from ..models import Candidate
from .base import Agent

RECHECK_AFTER_SEC = (180, 600, 1200, 2400)   # 3, 10, 20, 40 minutes after first sighting


class Scout(Agent):
    name = "Scout"

    def __init__(self, log_q, gateway, out_q: asyncio.Queue[Candidate]):
        super().__init__(log_q)
        self.gw = gateway
        self.out_q = out_q
        self.feedback: asyncio.Queue[tuple[Candidate, bool]] = asyncio.Queue()
        self.seen: set[str] = set()
        self.dead: set[str] = set()
        self._attempts: dict[str, int] = {}

    async def run(self) -> None:
        await asyncio.gather(self._discover(), self._rechecks())

    async def _discover(self) -> None:
        self.status = "scanning"
        async for c in self.gw.discover():
            if c.mint in self.seen:
                continue
            self.seen.add(c.mint)
            self.emit("scan", c.mint, f"new {c.source} pair {c.symbol}", source=c.source, symbol=c.symbol)
            await self.out_q.put(c)

    async def _rechecks(self) -> None:
        while True:
            c, permanent = await self.feedback.get()
            if permanent:
                self.dead.add(c.mint)
                continue
            n = self._attempts.get(c.mint, 0)
            if n >= len(RECHECK_AFTER_SEC):
                continue
            self._attempts[c.mint] = n + 1
            delay = c.seen_at + RECHECK_AFTER_SEC[n] - time.time()
            asyncio.create_task(self._requeue(c, max(delay, 1.0)))

    async def _requeue(self, c: Candidate, delay: float) -> None:
        await asyncio.sleep(delay)
        if c.mint not in self.dead:
            await self.out_q.put(c)
