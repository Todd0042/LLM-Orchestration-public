"""Which paid agents can take work right now, and recognising "out of usage".

The chain for heavy tasks is Claude Code -> AGY -> a local model. When an agent reports a usage
limit it is marked unavailable until its reset time (parsed from the message when it says, else
30 minutes), so later tasks skip straight to the next one instead of failing again.
"""

from __future__ import annotations

import datetime as dt
import re
import threading
import time
from typing import Any

LIMIT_RE = re.compile(
    r"usage limit|limit reached|hit your limit|reached your .*limit|rate.?limit|quota|resource[_ ]exhausted|"
    r"too many requests|\b429\b|credit balance|out of (?:credits|usage)",
    re.IGNORECASE,
)
DEFAULT_COOLDOWN_SEC = 30 * 60
AGENT_NAMES = {"claude-cli": "Claude", "agy-cli": "AGY"}


def is_usage_limit(text: str) -> bool:
    return bool(text) and bool(LIMIT_RE.search(text))


def parse_reset(text: str, now: float | None = None) -> float | None:
    """Best effort: '...|1732647600' (epoch), 'resets 9pm', 'resets at 3:30 am', 'try again in 2 hours'."""
    now = now or time.time()
    m = re.search(r"\|(\d{10})\b", text)
    if m:
        return float(m.group(1))
    m = re.search(r"in (\d+)\s*(minute|min|hour|hr)s?", text, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        return now + n * (3600 if m.group(2).lower().startswith("h") else 60)
    m = re.search(r"resets?(?: at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.IGNORECASE)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        base = dt.datetime.fromtimestamp(now)
        target = base.replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
        if target.timestamp() <= now:
            target += dt.timedelta(days=1)
        return target.timestamp()
    return None


class AgentAvailability:
    def __init__(self) -> None:
        self._until: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def available(self, agent: str) -> bool:
        with self._lock:
            until, _ = self._until.get(agent, (0.0, ""))
        return until <= time.time()

    def mark_exhausted(self, agent: str, text: str) -> float:
        until = parse_reset(text) or (time.time() + DEFAULT_COOLDOWN_SEC)
        until = min(max(until, time.time() + 60), time.time() + 8 * 86400)
        with self._lock:
            self._until[agent] = (until, text[:300])
        return until

    def clear(self, agent: str) -> None:
        with self._lock:
            self._until.pop(agent, None)

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            return {a: {"name": AGENT_NAMES.get(a, a), "until": u, "reason": r} for a, (u, r) in self._until.items() if u > now}


def until_text(ts: float) -> str:
    d = dt.datetime.fromtimestamp(ts)
    today = dt.datetime.now().date()
    stamp = d.strftime("%-I:%M %p").replace(":00 ", " ")
    return stamp if d.date() == today else d.strftime("%a ") + stamp
