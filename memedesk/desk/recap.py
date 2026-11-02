"""Morning recap: turns a session JSONL log into a Markdown report."""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

BLOCKS = " ▁▂▃▄▅▆▇█"


def sparkline(vals: list[float], width: int = 60) -> str:
    if not vals:
        return ""
    step = max(1, len(vals) // width)
    vals = vals[::step][-width:]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    return "".join(BLOCKS[int((v - lo) / span * (len(BLOCKS) - 1))] for v in vals)


def load(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def build(path: Path) -> str:
    ev = load(path)
    hdr = next((e["data"] for e in ev if e["kind"] == "session"), {})
    end = next((e["data"] for e in reversed(ev) if e["kind"] == "session_end"), None)
    buys = [e for e in ev if e["kind"] == "buy"]
    sells = [e for e in ev if e["kind"] == "sell"]
    rejects = [e for e in ev if e["kind"] == "reject" and e["agent"] == "Filter"]
    exec_rejects = [e for e in ev if e["kind"] == "reject" and e["agent"] == "Executor"]
    no_trade = [e for e in ev if e["kind"] == "no_trade"]
    scans = [e for e in ev if e["kind"] == "scan"]
    risk = [e for e in ev if e["agent"] == "Risk"]
    errors = [e for e in ev if e["kind"] == "error"]
    equity = [e["data"]["equity"] for e in ev if e["kind"] == "equity"]

    start_bal = hdr.get("starting_balance_sol", equity[0] if equity else 0.0)
    final = end["equity"] if end else (equity[-1] if equity else start_bal)
    equity = [start_bal] + equity + [final]
    pnl = final - start_bal
    wins = [s for s in sells if s["data"]["pnl_sol"] > 0]
    losses = [s for s in sells if s["data"]["pnl_sol"] <= 0]
    peak, mdd = start_bal, 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = max(mdd, (1 - v / peak) * 100 if peak else 0)

    fail_counts: Counter[str] = Counter()
    for r in rejects:
        for f in r["data"].get("failed", []):
            fail_counts[f] += 1
    t0 = hdr.get("started", ev[0]["ts"] if ev else 0)
    t1 = ev[-1]["ts"] if ev else t0
    fmt = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")

    L = [f"# Desk recap - {hdr.get('mode', '?').upper()}{' (SIMULATED DATA)' if hdr.get('simulated') else ''}",
         "", f"Session: {fmt(t0)} -> {fmt(t1)} ({(t1 - t0) / 3600:.1f} h). Log: `{path}`", ""]
    if hdr.get("simulated"):
        L += ["> **Simulated market.** These numbers exercise the software only and say nothing about "
              "real-world performance.", ""]
    L += ["## Balance", "",
          "| | SOL |", "|---|---|",
          f"| Start | {start_bal:.4f} |", f"| End (equity) | {final:.4f} |",
          f"| P&L | {pnl:+.4f} ({(pnl / start_bal * 100) if start_bal else 0:+.2f}%) |",
          f"| Max drawdown | {mdd:.2f}% |", "",
          "Equity curve:", "", "```", sparkline(equity) or "(no data)", "```", "",
          "## Pipeline", "",
          f"- Scout: **{len(scans)}** new pairs seen",
          f"- Filter: **{len(rejects)}** rejections, **{sum(1 for e in ev if e['kind'] == 'pass')}** passes",
          f"- Analyst: **{len(no_trade)}** scored under threshold, **{sum(1 for e in ev if e['kind'] == 'score')}** tradeable",
          f"- Executor: **{len(buys)}** buys, **{len(sells)}** sells, **{len(exec_rejects)}** refused",
          f"- Risk: **{len(risk)}** actions; errors logged: **{len(errors)}**", ""]
    if sells:
        L += [f"Win rate **{len(wins)}/{len(sells)} ({len(wins) / len(sells) * 100:.0f}%)**; "
              f"avg win {sum(s['data']['pnl_pct'] for s in wins) / max(len(wins), 1):+.2f}%, "
              f"avg loss {sum(s['data']['pnl_pct'] for s in losses) / max(len(losses), 1):+.2f}%", ""]
    L += ["## Why candidates were killed", "", "| Check | Rejections |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in fail_counts.most_common()] or ["| - | 0 |"]
    if exec_rejects:
        L += ["", "Executor refusals:", ""]
        L += [f"- {r} x{n}" for r, n in Counter(e["reason"].split(":")[1].strip().split(",")[0]
                                                  for e in exec_rejects).most_common(8)]
    L += ["", "## Trades", "", "| Opened | Token | Size SOL | P&L | Entry reason | Exit reason |",
          "|---|---|---|---|---|---|"]
    by_mint = {b["mint"]: b for b in buys}
    for s in sells:
        b = by_mint.get(s["mint"], {})
        L.append(f"| {fmt(b.get('ts', s['ts']))} | {s['data'].get('symbol', '?')} | {s['data']['cost']:.4f} | "
                 f"{s['data']['pnl_pct']:+.2f}% | {b.get('reason', '').split('|')[1].strip() if '|' in b.get('reason', '') else ''} | "
                 f"{s['data']['exit_reason']} |")
    open_ = [b for b in buys if b["mint"] not in {s["mint"] for s in sells}]
    for b in open_:
        L.append(f"| {fmt(b['ts'])} | {b['data'].get('symbol', '?')} | {b['data']['sol']:.4f} | OPEN | "
                 f"{b['reason'].split('|')[1].strip()} | - |")
    if halts := [e for e in risk if "HALT" in e["reason"]]:
        L += ["", "## Halts", ""] + [f"- {fmt(e['ts'])}: {e['reason']}" for e in halts]
    if errors:
        L += ["", "## Errors (last 10)", ""] + [f"- {fmt(e['ts'])} {e['agent']}: {e['reason']}" for e in errors[-10:]]
    return "\n".join(L) + "\n"


def main(argv: list[str]) -> None:
    if argv:
        path = Path(argv[0])
    else:
        logs = sorted(Path("logs").glob("session-*.jsonl"))
        if not logs:
            sys.exit("no session logs in ./logs")
        path = logs[-1]
    md = build(path)
    out = path.with_suffix(".recap.md")
    out.write_text(md)
    print(md)
    print(f"(saved to {out})")
