"""Filter: all-or-nothing safety gate. One failed check kills the candidate.
Unverifiable data counts as a failure.
"""
from __future__ import annotations

import asyncio

from ..config import FilterRules
from ..models import Candidate, CheckResult, FilterPass, Seal
from .base import Agent

PERMANENT = {"mint_revoked", "freeze_revoked", "dev_history", "not_rugged"}


class Filter(Agent):
    name = "Filter"

    def __init__(self, log_q, gateway, rules: FilterRules, seal: Seal,
                 in_q: asyncio.Queue[Candidate], out_q: asyncio.Queue[FilterPass],
                 scout_feedback: asyncio.Queue, workers: int = 4):
        super().__init__(log_q)
        self.gw, self.rules, self.seal = gateway, rules, seal
        self.in_q, self.out_q, self.feedback = in_q, out_q, scout_feedback
        self.workers = workers
        self.skip: set[str] = set()     # mints the desk holds or has closed: never re-filter

    async def run(self) -> None:
        await asyncio.gather(*(self._worker() for _ in range(self.workers)))

    async def _worker(self) -> None:
        while True:
            c = await self.in_q.get()
            if c.mint in self.skip:
                continue
            self.status = f"checking {c.symbol}"
            try:
                await self.evaluate(c)
            except Exception as e:  # any crash in a check = reject
                self.emit("reject", c.mint, f"filter error, rejected: {e!r}", symbol=c.symbol, failed=["error"])
            self.status = "waiting"

    async def evaluate(self, c: Candidate) -> FilterPass | None:
        r = self.rules
        safety, market = await asyncio.gather(self.gw.safety(c.mint), self.gw.market(c.mint))
        checks: list[CheckResult] = []

        def add(name: str, ok: bool | None, detail: str) -> None:
            checks.append(CheckResult(name, bool(ok), detail if ok is not None else f"unverifiable: {detail}"))

        add("mint_revoked",
            safety.mint_authority_revoked if r.require_mint_revoked else True,
            "mint authority " + ("revoked" if safety.mint_authority_revoked else "ACTIVE")
            if safety.mint_authority_revoked is not None else "mint authority")
        add("freeze_revoked",
            safety.freeze_authority_revoked if r.require_freeze_revoked else True,
            "freeze authority " + ("revoked" if safety.freeze_authority_revoked else "ACTIVE")
            if safety.freeze_authority_revoked is not None else "freeze authority")
        lp = safety.lp_locked_pct
        add("lp_locked", (lp >= r.min_lp_locked_pct) if lp is not None else (None if r.require_lp_locked else True),
            f"LP locked/burned {lp:.1f}% (need >= {r.min_lp_locked_pct:.0f}%)" if lp is not None else "LP lock status")
        t10 = safety.top10_pct
        add("top10_concentration", (t10 <= r.max_top10_pct) if t10 is not None else None,
            f"top-10 holders {t10:.1f}% (max {r.max_top10_pct:.0f}%)" if t10 is not None else "holder distribution")
        vol = market.volume_1h_usd if market else None
        add("volume_1h", (vol >= r.min_volume_1h_usd) if vol is not None else None,
            f"1h volume ${vol:,.0f} (min ${r.min_volume_1h_usd:,.0f})" if vol is not None else "1h volume")
        if not r.reject_dev_linked_to_rugs:
            add("dev_history", True, "dev check disabled")
        elif safety.creator_rug_flags:
            add("dev_history", False, "dev linked to rugs: " + "; ".join(safety.creator_rug_flags))
        elif not safety.creator or not safety.creator_history_checked:
            add("dev_history", None, "dev wallet history")
        else:
            add("dev_history", True, f"dev {safety.creator[:6]}… no rug links found")
        add("not_rugged", not safety.already_rugged, "token flagged as already rugged" if safety.already_rugged
            else "not flagged rugged")

        failed = [x for x in checks if not x.passed]
        if failed:
            permanent = any(x.name in PERMANENT and not x.detail.startswith("unverifiable") for x in failed)
            self.emit("reject", c.mint, " | ".join(x.detail for x in failed), symbol=c.symbol,
                      failed=[x.name for x in failed], permanent=permanent, source=c.source)
            self.feedback.put_nowait((c, permanent))
            return None

        tup = tuple(checks)
        fp = FilterPass(candidate=c, checks=tup, market=market, signature=self.seal.sign(c, tup),
                        top10_pct=t10)
        self.skip.add(c.mint)
        self.emit("pass", c.mint, "passed all filters: " + "; ".join(x.detail for x in checks),
                  symbol=c.symbol, top10_pct=t10)
        await self.out_q.put(fp)
        return fp
