"""The data contract every market gateway (real or simulated) must satisfy.

Every field is Optional on purpose: None means "could not verify", and the Filter
treats unverifiable as FAILED. The desk fails closed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol

from ..models import Candidate, MarketSnapshot


@dataclass(frozen=True)
class TokenSafety:
    mint_authority_revoked: bool | None
    freeze_authority_revoked: bool | None
    lp_locked_pct: float | None
    top10_pct: float | None              # excluding AMM/bonding-curve vaults
    creator: str | None
    creator_rug_flags: tuple[str, ...] = ()
    creator_history_checked: bool = False
    already_rugged: bool = False
    decimals: int | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Quote:
    in_amount: float          # human units of input
    out_amount: float         # human units of output
    price_impact_pct: float
    raw: dict | None = None   # router response, needed to build a live swap


class Gateway(Protocol):
    def discover(self) -> AsyncIterator[Candidate]: ...
    async def safety(self, mint: str) -> TokenSafety: ...
    async def market(self, mint: str) -> MarketSnapshot | None: ...
    async def quote_buy(self, mint: str, sol_in: float, slippage_bps: int) -> Quote | None: ...
    async def quote_sell(self, mint: str, tokens: float, slippage_bps: int) -> Quote | None: ...
    async def close(self) -> None: ...
