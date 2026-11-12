"""Typed, cached convenience layer on top of a bridge transport."""

from __future__ import annotations

import contextlib
import time
from typing import Any

from .base import Bridge, BridgeError

SYMBOL_CACHE_TTL = 60.0
ACCOUNT_CACHE_TTL = 3.0


class MT5:
    """High level MetaTrader access used by the MCP tools."""

    def __init__(self, bridge: Bridge, default_symbol: str = "XAUUSD"):
        self.bridge = bridge
        self.default_symbol = default_symbol
        self._symbol_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._account_cache: tuple[float, dict[str, Any]] | None = None

    # ------------------------------------------------------------------ raw
    def request(self, cmd: str, **params: Any) -> Any:
        return self.bridge.request(cmd, **params)

    def status(self) -> dict[str, Any]:
        return self.bridge.status()

    def close(self) -> None:
        self.bridge.close()

    # ------------------------------------------------------------- terminal
    def ping(self) -> dict[str, Any]:
        return self.request("ping")

    def terminal_info(self) -> dict[str, Any]:
        return self.request("terminal_info")

    def account_info(self, fresh: bool = False) -> dict[str, Any]:
        if not fresh and self._account_cache:
            ts, data = self._account_cache
            if time.time() - ts < ACCOUNT_CACHE_TTL:
                return data
        data = self.request("account_info")
        self._account_cache = (time.time(), data)
        return data

    # --------------------------------------------------------------- symbols
    def symbols_list(self, pattern: str = "", limit: int = 500) -> list[dict[str, Any]]:
        return self.request("symbols_list", pattern=pattern, limit=limit)

    def find_symbol(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search Market Watch (and the full broker list) for a symbol."""
        q = (query or self.default_symbol).upper()
        hits = [
            s
            for s in self.symbols_list(pattern=q, limit=2000)
            if q in s.get("symbol", "").upper() or q in (s.get("description") or "").upper()
        ]
        hits.sort(key=lambda s: (not s.get("visible"), len(s.get("symbol", "")), s.get("symbol", "")))
        return hits[:limit]

    def resolve_symbol(self, symbol: str | None = None) -> str:
        """Map a loose name ("XAUUSD") to the broker's real symbol ("XAUUSD.a")."""
        name = (symbol or self.default_symbol or "XAUUSD").strip()
        try:
            self.symbol_info(name)
            return name
        except BridgeError:
            pass
        candidates = self.find_symbol(name)
        if not candidates:
            raise BridgeError(
                f"symbol '{name}' not found in Market Watch or the broker list. "
                "Use find_symbol to see available names.",
                code="unknown_symbol",
            )
        chosen = candidates[0]["symbol"]
        with contextlib.suppress(BridgeError):
            self.symbol_select(chosen)
        self._symbol_cache.pop(chosen, None)
        return chosen

    def symbol_select(self, symbol: str, enable: bool = True) -> dict[str, Any]:
        return self.request("symbol_select", symbol=symbol, enable=enable)

    def symbol_info(self, symbol: str, fresh: bool = False) -> dict[str, Any]:
        now = time.time()
        cached = self._symbol_cache.get(symbol)
        if cached and not fresh and now - cached[0] < SYMBOL_CACHE_TTL:
            return cached[1]
        info = self.request("symbol_info", symbol=symbol)
        if info.get("visible") is False:
            try:
                self.symbol_select(symbol)
                info = self.request("symbol_info", symbol=symbol)
            except BridgeError:
                pass
        self._symbol_cache[symbol] = (now, info)
        return info

    # ------------------------------------------------------------------ data
    def tick(self, symbol: str) -> dict[str, Any]:
        return self.request("tick", symbol=symbol)

    def candles(self, symbol: str, timeframe: str = "M15", count: int = 300) -> list[dict[str, Any]]:
        data = self.request("rates", symbol=symbol, timeframe=timeframe, count=int(count))
        rates = data.get("rates") if isinstance(data, dict) else data
        return list(rates or [])

    # ------------------------------------------------------------- positions
    def positions(self) -> list[dict[str, Any]]:
        return list(self.request("positions") or [])

    def orders(self) -> list[dict[str, Any]]:
        return list(self.request("orders") or [])

    def deals(self, date_from: int = 0, date_to: int = 0) -> list[dict[str, Any]]:
        return list(self.request("history_deals", date_from=date_from, date_to=date_to) or [])

    def calc_order(
        self, symbol: str, side: str, volume: float, price: float = 0.0
    ) -> dict[str, Any]:
        return self.request(
            "calc_order", symbol=symbol, side=side, volume=float(volume), price=float(price)
        )

    # --------------------------------------------------------------- trading
    def open_order(
        self,
        symbol: str,
        side: str,
        volume: float,
        sl: float = 0.0,
        tp: float = 0.0,
        comment: str = "trade-mcp",
        magic: int = 0,
        deviation: int = 30,
    ) -> dict[str, Any]:
        return self.request(
            "trade_open",
            symbol=symbol,
            side=side,
            volume=float(volume),
            sl=float(sl or 0),
            tp=float(tp or 0),
            comment=comment,
            magic=int(magic),
            deviation=int(deviation),
        )

    def modify_position(self, ticket: int, sl: float = 0.0, tp: float = 0.0) -> dict[str, Any]:
        return self.request("trade_modify", ticket=int(ticket), sl=float(sl or 0), tp=float(tp or 0))

    def close_position(self, ticket: int, volume: float = 0.0, deviation: int = 30) -> dict[str, Any]:
        return self.request(
            "trade_close", ticket=int(ticket), volume=float(volume or 0), deviation=int(deviation)
        )
