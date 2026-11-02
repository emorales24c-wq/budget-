"""Logger: every scan, rejection, score, trade and risk action, with its reason, to JSONL."""
from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import asdict
from pathlib import Path

from ..models import LogEvent
from .base import Agent


class Logger(Agent):
    name = "Logger"

    def __init__(self, log_q: asyncio.Queue[LogEvent], path: Path, header: dict, echo=None):
        super().__init__(log_q)
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = path.open("a", buffering=1)
        self._f.write(json.dumps({"agent": "Logger", "kind": "session", "mint": None,
                                  "reason": "session start", "data": header, "ts": header["started"]}) + "\n")
        self.feed: deque[LogEvent] = deque(maxlen=200)
        self.echo = echo
        self.status = f"writing {path.name}"

    async def run(self) -> None:
        while True:
            ev = await self.log_q.get()
            self.write(ev)

    def write(self, ev: LogEvent) -> None:
        self._f.write(json.dumps(asdict(ev), default=str) + "\n")
        self.counters["events"] += 1
        if ev.kind != "scan":
            self.feed.append(ev)
        if self.echo:
            self.echo(ev)

    def drain(self) -> None:
        while not self.log_q.empty():
            self.write(self.log_q.get_nowait())
        self._f.flush()

    def close(self) -> None:
        self.drain()
        self._f.close()
