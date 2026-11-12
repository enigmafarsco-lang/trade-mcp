"""A fake MetaTrader that speaks the same protocol as the MQL5 EA.

Purpose: develop, test and demo the MCP server without MetaTrader, Wine or a
broker account. Every payload is tagged ``"mock": true`` so the model (and you)
can never mistake it for live data.

Prices are deterministic harmonic curves, not random per call: the same
timestamp always produces the same bar, so multi-timeframe analysis, repeated
tool calls and tests all stay consistent.
"""

from __future__ import annotations

import hashlib
import math
import time
from datetime import datetime, timezone
from typing import Any

from ..config import TF_SECONDS
from .base import BridgeError

TWO_PI = 2 * math.pi


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unit(*parts: Any) -> float:
    """Deterministic pseudo-random float in [0, 1)."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:6], "big") / float(1 << 48)


class _Series:
    """Smooth deterministic price curve for one symbol."""

    BASE = 2650.0
    SCALE = 0.8

    def __init__(self, symbol: str, base: float, scale: float, digits: int, point: float):
        self.symbol = symbol
        self.base = base
        self.scale = scale
        self.digits = digits
        self.point = point
        periods = (240, 720, 2880, 10080, 43200, 129600)
        amplitudes = (0.35, 0.8, 1.8, 4.2, 9.0, 18.0)
        self.harmonics = []
        for period, amp in zip(periods, amplitudes, strict=False):
            phase = _unit(symbol, "phase", period) * TWO_PI
            jitter = 0.55 + 0.9 * _unit(symbol, "amp", period)
            self.harmonics.append((period, amp * scale * jitter, phase))

    def price(self, ts: float) -> float:
        minutes = ts / 60.0
        value = self.base
        for period, amp, phase in self.harmonics:
            value += amp * math.sin(TWO_PI * minutes / period + phase)
        return round(value, self.digits)

    def bar_wick(self) -> float:
        return max(self.point * 20, self.scale * 0.35)


SYMBOL_TABLE: dict[str, dict[str, Any]] = {
    "XAUUSD": dict(
        description="Gold vs US Dollar", base=2650.0, scale=0.8, digits=2, point=0.01,
        contract=100.0, tick_value=1.0, tick_size=0.01, spread=25, stops_level=0,
        volume_min=0.01, volume_max=100.0, volume_step=0.01, path="Metals\\Gold",
        visible=True,
    ),
    "XAUUSD.a": dict(
        description="Gold vs US Dollar (account suffix variant)", base=2650.0, scale=0.8,
        digits=2, point=0.01, contract=100.0, tick_value=1.0, tick_size=0.01, spread=28,
        stops_level=0, volume_min=0.01, volume_max=50.0, volume_step=0.01,
        path="Metals\\Gold", visible=False,
    ),
    "XAGUSD": dict(
        description="Silver vs US Dollar", base=31.5, scale=0.9, digits=3, point=0.001,
        contract=5000.0, tick_value=5.0, tick_size=0.001, spread=30, stops_level=0,
        volume_min=0.01, volume_max=100.0, volume_step=0.01, path="Metals\\Silver",
        visible=True,
    ),
    "EURUSD": dict(
        description="Euro vs US Dollar", base=1.0850, scale=0.004, digits=5, point=0.00001,
        contract=100000.0, tick_value=1.0, tick_size=0.00001, spread=12, stops_level=0,
        volume_min=0.01, volume_max=100.0, volume_step=0.01, path="Forex\\Majors",
        visible=True,
    ),
    "GBPUSD": dict(
        description="British Pound vs US Dollar", base=1.2720, scale=0.004, digits=5,
        point=0.00001, contract=100000.0, tick_value=1.0, tick_size=0.00001, spread=15,
        stops_level=0, volume_min=0.01, volume_max=100.0, volume_step=0.01,
        path="Forex\\Majors", visible=True,
    ),
    "USDJPY": dict(
        description="US Dollar vs Japanese Yen", base=154.20, scale=0.35, digits=3,
        point=0.001, contract=100000.0, tick_value=0.65, tick_size=0.001, spread=14,
        stops_level=0, volume_min=0.01, volume_max=100.0, volume_step=0.01,
        path="Forex\\Majors", visible=True,
    ),
}


class MockBridge:
    """In-memory implementation of the bridge protocol."""

    backend = "mock"

    def __init__(self, balance: float = 10_000.0, leverage: int = 100, seed: int = 7):
        self.seed = seed
        self.balance = float(balance)
        self.credit = 0.0
        self.leverage = int(leverage)
        self.login = 5000123
        self.server = "MockBroker-Demo"
        self._series = {
            name: _Series(name, spec["base"], spec["scale"], spec["digits"], spec["point"])
            for name, spec in SYMBOL_TABLE.items()
        }
        self._visible = {name: bool(spec["visible"]) for name, spec in SYMBOL_TABLE.items()}
        self._positions: dict[int, dict[str, Any]] = {}
        self._next_ticket = 500_000
        self._started = time.time()
        self._closed = self._seed_history()

    # ------------------------------------------------------------------ misc
    def _now(self) -> float:
        return time.time()

    def _price(self, symbol: str, ts: float | None = None) -> float:
        series = self._series.get(symbol)
        if series is None:
            raise BridgeError(f"unknown symbol '{symbol}'", code="unknown_symbol")
        return series.price(ts if ts is not None else self._now())

    def _spread(self, symbol: str) -> float:
        return float(SYMBOL_TABLE[symbol]["spread"])

    def _quote(self, symbol: str) -> tuple[float, float]:
        spec = SYMBOL_TABLE[symbol]
        bid = self._price(symbol)
        return round(bid, spec["digits"]), round(bid + spec["spread"] * spec["point"], spec["digits"])

    def _symbol_info(self, symbol: str) -> dict[str, Any]:
        spec = SYMBOL_TABLE.get(symbol)
        if spec is None:
            raise BridgeError(f"unknown symbol '{symbol}'", code="unknown_symbol")
        bid, ask = self._quote(symbol)
        return {
            "name": symbol,
            "description": spec["description"],
            "path": spec["path"],
            "digits": spec["digits"],
            "point": spec["point"],
            "spread": spec["spread"],
            "spread_points": float(spec["spread"]),
            "trade_stops_level": spec["stops_level"],
            "trade_freeze_level": 0,
            "trade_tick_value": spec["tick_value"],
            "trade_tick_size": spec["tick_size"],
            "trade_contract_size": spec["contract"],
            "volume_min": spec["volume_min"],
            "volume_max": spec["volume_max"],
            "volume_step": spec["volume_step"],
            "trade_mode": "full",
            "currency_base": symbol[:3],
            "currency_profit": "USD" if symbol not in {"USDJPY"} else "JPY",
            "currency_margin": "USD",
            "swap_long": -2.4,
            "swap_short": 0.6,
            "filling_mode": "fok",
            "visible": self._visible[symbol],
            "bid": bid,
            "ask": ask,
            "mock": True,
        }

    def _seed_history(self) -> list[dict[str, Any]]:
        """A few closed trades so history reports have something to chew on."""
        now = self._now()
        out: list[dict[str, Any]] = []
        plans = [
            ("XAUUSD", "buy", 0.05, 5, 78.0),
            ("XAUUSD", "sell", 0.03, 9, -22.0),
            ("XAUUSD", "buy", 0.02, 14, 41.5),
            ("EURUSD", "sell", 0.10, 20, -12.0),
            ("XAUUSD", "buy", 0.04, 26, 96.0),
        ]
        for i, (symbol, side, volume, days_ago, profit) in enumerate(plans):
            price = self._price(symbol, now - days_ago * 86400)
            out.append(
                {
                    "ticket": 400_000 + i,
                    "position_id": 300_000 + i,
                    "symbol": symbol,
                    "type": side,
                    "volume": volume,
                    "price": price,
                    "profit": profit,
                    "swap": -0.5,
                    "commission": -0.7,
                    "time": int(now - days_ago * 86400),
                    "comment": "mock:seeded",
                    "mock": True,
                }
            )
        return out

    # ------------------------------------------------------------- positions
    def _position_view(self, pos: dict[str, Any]) -> dict[str, Any]:
        bid, ask = self._quote(pos["symbol"])
        current = bid if pos["type"] == "buy" else ask
        spec = SYMBOL_TABLE[pos["symbol"]]
        direction = 1 if pos["type"] == "buy" else -1
        profit = (current - pos["price_open"]) * direction * pos["volume"] * spec["contract"]
        out = dict(pos)
        out["price_current"] = current
        out["profit"] = round(profit, 2)
        out["mock"] = True
        return out

    def _equity(self) -> float:
        return round(self.balance + self.credit + sum(
            self._position_view(p)["profit"] for p in self._positions.values()
        ), 2)

    def _margin_used(self) -> float:
        total = 0.0
        for pos in self._positions.values():
            spec = SYMBOL_TABLE[pos["symbol"]]
            total += pos["volume"] * spec["contract"] * pos["price_open"] / self.leverage
        return round(total, 2)

    # --------------------------------------------------------------- dispatch
    def request(self, cmd: str, **params: Any) -> Any:
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            raise BridgeError(f"unknown command '{cmd}'", code="unknown_command")
        return handler(**params)

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "backend": "mock",
            "ea_connected": True,
            "mock": True,
            "note": "Mock backend - synthetic prices, no broker involved.",
            "uptime_seconds": round(self._now() - self._started, 1),
        }

    def close(self) -> None:  # pragma: no cover - nothing to release
        return None

    # ---------------------------------------------------------------- commands
    def _cmd_ping(self) -> dict[str, Any]:
        now = self._now()
        return {"pong": True, "server_time": _iso(now), "epoch": int(now), "mock": True}

    def _cmd_terminal_info(self) -> dict[str, Any]:
        return {
            "connected": True,
            "trade_allowed": True,
            "dll_allowed": True,
            "build": "mock",
            "company": "MockBroker Ltd",
            "name": "Mock Terminal",
            "path": "/mock/terminal64.exe",
            "data_path": "/mock",
            "mock": True,
        }

    def _cmd_account_info(self) -> dict[str, Any]:
        equity = self._equity()
        margin = self._margin_used()
        free = round(equity - margin, 2)
        return {
            "login": self.login,
            "server": self.server,
            "name": "Mock Trader",
            "company": "MockBroker Ltd",
            "currency": "USD",
            "leverage": self.leverage,
            "balance": round(self.balance, 2),
            "equity": equity,
            "margin": margin,
            "margin_free": free,
            "margin_level": round(equity / margin * 100, 2) if margin else 0.0,
            "profit": round(equity - self.balance, 2),
            "credit": self.credit,
            "trade_allowed": True,
            "trade_mode": "demo",
            "mock": True,
        }

    def _cmd_symbols_list(self, pattern: str = "", limit: int = 500) -> list[dict[str, Any]]:
        pat = (pattern or "").upper()
        out = []
        for name, spec in SYMBOL_TABLE.items():
            if pat and pat not in name.upper() and pat not in spec["path"].upper():
                continue
            out.append(
                {
                    "symbol": name,
                    "description": spec["description"],
                    "path": spec["path"],
                    "visible": self._visible[name],
                }
            )
        return out[: int(limit)]

    def _cmd_symbol_info(self, symbol: str = "") -> dict[str, Any]:
        return self._symbol_info(symbol)

    def _cmd_symbol_select(self, symbol: str = "", enable: bool = True) -> dict[str, Any]:
        if symbol not in SYMBOL_TABLE:
            raise BridgeError(f"unknown symbol '{symbol}'", code="unknown_symbol")
        self._visible[symbol] = bool(enable)
        return {"symbol": symbol, "visible": self._visible[symbol]}

    def _cmd_rates(self, symbol: str = "", timeframe: str = "M15", count: int = 200):
        if symbol not in SYMBOL_TABLE:
            raise BridgeError(f"unknown symbol '{symbol}'", code="unknown_symbol")
        tf = str(timeframe).upper()
        step = TF_SECONDS.get(tf)
        if step is None:
            raise BridgeError(f"unknown timeframe '{timeframe}'", code="bad_timeframe")
        count = max(1, min(int(count), 5000))
        spec = SYMBOL_TABLE[symbol]
        series = self._series[symbol]
        end = int(self._now() // step) * step
        wick = series.bar_wick()
        bars = []
        for i in range(count):
            t = end - (count - 1 - i) * step
            o = series.price(t)
            c = series.price(t + step)
            top, bot = max(o, c), min(o, c)
            wu, wd = _unit(symbol, t, "u"), _unit(symbol, t, "d")
            high = round(top + wick * wu, spec["digits"])
            low = round(bot - wick * wd, spec["digits"])
            bars.append(
                {
                    "time": int(t),
                    "open": o,
                    "high": high,
                    "low": low,
                    "close": c,
                    "tick_volume": 200 + int(_unit(symbol, t, "v") * 1800),
                    "spread": spec["spread"],
                    "real_volume": 0,
                }
            )
        return {"symbol": symbol, "timeframe": tf, "count": len(bars), "rates": bars, "mock": True}

    def _cmd_tick(self, symbol: str = "") -> dict[str, Any]:
        bid, ask = self._quote(symbol)
        return {
            "time": int(self._now()),
            "time_str": _iso(self._now()),
            "bid": bid,
            "ask": ask,
            "last": bid,
            "volume": 1,
            "spread_points": (ask - bid) / SYMBOL_TABLE[symbol]["point"],
            "mock": True,
        }

    def _cmd_positions(self) -> list[dict[str, Any]]:
        return [self._position_view(p) for p in self._positions.values()]

    def _cmd_orders(self) -> list[dict[str, Any]]:
        return []

    def _cmd_history_deals(self, date_from: int = 0, date_to: int = 0) -> list[dict[str, Any]]:
        lo = int(date_from or 0)
        hi = int(date_to or self._now() + 86400)
        return [d for d in self._closed if lo <= d["time"] <= hi]

    def _cmd_calc_order(
        self, symbol: str = "", side: str = "buy", volume: float = 0.01, price: float = 0.0
    ) -> dict[str, Any]:
        spec = SYMBOL_TABLE.get(symbol)
        if spec is None:
            raise BridgeError(f"unknown symbol '{symbol}'", code="unknown_symbol")
        bid, ask = self._quote(symbol)
        px = float(price) or (ask if str(side).lower() == "buy" else bid)
        margin = volume * spec["contract"] * px / self.leverage
        return {
            "margin": round(margin, 2),
            "margin_free_after": round(self._equity() - self._margin_used() - margin, 2),
            "price": px,
            "volume": volume,
            "mock": True,
        }

    def _cmd_trade_open(
        self,
        symbol: str = "",
        side: str = "buy",
        volume: float = 0.01,
        sl: float = 0.0,
        tp: float = 0.0,
        comment: str = "",
        magic: int = 0,
        **_: Any,
    ) -> dict[str, Any]:
        spec = SYMBOL_TABLE.get(symbol)
        if spec is None:
            raise BridgeError(f"unknown symbol '{symbol}'", code="unknown_symbol")
        side = str(side).lower()
        if side not in {"buy", "sell"}:
            raise BridgeError("side must be 'buy' or 'sell'", code="bad_request")
        volume = round(float(volume), 2)
        if volume < spec["volume_min"] or volume > spec["volume_max"]:
            raise BridgeError(
                f"volume {volume} outside {spec['volume_min']}-{spec['volume_max']}",
                code="bad_volume",
            )
        bid, ask = self._quote(symbol)
        price = ask if side == "buy" else bid
        self._next_ticket += 1
        ticket = self._next_ticket
        self._positions[ticket] = {
            "ticket": ticket,
            "symbol": symbol,
            "type": side,
            "volume": volume,
            "price_open": price,
            "sl": float(sl),
            "tp": float(tp),
            "swap": 0.0,
            "magic": int(magic),
            "comment": comment or "trade-mcp",
            "time": int(self._now()),
            "mock": True,
        }
        return {
            "retcode": 10009,
            "retcode_text": "done (mock)",
            "order": ticket,
            "deal": ticket,
            "ticket": ticket,
            "volume": volume,
            "price": price,
            "symbol": symbol,
            "mock": True,
        }

    def _cmd_trade_modify(
        self, ticket: int = 0, sl: float = 0.0, tp: float = 0.0, **_: Any
    ) -> dict[str, Any]:
        pos = self._positions.get(int(ticket))
        if pos is None:
            raise BridgeError(f"position {ticket} not found", code="not_found")
        pos["sl"] = float(sl)
        pos["tp"] = float(tp)
        return {"retcode": 10009, "retcode_text": "done (mock)", "ticket": int(ticket), "mock": True}

    def _cmd_trade_close(
        self, ticket: int = 0, volume: float = 0.0, **_: Any
    ) -> dict[str, Any]:
        pos = self._positions.get(int(ticket))
        if pos is None:
            raise BridgeError(f"position {ticket} not found", code="not_found")
        close_volume = round(float(volume or pos["volume"]), 2)
        view = self._position_view(pos)
        profit = view["profit"] * (close_volume / pos["volume"])
        self.balance = round(self.balance + profit, 2)
        self._closed.append(
            {
                "ticket": int(self._now() * 1000) % 10**9,
                "position_id": pos["ticket"],
                "symbol": pos["symbol"],
                "type": "sell" if pos["type"] == "buy" else "buy",
                "volume": close_volume,
                "price": view["price_current"],
                "profit": round(profit, 2),
                "swap": 0.0,
                "commission": 0.0,
                "time": int(self._now()),
                "comment": "mock:close",
                "mock": True,
            }
        )
        if close_volume >= pos["volume"] - 1e-9:
            self._positions.pop(pos["ticket"], None)
        else:
            pos["volume"] = round(pos["volume"] - close_volume, 2)
        return {
            "retcode": 10009,
            "retcode_text": "done (mock)",
            "ticket": int(ticket),
            "volume": close_volume,
            "profit": round(profit, 2),
            "mock": True,
        }

    def _cmd_trade_close_all(self, symbol: str = "", **_: Any) -> dict[str, Any]:
        targets = [
            t for t, p in self._positions.items() if not symbol or p["symbol"] == symbol
        ]
        results = [self._cmd_trade_close(ticket=t) for t in targets]
        return {"closed": len(results), "results": results, "mock": True}
