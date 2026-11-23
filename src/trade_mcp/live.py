"""Bounded, shared quote polling cache. No trading calls and no invented ticks."""
from __future__ import annotations

import math
import re
import threading
import time
from typing import Any

from .bridge.base import BridgeError


class QuoteFeed:
    def __init__(self, mt5: Any, lock: Any, ttl: float = .1):
        self.mt5, self.lock, self.ttl = mt5, lock, ttl
        self.guard = threading.Lock()
        self.cache: dict[str, tuple[float, dict]] = {}
        self.sequence = 0

    def snapshot(self, names: list[str]) -> dict:
        names = list(dict.fromkeys(n.strip() for n in names if n.strip()))
        if not 1 <= len(names) <= 4:
            raise ValueError("request between one and four unique symbols")
        if any(not re.fullmatch(r"[\w.#+/!&() -]{1,64}", n) for n in names):
            raise ValueError("invalid symbol")
        started = time.monotonic()
        quotes, errors = {}, {}
        # Coalesce concurrent browser requests; cache bounds prevent unbounded growth.
        with self.guard:
            for name in names:
                saved = self.cache.get(name)
                if saved and time.monotonic() - saved[0] < self.ttl:
                    quotes[name] = saved[1]
                    continue
                try:
                    with self.lock:
                        tick = dict(self.mt5.tick(name))
                    bid, ask = float(tick.get("bid") or 0), float(tick.get("ask") or 0)
                    if not all(math.isfinite(v) and v > 0 for v in (bid, ask)) or ask < bid:
                        raise BridgeError("no valid bid/ask quote", code="invalid_quote")
                    self.sequence += 1
                    tick.update(received_at_ms=int(time.time()*1000), sequence=self.sequence)
                    self.cache[name] = (time.monotonic(), tick)
                    quotes[name] = tick
                except (BridgeError, ValueError, TypeError) as exc:
                    # Do not return an old cached price as a fresh successful quote.
                    errors[name] = str(exc)
            while len(self.cache) > 32:
                del self.cache[next(iter(self.cache))]
        return {"ok": True, "quotes": quotes, "errors": errors,
                "elapsed_ms": round((time.monotonic()-started)*1000, 1),
                "transport": "polling", "server_time_ms": int(time.time()*1000)}
