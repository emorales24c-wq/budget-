"""Offline simulated market. For tests and for checking the terminal UI without network.

NOT real data. Results from this gateway say nothing about real-world performance.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass
from typing import AsyncIterator

from ..models import Candidate, MarketSnapshot
from .base import Quote, TokenSafety


@dataclass
class _SimToken:
    mint: str
    symbol: str
    safety: TokenSafety
    price: float
    liquidity_sol: float
    vol_1h: float
    drift: float
    vol: float
    rug_at: float | None
    born: float


class SimGateway:
    def __init__(self, seed: int = 7, spawn_every: float = 1.5, speed: float = 1.0):
        self.rng = random.Random(seed)
        self.spawn_every = spawn_every
        self.speed = speed
        self.tokens: dict[str, _SimToken] = {}
        self._n = 0

    async def close(self) -> None:
        pass

    def _spawn(self) -> _SimToken:
        r = self.rng
        self._n += 1
        mint = f"SIM{self._n:05d}" + "x" * 35
        bad = r.random()
        safety = TokenSafety(
            mint_authority_revoked=r.random() > 0.15,
            freeze_authority_revoked=r.random() > 0.10,
            lp_locked_pct=r.choice([0.0, 50.0, 100.0, 100.0, 100.0]),
            top10_pct=r.uniform(8, 45),
            creator=f"DEV{r.randint(1, 60)}",
            creator_rug_flags=("Creator history of rugged tokens",) if bad < 0.12 else (),
            creator_history_checked=r.random() > 0.05,
            decimals=6,
        )
        is_rug = r.random() < 0.25
        t = _SimToken(
            mint=mint, symbol=f"SIM{self._n}", safety=safety,
            price=r.uniform(1e-7, 5e-6), liquidity_sol=r.uniform(40, 600),
            vol_1h=r.lognormvariate(10.8, 0.9), drift=r.gauss(0.0, 0.0025), vol=r.uniform(0.006, 0.02),
            rug_at=time.time() + r.uniform(20, 240) / self.speed if is_rug else None, born=time.time(),
        )
        self.tokens[mint] = t
        return t

    async def discover(self) -> AsyncIterator[Candidate]:
        while True:
            await asyncio.sleep(self.spawn_every)
            t = self._spawn()
            yield Candidate(mint=t.mint, source=self.rng.choice(["pumpfun", "raydium"]),
                            symbol=t.symbol, creator=t.safety.creator)

    def _tick(self, t: _SimToken) -> None:
        if t.rug_at and time.time() >= t.rug_at:
            t.price *= 0.05
            t.liquidity_sol *= 0.05
            t.rug_at = None
            return
        t.price *= math.exp(t.drift + self.rng.gauss(0, t.vol))

    async def safety(self, mint: str) -> TokenSafety:
        t = self.tokens.get(mint)
        return t.safety if t else TokenSafety(None, None, None, None, None)

    async def market(self, mint: str) -> MarketSnapshot | None:
        t = self.tokens.get(mint)
        if not t:
            return None
        self._tick(t)
        r = self.rng
        buys = r.randint(50, 900)
        return MarketSnapshot(
            price_usd=t.price * 160, price_sol=t.price, liquidity_usd=t.liquidity_sol * 160 * 2,
            volume_1h_usd=t.vol_1h, buys_1h=buys, sells_1h=int(buys * r.uniform(0.4, 1.4)),
            price_change_1h_pct=r.uniform(-30, 120), price_change_5m_pct=r.uniform(-10, 25),
            pair_age_min=(time.time() - t.born) / 60 + r.uniform(5, 90), fdv_usd=t.price * 1e9 * 160,
        )

    def _impact(self, t: _SimToken, sol: float) -> float:
        return sol / (t.liquidity_sol + sol) * 100

    async def quote_buy(self, mint: str, sol_in: float, slippage_bps: int) -> Quote | None:
        t = self.tokens.get(mint)
        if not t:
            return None
        self._tick(t)
        imp = self._impact(t, sol_in)
        return Quote(sol_in, sol_in / t.price * (1 - imp / 100) * 0.9975, imp)

    async def quote_sell(self, mint: str, tokens: float, slippage_bps: int) -> Quote | None:
        t = self.tokens.get(mint)
        if not t:
            return None
        self._tick(t)
        sol = tokens * t.price
        imp = self._impact(t, sol)
        return Quote(tokens, sol * (1 - imp / 100) * 0.9975, imp)
