from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from ..models import LogEvent


class Agent:
    name = "agent"

    def __init__(self, log_q: asyncio.Queue[LogEvent]):
        self.log_q = log_q
        self.status = "idle"
        self.counters: Counter[str] = Counter()
        self.last = ""

    def emit(self, kind: str, mint: str | None, reason: str, **data: Any) -> None:
        self.counters[kind] += 1
        self.last = reason
        self.log_q.put_nowait(LogEvent(self.name, kind, mint, reason, data))
