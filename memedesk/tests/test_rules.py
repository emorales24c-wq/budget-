"""The hard rules, each pinned by a test. If one of these fails, do not trade."""
import asyncio
import dataclasses
import time

import pytest

from desk.agents.analyst import Analyst
from desk.agents.executor import Executor, PaperBroker
from desk.agents.filter import Filter
from desk.agents.risk import Risk
from desk.config import Config
from desk.models import Candidate, MarketSnapshot, Seal, TradeTicket, CheckResult, FilterPass
from desk.portfolio import Portfolio
from desk.sources.base import Quote, TokenSafety

GOOD_SAFETY = TokenSafety(True, True, 100.0, 12.0, "DEVgood", (), True, False, 6)
GOOD_MARKET = MarketSnapshot(price_usd=0.001, price_sol=1e-5, liquidity_usd=150_000, volume_1h_usd=200_000,
                             buys_1h=700, sells_1h=350, price_change_1h_pct=35, price_change_5m_pct=3,
                             pair_age_min=60, fdv_usd=1e6)


class FakeGW:
    def __init__(self, safety=GOOD_SAFETY, market=GOOD_MARKET, px=1e-5, impact=0.3):
        self.s, self.m, self.px, self.impact = safety, market, px, impact
        self.no_quote = False

    async def safety(self, mint): return self.s
    async def market(self, mint): return self.m

    async def quote_buy(self, mint, sol_in, bps):
        return Quote(sol_in, sol_in / self.px, self.impact)

    async def quote_sell(self, mint, tokens, bps):
        return None if self.no_quote else Quote(tokens, tokens * self.px, self.impact)

    async def close(self): pass


def rig(gw, cfg=Config(), cash=10.0):
    q = asyncio.Queue
    log_q, cand_q, pass_q, ticket_q, exit_q, fb = q(), q(), q(), q(), q(), q()
    seal = Seal()
    pf = Portfolio(cash)
    flt = Filter(log_q, gw, cfg.filters, seal, cand_q, pass_q, fb)
    an = Analyst(log_q, gw, cfg.analyst, pass_q, ticket_q)
    risk = Risk(log_q, gw, cfg.risk, pf, exit_q, 300)
    ex = Executor(log_q, gw, cfg, seal, pf, risk, ticket_q, exit_q, PaperBroker(0.00005))
    return dict(flt=flt, an=an, risk=risk, ex=ex, pf=pf, seal=seal, log_q=log_q, exit_q=exit_q, fb=fb)


def events(log_q):
    out = []
    while not log_q.empty():
        out.append(log_q.get_nowait())
    return out


async def _ticket(r, mint="M1"):
    fp = await r["flt"].evaluate(Candidate(mint, "raydium", "TKN"))
    assert fp is not None
    t = r["an"].evaluate(fp)
    assert t is not None
    return t


# ----------------------------------------------------------------- Filter
@pytest.mark.parametrize("change,failed", [
    (dict(mint_authority_revoked=False), "mint_revoked"),
    (dict(freeze_authority_revoked=False), "freeze_revoked"),
    (dict(lp_locked_pct=40.0), "lp_locked"),
    (dict(top10_pct=20.5), "top10_concentration"),
    (dict(creator_rug_flags=("Creator history of rugged tokens",)), "dev_history"),
    (dict(creator_history_checked=False), "dev_history"),
    (dict(mint_authority_revoked=None), "mint_revoked"),       # unverifiable = fail
    (dict(lp_locked_pct=None), "lp_locked"),
    (dict(top10_pct=None), "top10_concentration"),
    (dict(already_rugged=True), "not_rugged"),
])
async def test_filter_kills_each_failure(change, failed):
    r = rig(FakeGW(safety=dataclasses.replace(GOOD_SAFETY, **change)))
    assert await r["flt"].evaluate(Candidate("M1", "raydium", "TKN")) is None
    rej = [e for e in events(r["log_q"]) if e.kind == "reject"]
    assert failed in rej[0].data["failed"] and rej[0].reason


async def test_filter_kills_low_volume_and_missing_market():
    r = rig(FakeGW(market=dataclasses.replace(GOOD_MARKET, volume_1h_usd=49_999)))
    assert await r["flt"].evaluate(Candidate("M1", "raydium")) is None
    r = rig(FakeGW(market=None))
    assert await r["flt"].evaluate(Candidate("M1", "raydium")) is None


async def test_permanent_vs_transient_feedback_to_scout():
    r = rig(FakeGW(safety=dataclasses.replace(GOOD_SAFETY, mint_authority_revoked=False)))
    await r["flt"].evaluate(Candidate("M1", "raydium"))
    assert r["fb"].get_nowait()[1] is True
    r = rig(FakeGW(market=dataclasses.replace(GOOD_MARKET, volume_1h_usd=1000)))
    await r["flt"].evaluate(Candidate("M2", "raydium"))
    assert r["fb"].get_nowait()[1] is False


# ----------------------------------------------------------------- Analyst
async def test_analyst_blocks_under_threshold():
    weak = dataclasses.replace(GOOD_MARKET, buys_1h=100, sells_1h=300, price_change_1h_pct=-20,
                               price_change_5m_pct=-12, liquidity_usd=21_000)
    r = rig(FakeGW(market=weak))
    fp = await r["flt"].evaluate(Candidate("M1", "raydium"))
    assert r["an"].evaluate(fp) is None
    assert any(e.kind == "no_trade" for e in events(r["log_q"]))


# ----------------------------------------------------------------- Executor
async def test_size_is_8pct_of_current_balance():
    r = rig(FakeGW())
    await r["ex"].enter(await _ticket(r))
    pos = r["pf"].positions["M1"]
    assert pos.cost_sol <= 10.0 * 0.08 + 1e-12
    assert r["pf"].cash_sol == pytest.approx(10.0 - pos.cost_sol)
    # second trade sizes off the *new* balance
    await r["ex"].enter(await _ticket(r, "M2"))
    assert r["pf"].positions["M2"].cost_sol <= r["pf"].cash_sol / 0.92 * 0.08 + 1e-12


async def test_executor_rejects_forged_or_tampered_ticket():
    r = rig(FakeGW())
    real = await _ticket(r)
    forged_fp = dataclasses.replace(real.filter_pass, signature="00" * 32)
    await r["ex"].enter(dataclasses.replace(real, filter_pass=forged_fp))
    # tampered: flip a failed check to passed after signing -> signature no longer matches
    bad_checks = tuple(CheckResult(c.name, c.passed, c.detail) for c in real.filter_pass.checks[:-1])
    other = Seal()  # wrong key
    fp2 = FilterPass(real.filter_pass.candidate, bad_checks, GOOD_MARKET,
                     other.sign(real.filter_pass.candidate, bad_checks))
    await r["ex"].enter(dataclasses.replace(real, filter_pass=fp2))
    await r["ex"].enter(dataclasses.replace(real, score=0.59))
    assert not r["pf"].positions
    rej = [e.reason for e in events(r["log_q"]) if e.agent == "Executor" and e.kind == "reject"]
    assert len(rej) == 3


async def test_no_averaging_down_and_no_reentry():
    gw = FakeGW()
    r = rig(gw)
    t = await _ticket(r)
    await r["ex"].enter(t)
    await r["ex"].enter(t)  # add to same mint
    assert r["pf"].positions["M1"].cost_sol <= 0.8
    assert "no averaging down" in [e for e in events(r["log_q"]) if e.kind == "reject"][-1].reason
    gw.px = 0.9e-5
    await r["risk"].check_all()
    await r["ex"].exit(r["exit_q"].get_nowait())
    assert not r["pf"].positions
    r["risk"].cooldown_until = 0  # even with cooldown over, the mint stays banned
    await r["ex"].enter(t)
    assert not r["pf"].positions


async def test_price_impact_cap():
    r = rig(FakeGW(impact=5.0))
    await r["ex"].enter(await _ticket(r))
    assert not r["pf"].positions


# ----------------------------------------------------------------- Risk
async def test_hard_stop_at_minus_6():
    gw = FakeGW()
    r = rig(gw)
    await r["ex"].enter(await _ticket(r))
    entry = r["pf"].positions["M1"].entry_price_sol
    gw.px = entry * 0.95          # -5%: hold
    await r["risk"].check_all()
    assert r["exit_q"].empty()
    gw.px = entry * 0.939         # -6.1%: out
    await r["risk"].check_all()
    o = r["exit_q"].get_nowait()
    assert "HARD STOP" in o.reason
    await r["ex"].exit(o)
    assert not r["pf"].positions and r["pf"].closed[0].pnl_sol < 0


async def test_trail_only_after_plus_12():
    gw = FakeGW()
    r = rig(gw)
    await r["ex"].enter(await _ticket(r))
    pos = r["pf"].positions["M1"]
    e = pos.entry_price_sol
    gw.px = e * 1.11; await r["risk"].check_all()
    gw.px = e * 1.03; await r["risk"].check_all()   # -7% off an 11% peak: trail NOT armed, hold
    assert not pos.trail_armed and r["exit_q"].empty()
    gw.px = e * 1.20; await r["risk"].check_all()
    assert pos.trail_armed and r["exit_q"].empty()
    gw.px = e * 1.20 * 0.95; await r["risk"].check_all()  # 5% off peak: hold
    assert r["exit_q"].empty()
    gw.px = e * 1.20 * 0.939; await r["risk"].check_all()  # >6% off peak: out, in profit
    o = r["exit_q"].get_nowait()
    assert "TRAILING" in o.reason
    await r["ex"].exit(o)
    assert r["pf"].closed[0].pnl_sol > 0


async def test_no_revenge_cooldown_and_halt_after_losses():
    gw = FakeGW()
    r = rig(gw)
    for i in range(3):
        r["risk"].cooldown_until = 0
        gw.px = 1e-5
        await r["ex"].enter(await _ticket(r, f"L{i}"))
        gw.px = 0.5e-5
        await r["risk"].check_all()
        await r["ex"].exit(r["exit_q"].get_nowait())
        assert r["risk"].cooldown_until > time.time()
    assert r["risk"].halted_reason
    r["risk"].cooldown_until = 0
    gw.px = 1e-5
    await r["ex"].enter(await _ticket(r, "NEW"))
    assert not r["pf"].positions


async def test_emergency_exit_when_no_quote():
    gw = FakeGW()
    r = rig(gw)
    await r["ex"].enter(await _ticket(r))
    gw.no_quote = True
    for _ in range(3):
        await r["risk"].check_all()
    assert "emergency" in r["exit_q"].get_nowait().reason
