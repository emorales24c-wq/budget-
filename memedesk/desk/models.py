"""Messages passed between agents. Each handoff is a distinct, frozen type.

Scout -> Candidate -> Filter -> FilterPass -> Analyst -> TradeTicket -> Executor
Risk  -> ExitOrder -> Executor
Everyone -> LogEvent -> Logger

Filter signs every FilterPass with a per-session secret; Executor re-verifies the
signature and every individual check before it will touch the wallet.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any


SOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass(frozen=True)
class Candidate:
    mint: str
    source: str            # "pumpfun" | "raydium" | "sim"
    symbol: str = "?"
    pair_address: str | None = None
    creator: str | None = None
    seen_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class MarketSnapshot:
    price_usd: float
    price_sol: float
    liquidity_usd: float
    volume_1h_usd: float
    buys_1h: int
    sells_1h: int
    price_change_1h_pct: float
    price_change_5m_pct: float
    pair_age_min: float
    fdv_usd: float
    holder_count: int | None = None
    rugcheck_score: float | None = None   # RugCheck normalized risk (lower = safer)


@dataclass(frozen=True)
class FilterPass:
    candidate: Candidate
    checks: tuple[CheckResult, ...]
    market: MarketSnapshot
    signature: str
    top10_pct: float = 100.0

    def payload(self) -> bytes:
        return _payload(self.candidate, self.checks)


@dataclass(frozen=True)
class TradeTicket:
    filter_pass: FilterPass
    score: float
    score_breakdown: dict[str, float]
    reason: str


@dataclass(frozen=True)
class ExitOrder:
    mint: str
    reason: str
    trigger_price_sol: float


@dataclass
class Position:
    mint: str
    symbol: str
    entry_price_sol: float
    tokens: float
    cost_sol: float
    opened_at: float
    peak_price_sol: float
    last_price_sol: float
    trail_armed: bool = False
    entry_reason: str = ""

    @property
    def pnl_pct(self) -> float:
        return (self.last_price_sol / self.entry_price_sol - 1.0) * 100.0

    @property
    def value_sol(self) -> float:
        return self.tokens * self.last_price_sol


@dataclass(frozen=True)
class LogEvent:
    agent: str
    kind: str              # scan | reject | pass | score | no_trade | buy | sell | risk | error | info | equity
    mint: str | None
    reason: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


def _payload(c: Candidate, checks: tuple[CheckResult, ...]) -> bytes:
    body = {"mint": c.mint, "checks": [(x.name, x.passed) for x in checks]}
    return json.dumps(body, sort_keys=True).encode()


class Seal:
    """Per-session HMAC shared only by Filter (signer) and Executor (verifier)."""

    REQUIRED_CHECKS = ("mint_revoked", "freeze_revoked", "lp_locked", "top10_concentration",
                       "volume_1h", "dev_history", "not_rugged")

    def __init__(self, key: bytes | None = None):
        self._key = key or secrets.token_bytes(32)

    def sign(self, c: Candidate, checks: tuple[CheckResult, ...]) -> str:
        return hmac.new(self._key, _payload(c, checks), hashlib.sha256).hexdigest()

    def verify(self, fp: FilterPass) -> bool:
        expected = hmac.new(self._key, fp.payload(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, fp.signature)
