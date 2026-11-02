"""All hard limits live here. Agents read them; nothing mutates them at runtime."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class FilterRules:
    require_mint_revoked: bool = True
    require_freeze_revoked: bool = True        # extra: a live freeze authority can brick your tokens
    require_lp_locked: bool = True
    min_lp_locked_pct: float = 95.0            # % of LP that must be burned/locked
    max_top10_pct: float = 20.0                # top 10 holders (excl. pool vaults) must hold <= this %
    min_volume_1h_usd: float = 50_000.0
    reject_dev_linked_to_rugs: bool = True


@dataclass(frozen=True)
class AnalystRules:
    min_score: float = 0.6                     # under this = no trade


@dataclass(frozen=True)
class ExecutionRules:
    max_position_pct: float = 0.08             # of current SOL balance, per trade. Never more.
    max_open_positions: int = 4
    max_slippage_bps: int = 300                # refuse fills worse than 3%
    max_price_impact_pct: float = 2.0
    priority_fee_lamports: int = 50_000
    min_trade_sol: float = 0.01


@dataclass(frozen=True)
class RiskRules:
    hard_stop_pct: float = -6.0                # exit at -6% from entry
    trail_activate_pct: float = 12.0           # trailing only arms after +12%
    trail_distance_pct: float = 6.0            # once armed, exit if price falls this far off the peak
    loss_cooldown_sec: int = 15 * 60           # no new entries this long after any losing exit (no revenge)
    max_consecutive_losses: int = 3            # then halt entries for the session
    daily_max_drawdown_pct: float = 15.0       # equity drawdown from session start -> halt entries
    poll_interval_sec: float = 3.0


@dataclass(frozen=True)
class Endpoints:
    rpc_http: str = field(default_factory=lambda: os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"))
    rpc_ws: str = field(default_factory=lambda: os.getenv("SOLANA_WS_URL", "wss://api.mainnet-beta.solana.com"))
    pumpportal_ws: str = "wss://pumpportal.fun/api/data"
    dexscreener: str = "https://api.dexscreener.com"
    rugcheck: str = "https://api.rugcheck.xyz/v1"
    jupiter: str = field(default_factory=lambda: os.getenv("JUPITER_API_URL", "https://lite-api.jup.ag/swap/v1"))


@dataclass(frozen=True)
class Config:
    mode: str = "paper"                        # paper | live
    starting_balance_sol: float = 10.0         # paper only; live reads the wallet
    filters: FilterRules = field(default_factory=FilterRules)
    analyst: AnalystRules = field(default_factory=AnalystRules)
    execution: ExecutionRules = field(default_factory=ExecutionRules)
    risk: RiskRules = field(default_factory=RiskRules)
    endpoints: Endpoints = field(default_factory=Endpoints)
    log_dir: str = "logs"
    # Paper-first gate: live refuses to start without this many closed paper trades on record.
    min_paper_trades_before_live: int = 30
    live_max_balance_sol: float = 5.0          # live refuses to trade a wallet holding more than this
