"""Wiring + CLI.

    python -m desk paper  [--hours 8] [--balance 10] [--headless] [--sim]
    python -m desk live   --keypair ~/.config/solana/desk.json --i-accept-real-losses [--hours 8]
    python -m desk recap  [logs/session-....jsonl]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from .agents.analyst import Analyst
from .agents.executor import Executor, LiveBroker, PaperBroker
from .agents.filter import Filter
from .agents.logger import Logger
from .agents.risk import Risk
from .agents.scout import Scout
from .config import Config
from .models import LogEvent, Seal
from .portfolio import Portfolio


class Desk:
    def __init__(self, cfg: Config, gateway, broker, starting_sol: float, simulated: bool = False,
                 echo=None):
        self.cfg, self.gw, self.simulated = cfg, gateway, simulated
        self.started = time.time()
        seal = Seal()
        log_q: asyncio.Queue[LogEvent] = asyncio.Queue()
        cand_q, pass_q, ticket_q, exit_q = (asyncio.Queue() for _ in range(4))
        self.portfolio = Portfolio(starting_sol)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        tag = f"{cfg.mode}{'-sim' if simulated else ''}"
        self.log_path = Path(cfg.log_dir) / f"session-{stamp}-{tag}.jsonl"
        self.logger = Logger(log_q, self.log_path, {
            "mode": cfg.mode, "simulated": simulated, "starting_balance_sol": starting_sol,
            "started": self.started, "config": asdict(cfg)}, echo=echo)
        self.scout = Scout(log_q, gateway, cand_q)
        self.filter = Filter(log_q, gateway, cfg.filters, seal, cand_q, pass_q, self.scout.feedback)
        self.analyst = Analyst(log_q, gateway, cfg.analyst, pass_q, ticket_q)
        self.risk = Risk(log_q, gateway, cfg.risk, self.portfolio, exit_q, cfg.execution.max_slippage_bps)
        self.executor = Executor(log_q, gateway, cfg, seal, self.portfolio, self.risk, ticket_q, exit_q, broker)
        self.agents = [self.scout, self.filter, self.analyst, self.executor, self.risk, self.logger]
        self._log_q = log_q

    async def _equity_tick(self) -> None:
        while True:
            await asyncio.sleep(30)
            self._log_q.put_nowait(LogEvent("Desk", "equity", None, "equity snapshot",
                                            {"equity": self.portfolio.equity(), "cash": self.portfolio.cash_sol,
                                             "open": len(self.portfolio.positions)}))

    async def run(self, hours: float | None, ui: bool) -> None:
        tasks = [asyncio.create_task(a.run()) for a in self.agents] + [asyncio.create_task(self._equity_tick())]
        try:
            if ui:
                from rich.live import Live
                from .ui import Dashboard
                dash = Dashboard(self)
                with Live(dash.render(), refresh_per_second=2, screen=True) as live:
                    end = time.time() + hours * 3600 if hours else float("inf")
                    while time.time() < end:
                        await asyncio.sleep(0.5)
                        live.update(dash.render())
                        _raise_if_crashed(tasks)
            else:
                end = time.time() + hours * 3600 if hours else float("inf")
                while time.time() < end:
                    await asyncio.sleep(1)
                    _raise_if_crashed(tasks)
        finally:
            await self.shutdown(tasks)

    async def shutdown(self, tasks) -> None:
        # stop discovery/entries first, then flatten anything still open
        for t in tasks[:3]:  # scout, filter, analyst
            t.cancel()
        if self.portfolio.positions:
            from .models import ExitOrder
            for p in list(self.portfolio.positions.values()):
                await self.executor.exit(ExitOrder(p.mint, "session end: flatten", p.last_price_sol))
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.logger.write(LogEvent("Desk", "session_end", None, "session end",
                                   {"equity": self.portfolio.equity(), "cash": self.portfolio.cash_sol}))
        self.logger.close()
        await self.gw.close()


def _raise_if_crashed(tasks) -> None:
    for t in tasks:
        if t.done() and not t.cancelled() and t.exception():
            raise t.exception()


def _paper_track_record(log_dir: str) -> int:
    n = 0
    for p in Path(log_dir).glob("session-*-paper.jsonl"):   # real-data paper only; -sim excluded
        n += sum(1 for line in p.read_text().splitlines() if '"kind": "sell"' in line)
    return n


def _echo(ev: LogEvent) -> None:
    if ev.kind in ("scan",):
        return
    sym = (ev.data or {}).get("symbol", "")
    print(f"{datetime.fromtimestamp(ev.ts):%H:%M:%S} {ev.agent:<8} {ev.kind:<8} {sym:<10} {ev.reason}", flush=True)


async def _main(args) -> None:
    cfg = Config(mode=args.cmd, starting_balance_sol=getattr(args, "balance", 10.0))
    Path(cfg.log_dir).mkdir(exist_ok=True)
    logging.basicConfig(filename=str(Path(cfg.log_dir) / "desk-debug.log"), level=logging.INFO)

    if args.cmd == "paper":
        if args.sim:
            from .sources.sim import SimGateway
            gw, sim = SimGateway(speed=args.sim_speed), True
            cfg = replace(cfg, risk=replace(cfg.risk, loss_cooldown_sec=int(cfg.risk.loss_cooldown_sec / args.sim_speed)))
        else:
            from .sources.solana import SolanaGateway
            gw, sim = SolanaGateway(cfg.endpoints), False
        broker = PaperBroker(cfg.execution.priority_fee_lamports / 1e9)
        desk = Desk(cfg, gw, broker, cfg.starting_balance_sol, simulated=sim,
                    echo=_echo if args.headless else None)
    else:
        # ---------------- live gates: every one must pass ----------------
        if not args.i_accept_real_losses:
            sys.exit("live mode needs --i-accept-real-losses")
        n = _paper_track_record(cfg.log_dir)
        if n < cfg.min_paper_trades_before_live:
            sys.exit(f"paper first: {n} closed real-data paper trades on record, "
                     f"{cfg.min_paper_trades_before_live} required before live")
        from solders.keypair import Keypair
        from .sources.solana import SolanaGateway
        kp = Keypair.from_bytes(bytes(json.loads(Path(args.keypair).expanduser().read_text())))
        gw = SolanaGateway(cfg.endpoints)
        bal = await gw.sol_balance(str(kp.pubkey()))
        if bal is None:
            sys.exit("could not read wallet balance")
        if bal > cfg.live_max_balance_sol:
            sys.exit(f"wallet holds {bal:.3f} SOL > live cap {cfg.live_max_balance_sol}. "
                     f"Use a dedicated hot wallet with only what you can lose.")
        desk = Desk(cfg, gw, LiveBroker(kp), bal, echo=_echo if args.headless else None)

    try:
        await desk.run(args.hours, ui=not args.headless)
    finally:
        from .recap import build
        out = desk.log_path.with_suffix(".recap.md")
        out.write_text(build(desk.log_path))
        print(f"\nlog: {desk.log_path}\nrecap: {out}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="desk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("paper", "live"):
        p = sub.add_parser(name)
        p.add_argument("--hours", type=float, default=None, help="stop after N hours, flatten, write recap")
        p.add_argument("--headless", action="store_true", help="plain log lines instead of the live terminal")
    sub.choices["paper"].add_argument("--balance", type=float, default=10.0)
    sub.choices["paper"].add_argument("--sim", action="store_true", help="offline simulated market (testing only)")
    sub.choices["paper"].add_argument("--sim-speed", type=float, default=1.0)
    sub.choices["live"].add_argument("--keypair", required=True)
    sub.choices["live"].add_argument("--i-accept-real-losses", action="store_true")
    r = sub.add_parser("recap")
    r.add_argument("log", nargs="?")
    args = ap.parse_args(argv)
    if args.cmd == "recap":
        from .recap import main as recap_main
        recap_main([args.log] if args.log else [])
        return
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass
