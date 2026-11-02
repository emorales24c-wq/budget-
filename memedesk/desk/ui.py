"""Live terminal: balance, agent activity, positions, equity curve, event feed."""
from __future__ import annotations

import time
from datetime import datetime

from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

KIND_STYLE = {"reject": "red", "no_trade": "yellow", "pass": "cyan", "score": "green", "buy": "bold green",
              "sell": "bold magenta", "risk": "bold yellow", "error": "bold red", "info": "white"}
BLOCKS = " ▁▂▃▄▅▆▇█"


def equity_chart(points: list[float], width: int, height: int) -> Text:
    if len(points) < 2:
        return Text("collecting data…", style="dim")
    step = max(1, len(points) // width)
    pts = points[::step][-width:]
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or max(hi * 0.001, 1e-9)
    base = points[0]
    rows = []
    for r in range(height, 0, -1):
        line = Text()
        for v in pts:
            level = (v - lo) / span * height
            ch = "█" if level >= r else (BLOCKS[int((level - r + 1) * 8)] if level > r - 1 else " ")
            line.append(ch, style="green" if v >= base else "red")
        rows.append(line)
    out = Text()
    out.append(f"{hi:.4f}\n", style="dim")
    for row in rows:
        out.append_text(row)
        out.append("\n")
    out.append(f"{lo:.4f}", style="dim")
    return out


class Dashboard:
    def __init__(self, desk):
        self.d = desk

    def render(self) -> Layout:
        d = self.d
        pf = d.portfolio
        eq = pf.equity()
        pnl = eq - pf.start_sol
        lay = Layout()
        lay.split_column(Layout(name="head", size=3), Layout(name="mid", ratio=3), Layout(name="feed", ratio=2))
        lay["mid"].split_row(Layout(name="left", ratio=1), Layout(name="right", ratio=1))
        lay["left"].split_column(Layout(name="agents"), Layout(name="positions"))

        mode = Text(f" {d.cfg.mode.upper()}{' · SIMULATED DATA' if d.simulated else ''} ",
                    style="black on yellow" if d.cfg.mode == "paper" else "white on red")
        hdr = Text.assemble(mode, "  Balance ", (f"{pf.cash_sol:.4f} SOL", "bold"),
                            "  Equity ", (f"{eq:.4f} SOL", "bold"),
                            "  P&L ", (f"{pnl:+.4f} ({pnl / pf.start_sol * 100:+.2f}%)",
                                       "bold green" if pnl >= 0 else "bold red"),
                            f"  Open {len(pf.positions)}/{d.cfg.execution.max_open_positions}",
                            f"  Closed {len(pf.closed)}",
                            f"  Up {(time.time() - d.started) / 3600:.2f}h",
                            ("  ENTRIES HALTED: " + d.risk.halted_reason, "bold red") if d.risk.halted_reason else "")
        lay["head"].update(Panel(hdr, title="memedesk", border_style="blue"))

        at = Table(expand=True, box=None, pad_edge=False)
        at.add_column("Agent", style="bold", no_wrap=True, min_width=8)
        at.add_column("Status", overflow="ellipsis", no_wrap=True, ratio=2)
        at.add_column("Counts", overflow="ellipsis", no_wrap=True, ratio=2)
        for a in d.agents:
            counts = " ".join(f"{k}:{v}" for k, v in a.counters.most_common(3))
            at.add_row(a.name, a.status, counts)
        lay["agents"].update(Panel(at, title="Agents", border_style="cyan"))

        pt = Table(expand=True, box=None)
        for c in ("Token", "Cost", "Value", "P&L", "Peak", "Trail"):
            pt.add_column(c)
        for p in pf.positions.values():
            peak = (p.peak_price_sol / p.entry_price_sol - 1) * 100
            pt.add_row(p.symbol, f"{p.cost_sol:.3f}", f"{p.value_sol:.3f}",
                       Text(f"{p.pnl_pct:+.2f}%", style="green" if p.pnl_pct >= 0 else "red"),
                       f"{peak:+.1f}%", "ARMED" if p.trail_armed else "-")
        if not pf.positions:
            pt.add_row("—", "", "", "", "", "")
        lay["positions"].update(Panel(pt, title="Positions", border_style="magenta"))

        eqs = [v for _, v in pf.equity_curve]
        lay["right"].update(Panel(equity_chart(eqs, width=60, height=10), title="Equity (SOL)", border_style="green"))

        feed = Table(expand=True, box=None, show_header=False)
        feed.add_column("t", style="dim", min_width=8, no_wrap=True)
        feed.add_column("agent", min_width=8, no_wrap=True)
        feed.add_column("msg", overflow="ellipsis", no_wrap=True, ratio=1)
        for ev in list(d.logger.feed)[-14:]:
            sym = (ev.data or {}).get("symbol", "")
            feed.add_row(datetime.fromtimestamp(ev.ts).strftime("%H:%M:%S"), ev.agent,
                         Text(f"{sym+' ' if sym else ''}{ev.reason}", style=KIND_STYLE.get(ev.kind, "")))
        lay["feed"].update(Panel(feed, title="Activity (every decision is logged with its reason)",
                                 border_style="white"))
        return lay
