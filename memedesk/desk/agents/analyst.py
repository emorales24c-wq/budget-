"""Analyst: scores Filter survivors 0..1. Below the threshold = no trade.

The score is a transparent weighted sum of simple features so every decision can be
explained in the log. It is a heuristic, not a predictive model; tune weights only
against paper-trading logs, never against hope.
"""
from __future__ import annotations

import asyncio
import math

from ..config import AnalystRules
from ..models import FilterPass, MarketSnapshot, TradeTicket
from .base import Agent

WEIGHTS = {
    "liquidity": 0.20,       # can we get out?
    "turnover": 0.15,        # real interest vs. liquidity, but not wash-trade levels
    "buy_pressure": 0.20,
    "trend": 0.15,           # up over 1h, but not a vertical blow-off
    "no_chase": 0.10,        # don't buy the top of a 5m candle or into a 5m dump
    "maturity": 0.10,        # survived the first minutes
    "distribution": 0.10,    # lower top-10 concentration is better
}


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _ramp(x: float, lo: float, hi: float) -> float:
    return _clip((x - lo) / (hi - lo))


def score_features(m: MarketSnapshot, top10_pct: float) -> dict[str, float]:
    turnover = m.volume_1h_usd / max(m.liquidity_usd, 1.0)
    total = m.buys_1h + m.sells_1h
    buy_ratio = m.buys_1h / total if total else 0.0
    ch1 = m.price_change_1h_pct
    if ch1 <= 0:
        trend = _ramp(ch1, -30, 0) * 0.3
    elif ch1 <= 80:
        trend = 0.3 + 0.7 * _ramp(ch1, 0, 20)
    else:                                   # parabolic: fade the score as it goes vertical
        trend = 1.0 - _ramp(ch1, 80, 300)
    ch5 = m.price_change_5m_pct
    no_chase = 1.0 - max(_ramp(ch5, 10, 30), _ramp(-ch5, 5, 15))
    return {
        "liquidity": _ramp(math.log10(max(m.liquidity_usd, 1)), math.log10(20_000), math.log10(250_000)),
        "turnover": _ramp(turnover, 0.2, 1.0) if turnover <= 3 else 1.0 - _ramp(turnover, 3, 10),
        "buy_pressure": _ramp(buy_ratio, 0.48, 0.70),
        "trend": trend,
        "no_chase": no_chase,
        "maturity": _ramp(m.pair_age_min, 5, 30),
        "distribution": 1.0 - _ramp(top10_pct, 5, 20),
    }


class Analyst(Agent):
    name = "Analyst"

    def __init__(self, log_q, gateway, rules: AnalystRules,
                 in_q: asyncio.Queue[FilterPass], out_q: asyncio.Queue[TradeTicket]):
        super().__init__(log_q)
        self.gw, self.rules, self.in_q, self.out_q = gateway, rules, in_q, out_q

    async def run(self) -> None:
        while True:
            fp = await self.in_q.get()
            self.status = f"scoring {fp.candidate.symbol}"
            ticket = self.evaluate(fp)
            if ticket:
                await self.out_q.put(ticket)
            self.status = "waiting"

    def evaluate(self, fp: FilterPass) -> TradeTicket | None:
        feats = score_features(fp.market, fp.top10_pct)
        score = round(sum(WEIGHTS[k] * v for k, v in feats.items()), 4)
        ranked = sorted(feats.items(), key=lambda kv: WEIGHTS[kv[0]] * kv[1], reverse=True)
        weakest = sorted(feats.items(), key=lambda kv: kv[1])[:2]
        why = (f"score {score:.2f}; strongest: " + ", ".join(f"{k}={v:.2f}" for k, v in ranked[:2])
               + "; weakest: " + ", ".join(f"{k}={v:.2f}" for k, v in weakest))
        sym, mint = fp.candidate.symbol, fp.candidate.mint
        if score < self.rules.min_score:
            self.emit("no_trade", mint, f"below {self.rules.min_score}: {why}", symbol=sym,
                      score=score, features=feats)
            return None
        self.emit("score", mint, f"tradeable: {why}", symbol=sym, score=score, features=feats)
        return TradeTicket(filter_pass=fp, score=score, score_breakdown=feats, reason=why)
