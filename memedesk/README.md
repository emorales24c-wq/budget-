# memedesk: a Solana memecoin desk that puts capital preservation first

Six agents. Each one passes work to the next through its own queue and message type, so no agent can skip a step.

```
Scout ──Candidate──▶ Filter ──FilterPass (HMAC-sealed)──▶ Analyst ──TradeTicket──▶ Executor ◀──ExitOrder── Risk
  ▲                    │                                                              │                    │
  └── recheck/kill ────┘                     every decision + reason ──▶ Logger (JSONL) ◀───────────────────┘
```

| Agent | Job | Hard rules (in code, covered by `tests/test_rules.py`) |
|---|---|---|
| **Scout** | Streams new pairs from Pump.fun (PumpPortal WS: new tokens and migrations) and Raydium (RPC `logsSubscribe` on AMM v4 and CPMM pool init) | Re-submits *transient* rejects at 3/10/20/40 min. Never looks at a *permanent* reject again. |
| **Filter** | All-or-nothing safety gate | Kills a token if: the mint authority is still active, the freeze authority is still active, under 95% of LP is locked or burned, the top 10 holders (not counting pool vaults) hold more than 20%, 1h volume is under $50k, the dev is linked to rugs, or the token is already flagged as rugged. **If a check can't be verified, the token fails it.** |
| **Analyst** | Weighted 0–1 score: liquidity, turnover, buy pressure, trend, no-chase, maturity, distribution | Score under **0.6** means no trade. Every score is logged with its strongest and weakest factors. |
| **Executor** | The only agent that can move money | Checks every ticket again: Filter's HMAC seal, all 7 checks present and passed, score ≥ 0.6, and no veto from Risk. Size is **≤ 8% of the current SOL balance**, fees included. It also refuses: price impact over 2%, more than 4 open positions, adding to a position it already holds. |
| **Risk** | Manages every open position, every exit, and every reason not to enter | **Hard stop at −6%**, measured on the real sell quote (after price impact and fees), not on a chart price. The **trailing stop arms only after +12%**, then exits at 6% off the peak. **No averaging down.** **No revenge trades:** a losing exit bans that mint for the session and freezes all new entries for 15 min. Entries halt after 3 losses in a row or a 15% session drawdown. If there's no sell route 3 polls in a row, it makes an emergency exit. |
| **Logger** | JSONL record of every scan, rejection, score, trade, and risk action, each with its reason | Writes a Markdown recap when the session ends. |

All limits are in `desk/config.py`.

## Run

```bash
cd memedesk
pip install -r requirements.txt

# 1. Paper trading on real market data (uses a free public RPC; for Raydium discovery, get a Helius/Triton key)
export SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=XXX
export SOLANA_WS_URL=wss://mainnet.helius-rpc.com/?api-key=XXX
python -m desk paper --balance 10 --hours 8          # live terminal UI
python -m desk paper --balance 10 --hours 8 --headless > overnight.log   # under nohup/tmux/systemd

# Morning: the recap is written automatically at the end of the session, or rebuild it any time:
python -m desk recap                                  # latest log -> logs/session-*.recap.md

# 2. Live trading. It won't start until ALL of these hold:
#    - at least 30 closed paper trades on REAL data in ./logs (simulator runs don't count)
#    - the wallet holds ≤ 5 SOL (use a dedicated hot wallet)
#    - you pass --i-accept-real-losses
python -m desk live --keypair ~/.config/solana/desk-hot.json --i-accept-real-losses --hours 8

# Offline simulator: only for testing the software and the UI. NOT market data.
python -m desk paper --sim --sim-speed 10
pytest -q
```

When a session ends (`--hours` elapsed or Ctrl-C), the desk stops discovery, sells every open position, and writes the recap.

## Data sources and how they can fail

| Check | Source |
|---|---|
| Mint and freeze authority | Solana RPC `getAccountInfo` (read straight from the chain) |
| LP lock %, top holders, creator, rug flags | RugCheck `/v1/tokens/{mint}/report` |
| Dev linked to rugs | RugCheck creator risk flags, plus a local blacklist `data/rugged_devs.txt` (add wallets you know) |
| Price, liquidity, 1h volume, buys/sells | DexScreener `/token-pairs/v1/solana/{mint}` (SOL-quoted pair with the most liquidity) |
| Quotes and swaps | Jupiter (`otherAmountThreshold`, the worst case under slippage, is used as the fill) |

Third-party APIs change their response formats. When a parser can't read a response, the check fails, so the desk **rejects more, never less**. During your first paper run, read the rejection reasons. If almost everything says `unverifiable: …`, an API format has changed and `desk/sources/solana.py` needs a fix. Don't loosen the filter to compensate.

## Limits you should know about

- **A −6% stop is not a −6% maximum loss.** If the dev pulls liquidity or dumps between two polls, the next executable quote can be −90%. Your loss per rug is capped by the **8% position size**, not by the stop. The simulator shows exactly this.
- The dev-history check is only as good as RugCheck's flags and your blacklist. A fresh wallet funded by a serial rugger will pass it.
- Pump.fun tokens on the bonding curve have no LP, so they fail `lp_locked` until they migrate. That's intended.
- The Analyst score is a transparent heuristic, not a proven edge. Adjust it only based on paper-trade logs.
