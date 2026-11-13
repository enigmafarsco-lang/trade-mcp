"""Bridge over MetaTrader 5's built-in MCP server (no EA, no compilation).

Newer terminals expose their own MCP server on ``http://127.0.0.1:22346/mcp``
with ~107 tools: account, positions, chart history, market watch, trading,
news, economic calendar, MetaEditor, strategy tester.  This module maps our
internal command contract (the same one the EA and the mock speak) onto those
tool calls, so the console, the MCP tools and the built-in agents work
unchanged - and without compiling anything in MetaEditor.

Design notes
------------
* The tool *input* schemas are documented, but their *output* shapes are not
  published, so every field is read through :func:`find`, which looks a key up
  recursively and ignoring case/underscores.  Anything missing falls back to a
  sane default instead of raising.
* Tickets are strings on the MCP side and integers in our contract, volumes are
  floats on both sides.  Conversions happen here, in one place.
* ``trade_close`` cannot partially close: the native server only offers
  ``trade_close_single_position``.  We refuse the request explicitly instead of
  quietly closing the whole position.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import TF_SECONDS
from ..mcpclient import McpError, McpHttpClient
from .base import BridgeError

DEFAULT_URL = "http://127.0.0.1:22346/mcp"
MARKETDATA_URL = "https://www.metatrader.com/mcp"


# --------------------------------------------------------------------------- #
# Tolerant payload helpers                                                     #
# --------------------------------------------------------------------------- #
def _key(name: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def find(payload: Any, *names: str, depth: int = 6) -> Any:
    """First scalar value whose key matches one of ``names`` (order-insensitive)."""
    wanted = {_key(n) for n in names}
    queue: list[tuple[Any, int]] = [(payload, 0)]
    while queue:
        current, level = queue.pop(0)
        if level > depth:
            continue
        if isinstance(current, dict):
            for key, value in current.items():
                if _key(key) in wanted and not isinstance(value, (dict, list)):
                    return value
            for value in current.values():
                if isinstance(value, (dict, list)):
                    queue.append((value, level + 1))
        elif isinstance(current, list):
            for value in current[:8]:
                if isinstance(value, (dict, list)):
                    queue.append((value, level + 1))
    return None


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_text(value: Any, default: str = "") -> str:
    if value is None or isinstance(value, (dict, list)):
        return default
    return str(value)


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def records(payload: Any, *preferred: str, depth: int = 4) -> list[dict[str, Any]]:
    """The first list of objects found, preferring the given key names."""
    items: list[Any] | None = payload if isinstance(payload, list) else None
    if items is None and isinstance(payload, dict):
        wanted = {_key(n) for n in preferred}
        # preferred names win, at any depth (nested results are common)
        queue: list[tuple[Any, int]] = [(payload, 0)]
        while queue and items is None:
            current, level = queue.pop(0)
            if level > depth:
                continue
            if isinstance(current, dict):
                for key, value in current.items():
                    if _key(key) in wanted and isinstance(value, list):
                        items = value
                        break
                if items is None:
                    queue.extend((v, level + 1) for v in current.values() if isinstance(v, (dict, list)))
            elif isinstance(current, list) and current and isinstance(current[0], dict):
                items = current
        if items is None:  # last resort: the first list of objects anywhere
            queue = [(payload, 0)]
            while queue and items is None:
                current, level = queue.pop(0)
                if level > depth:
                    continue
                if isinstance(current, dict):
                    for value in current.values():
                        if isinstance(value, list) and value and isinstance(value[0], dict):
                            items = value
                            break
                        if isinstance(value, (dict, list)):
                            queue.append((value, level + 1))
    return [item for item in items or [] if isinstance(item, dict)]


def to_epoch(value: Any) -> int:
    """Accept epoch seconds, epoch milliseconds, or an ISO/naive datetime string."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        number = int(value)
        return number // 1000 if number > 100_000_000_000 else number
    text = str(value).strip()
    if not text:
        return 0
    if re.fullmatch(r"\d+", text):
        return to_epoch(int(text))
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return 0
    if parsed.tzinfo is None:  # server-time values are treated as UTC
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _side(value: Any) -> str:
    text = as_text(value).strip().lower()
    if text in {"buy", "long", "0", "position_type_buy", "order_type_buy", "deal_type_buy"}:
        return "buy"
    if text in {"sell", "short", "1", "position_type_sell", "order_type_sell", "deal_type_sell"}:
        return "sell"
    return text or "buy"


# --------------------------------------------------------------------------- #
# Bridge                                                                       #
# --------------------------------------------------------------------------- #
class NativeBridge:
    """Drop-in bridge that speaks MCP to the terminal."""

    backend = "native"

    def __init__(
        self,
        url: str = DEFAULT_URL,
        token: str = "",
        *,
        timeout: float = 30.0,
        default_symbol: str = "XAUUSD",
        client_name: str = "trade-mcp",
        client_version: str = "0.2.0",
    ):
        self.url = url
        self.token = token
        self.timeout = float(timeout)
        self.default_symbol = default_symbol
        self.client_name = client_name
        self.client_version = client_version
        self._client: McpHttpClient | None = None
        self._lock = threading.RLock()
        self._requests = 0
        self._errors = 0
        self._last_error = ""
        self.last_raw: dict[str, Any] = {}

    # ------------------------------------------------------------- plumbing
    @property
    def connected_hint(self) -> str:
        return f"mcp:{self.url}"

    def _connect(self) -> McpHttpClient:
        if self._client is None:
            client = McpHttpClient(
                self.url,
                self.token,
                timeout=self.timeout,
                client_name=self.client_name,
                client_version=self.client_version,
            )
            client.connect()
            self._client = client
        return self._client

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call one MCP tool, reconnecting once if the session went away."""
        with self._lock:
            result: dict[str, Any] = {}
            for attempt in (1, 2):
                try:
                    result = self._connect().call_tool(tool, arguments or {})
                    break
                except McpError as exc:
                    self._client = None
                    if attempt == 2:
                        self._errors += 1
                        self._last_error = str(exc)
                        raise BridgeError(f"MCP call '{tool}' failed: {exc}", code="mcp_error") from None
            self._requests += 1
            if result.get("isError"):
                message = McpHttpClient.text_content(result) or "the tool reported an error"
                self._errors += 1
                self._last_error = message
                raise BridgeError(f"{tool}: {message}", code="mcp_tool_error")
            body = McpHttpClient.structured_content(result)
            if isinstance(body, str):
                return {"text": body}
            return body if body is not None else {}

    def status(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "transport": "native",
            "backend": "native",
            "url": self.url,
            "ea_connected": False,
            "ea_peer": self.connected_hint,
            "ea_hello": None,
            "requests": self._requests,
            "errors": self._errors,
        }
        started = time.monotonic()
        try:
            raw = self.call("terminal_get_info")
        except BridgeError as exc:
            base["reason"] = str(exc)
            return base
        elapsed = (time.monotonic() - started) * 1000
        hello = {
            "ea": "MT5 native MCP",
            "version": as_text(find(raw, "version", "build"), "?"),
            "transport": "native",
            "url": self.url,
            "build": as_int(find(raw, "build"), 0),
            "company": as_text(find(raw, "company", "name")),
            "data_path": as_text(find(raw, "data_path", "data_folder")),
            "terminal_connected": as_bool(find(raw, "connected", "connection_established"), True),
            "trade_allowed": as_bool(find(raw, "trade_allowed"), True),
            "ea_trading_allowed": as_bool(find(raw, "trade_allowed", "algo_trading"), True),
            "ping_ms": round(elapsed, 1),
            "requests": self._requests,
            "errors": self._errors,
        }
        base.update({"ea_connected": True, "ea_hello": hello, "age_seconds": 0.0})
        if self._last_error:
            base["last_error"] = self._last_error
        return base

    def close(self) -> None:
        self._client = None

    # ------------------------------------------------------------ dispatch
    def request(self, cmd: str, **params: Any) -> Any:
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            raise BridgeError(
                f"the native MCP backend does not implement '{cmd}'", code="unsupported_command"
            )
        return handler(**params)

    # ------------------------------------------------------------- terminal
    def _cmd_ping(self, **_: Any) -> dict[str, Any]:
        info = self.call("get_time_information")
        return {
            "pong": True,
            "server_time": as_text(find(info, "utc_time", "server_time", "local_time")),
            "epoch": to_epoch(find(info, "utc_time", "server_time")) or int(time.time()),
            "symbol": self.default_symbol,
            "requests": self._requests,
            "errors": self._errors,
            "ea": "MT5 native MCP",
            "ea_version": self.client_version,
        }

    def _cmd_terminal_info(self, **_: Any) -> dict[str, Any]:
        raw = self.call("terminal_get_info")
        self.last_raw["terminal_get_info"] = raw
        return {
            "connected": as_bool(find(raw, "connected", "connection"), True),
            "trade_allowed": as_bool(find(raw, "trade_allowed"), True),
            "dll_allowed": as_bool(find(raw, "dll_allowed"), False),
            "community": as_bool(find(raw, "community_connected"), False),
            "build": as_int(find(raw, "build"), 0),
            "company": as_text(find(raw, "company", "name")),
            "name": as_text(find(raw, "name", "terminal_name"), "MetaTrader 5"),
            "path": as_text(find(raw, "path", "install_path")),
            "data_path": as_text(find(raw, "data_path", "data_folder")),
            "language": as_text(find(raw, "language")),
            "ea_trading_allowed": as_bool(find(raw, "trade_allowed", "algo_trading"), True),
            "ea_allow_trading_input": True,
            "ea": "MT5 native MCP",
            "ea_version": self.client_version,
            "transport": "native",
        }

    def _cmd_account_info(self, **_: Any) -> dict[str, Any]:
        raw = self.call("get_trading_account_info")
        self.last_raw["get_trading_account_info"] = raw
        account = raw.get("account", raw)
        mode = as_text(find(account, "trade_mode", "account_mode", "type"), "").lower()
        if not mode:
            demo = find(raw, "demo", "is_demo")
            mode = "demo" if as_bool(demo) else "real"
        return {
            "login": as_int(find(raw, "login", "account", "account_number"), 0),
            "server": as_text(find(raw, "server", "trade_server")),
            "name": as_text(find(raw, "name", "owner")),
            "company": as_text(find(raw, "company", "broker")),
            "currency": as_text(find(raw, "currency", "deposit_currency"), "USD"),
            "leverage": as_int(find(raw, "leverage")) if find(raw, "leverage") is not None else None,
            "balance": as_float(find(raw, "balance")),
            "equity": as_float(find(raw, "equity")),
            "margin": as_float(find(raw, "margin")),
            "margin_free": as_float(find(raw, "margin_free", "free_margin")),
            "margin_level": as_float(find(raw, "margin_level")),
            "profit": as_float(find(raw, "profit", "floating_profit", "equity_minus_balance")),
            "credit": as_float(find(raw, "credit")),
            "trade_allowed": as_bool(find(raw, "trade_allowed"), True),
            "trade_expert": as_bool(find(raw, "trade_expert", "expert_trading_allowed"), True),
            "trade_mode": mode if mode in {"demo", "real", "contest"} else "real",
        }

    # ------------------------------------------------------------- symbols
    def _symbols(self, symbol: str = "", limit: int = 500) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"limit": max(1, min(int(limit or 500), 5000))}
        if symbol:
            args["symbol"] = symbol
            args["include_hidden"] = True
        else:
            args["include_hidden"] = True
        raw = self.call("get_marketwatch_symbols", args)
        self.last_raw["get_marketwatch_symbols"] = raw
        return [self._symbol_record(record) for record in records(raw, "symbols", "market_watch")]

    @staticmethod
    def _symbol_record(record: dict[str, Any]) -> dict[str, Any]:
        name = as_text(find(record, "name", "symbol", "symbol_name"))
        digits = as_int(find(record, "digits"), 2)
        point = as_float(find(record, "point"))
        if point <= 0:
            point = 10.0 ** (-digits)
        bid = as_float(find(record, "bid"))
        ask = as_float(find(record, "ask"))
        spread = as_float(find(record, "spread"))
        if not spread and bid > 0 and ask > 0:
            spread = round((ask - bid) / point, 1)
        return {
            "name": name,
            "symbol": name,
            "description": as_text(find(record, "description", "display_name")),
            "path": as_text(find(record, "path", "group")),
            "digits": digits,
            "point": point,
            "spread": spread,
            "spread_points": spread,
            "spread_float": 0.0,
            "trade_stops_level": as_int(find(record, "trade_stops_level", "stops_level")),
            "trade_freeze_level": as_int(find(record, "trade_freeze_level", "freeze_level")),
            "trade_tick_value": as_float(find(record, "trade_tick_value", "tick_value")),
            "trade_tick_size": as_float(find(record, "trade_tick_size", "tick_size")) or point,
            "trade_contract_size": as_float(find(record, "trade_contract_size", "contract_size"), 1.0),
            "volume_min": as_float(find(record, "volume_min", "min_volume"), 0.01),
            "volume_max": as_float(find(record, "volume_max", "max_volume"), 100.0),
            "volume_step": as_float(find(record, "volume_step", "volume_step_size"), 0.01),
            "swap_long": as_float(find(record, "swap_long")),
            "swap_short": as_float(find(record, "swap_short")),
            "bid": bid,
            "ask": ask,
            "currency_base": as_text(find(record, "currency_base", "base_currency")),
            "currency_profit": as_text(find(record, "currency_profit", "profit_currency")),
            "currency_margin": as_text(find(record, "currency_margin", "margin_currency")),
            "visible": as_bool(find(record, "visible", "in_market_watch"), True),
            "trade_mode": as_text(find(record, "trade_mode"), "full"),
            "filling_mode": as_text(find(record, "filling_mode"), "both"),
            "native": True,
        }

    def _cmd_symbols_list(self, pattern: str = "", limit: int = 500, **_: Any) -> list[dict[str, Any]]:
        rows = self._symbols(limit=limit)
        if not pattern:
            return rows
        needle = pattern.upper()
        return [
            row
            for row in rows
            if needle in row["symbol"].upper() or needle in (row["description"] or "").upper()
        ]

    def _cmd_symbol_info(self, symbol: str = "", **_: Any) -> dict[str, Any]:
        wanted = symbol or self.default_symbol
        rows = self._symbols(symbol=wanted, limit=10)
        for row in rows:
            if row["symbol"].upper() == wanted.upper():
                return row
        if rows:
            return rows[0]
        raise BridgeError(f"symbol '{wanted}' is not in Market Watch", code="unknown_symbol")

    def _cmd_symbol_select(self, symbol: str = "", enable: bool = True, **_: Any) -> dict[str, Any]:
        tool = "add_marketwatch_symbol" if enable else "remove_marketwatch_symbol"
        self.call(tool, {"symbol": symbol})
        return {"symbol": symbol, "visible": bool(enable)}

    # ---------------------------------------------------------------- data
    def _cmd_tick(self, symbol: str = "", **_: Any) -> dict[str, Any]:
        wanted = symbol or self.default_symbol
        info = self._cmd_symbol_info(symbol=wanted)
        point = as_float(info.get("point")) or 0.0
        bid = as_float(info.get("bid"))
        ask = as_float(info.get("ask"))
        return {
            "time_source": "received",  # endpoint exposes no verified broker tick timestamp
            "time": int(time.time()),
            "time_str": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "bid": bid,
            "ask": ask,
            "last": as_float(info.get("last"), bid),
            "volume": as_float(info.get("tick_volume")),
            "spread_points": round((ask - bid) / point, 1) if point and bid and ask else 0.0,
        }

    def _cmd_rates(
        self, symbol: str = "", timeframe: str = "M15", count: int = 200, **_: Any
    ) -> dict[str, Any]:
        wanted = symbol or self.default_symbol
        period = (timeframe or "M15").upper()
        bars = max(1, min(int(count or 200), 5000))
        step = TF_SECONDS.get(period, 900)
        # Ask for a generous window so weekends/holidays cannot starve the response.
        now = datetime.now(timezone.utc)
        start = now - timedelta(seconds=step * (bars + 5) * 3)
        raw = self.call(
            "get_chart_history",
            {
                "datetime_from": iso(start),
                "datetime_to": iso(now + timedelta(seconds=step)),
                "symbol": wanted,
                "period": period,
                "limit": min(bars * 3, 20000),
            },
        )
        self.last_raw["get_chart_history"] = raw
        rows = records(raw, "candles", "rates", "bars", "history", "data")
        out: list[dict[str, Any]] = []
        for row in rows:
            opened = as_float(find(row, "open"))
            closed = as_float(find(row, "close"))
            if not opened and not closed:
                continue
            out.append(
                {
                    "time": to_epoch(find(row, "time", "datetime", "date", "timestamp")),
                    "open": opened,
                    "high": as_float(find(row, "high")),
                    "low": as_float(find(row, "low")),
                    "close": closed,
                    "tick_volume": as_float(find(row, "tick_volume", "volume", "tick_count")),
                    "real_volume": as_float(find(row, "real_volume"), 0.0),
                    "spread": as_int(find(row, "spread"), 0),
                }
            )
        out.sort(key=lambda bar: bar["time"])
        out = out[-bars:]
        if not out:
            raise BridgeError(
                f"get_chart_history returned no candles for {wanted} {period}", code="rates_unavailable"
            )
        return {"symbol": wanted, "timeframe": period, "count": len(out), "rates": out}

    # ------------------------------------------------------------- trading
    def _positions_and_orders(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        raw = self.call("get_trading_open_positions")
        self.last_raw["get_trading_open_positions"] = raw
        positions = [self._position_record(row) for row in records(raw, "positions", "open_positions")]
        orders = [self._order_record(row) for row in records(raw, "orders", "pending_orders")]
        return positions, orders

    @staticmethod
    def _position_record(row: dict[str, Any]) -> dict[str, Any]:
        symbol = as_text(find(row, "symbol", "name"))
        return {
            "ticket": as_int(find(row, "ticket", "position_ticket", "position_id", "id")),
            "symbol": symbol,
            "type": _side(find(row, "type", "side", "direction", "action")),
            "volume": as_float(find(row, "volume", "lots")),
            "price_open": as_float(find(row, "price_open", "open_price", "price")),
            "sl": as_float(find(row, "sl", "stop_loss")),
            "tp": as_float(find(row, "tp", "take_profit")),
            "price_current": as_float(find(row, "price_current", "current_price", "price_last")),
            "profit": as_float(find(row, "profit", "profit_loss")),
            "swap": as_float(find(row, "swap", "swaps")),
            "magic": as_int(find(row, "magic", "magic_number")),
            "comment": as_text(find(row, "comment")),
            "time": to_epoch(find(row, "time", "open_time", "create_time", "datetime")),
        }

    @staticmethod
    def _order_record(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "ticket": as_int(find(row, "ticket", "order_ticket", "id")),
            "symbol": as_text(find(row, "symbol", "name")),
            "type": as_text(find(row, "type", "order_type"), "buy_limit"),
            "volume": as_float(find(row, "volume", "lots")),
            "price_open": as_float(find(row, "price_open", "open_price", "price")),
            "sl": as_float(find(row, "sl", "stop_loss")),
            "tp": as_float(find(row, "tp", "take_profit")),
            "magic": as_int(find(row, "magic", "magic_number")),
            "time_setup": to_epoch(find(row, "time_setup", "time", "setup_time")),
            "comment": as_text(find(row, "comment")),
        }

    def _cmd_positions(self, **_: Any) -> list[dict[str, Any]]:
        return self._positions_and_orders()[0]

    def _cmd_orders(self, **_: Any) -> list[dict[str, Any]]:
        return self._positions_and_orders()[1]

    def _cmd_history_deals(
        self, date_from: int = 0, date_to: int = 0, **_: Any
    ) -> list[dict[str, Any]]:
        end = date_to or int(time.time())
        start = date_from or end - 30 * 86400
        raw = self.call(
            "get_trading_history_orders",
            {
                "datetime_from": iso(datetime.fromtimestamp(start, tz=timezone.utc)),
                "datetime_to": iso(datetime.fromtimestamp(end, tz=timezone.utc)),
                "include_orders": False,
                "include_deals": True,
                "limit": 10000,
            },
        )
        self.last_raw["get_trading_history_orders"] = raw
        out = []
        for row in records(raw, "deals"):
            entry = as_text(find(row, "entry", "deal_entry"), "").lower()
            out.append(
                {
                    "ticket": as_int(find(row, "ticket", "deal_ticket", "id")),
                    "position_id": as_int(find(row, "position_id", "position")),
                    "order_id": as_int(find(row, "order_id", "order")),
                    "symbol": as_text(find(row, "symbol", "name")),
                    "type": _side(find(row, "type", "deal_type", "side")),
                    "entry": "in" if "in" in entry else ("out" if "out" in entry else "inout"),
                    "volume": as_float(find(row, "volume", "lots")),
                    "price": as_float(find(row, "price")),
                    "profit": as_float(find(row, "profit")),
                    "swap": as_float(find(row, "swap", "swaps")),
                    "commission": as_float(find(row, "commission", "fee")),
                    "magic": as_int(find(row, "magic", "magic_number")),
                    "time": to_epoch(find(row, "time", "datetime")),
                    "comment": as_text(find(row, "comment")),
                }
            )
        out.sort(key=lambda deal: deal["time"])
        return out

    def _cmd_calc_order(
        self, symbol: str = "", side: str = "buy", volume: float = 0.01, price: float = 0.0, **_: Any
    ) -> dict[str, Any]:
        wanted = symbol or self.default_symbol
        if not price:
            info = self._cmd_symbol_info(symbol=wanted)
            price = as_float(info.get("ask") if _side(side) == "buy" else info.get("bid"))
        if not price:
            raise BridgeError(f"no price for {wanted}", code="price_unavailable")
        raw = self.call(
            "trade_order_calc_margin",
            {"symbol": wanted, "type": _side(side), "volume": float(volume), "price": float(price)},
        )
        margin = as_float(find(raw, "margin", "margin_required"))
        account = self._cmd_account_info()
        free = as_float(account.get("margin_free"))
        return {
            "margin": margin,
            "price": float(price),
            "volume": float(volume),
            "free_margin_after": round(free - margin, 2),
        }

    @staticmethod
    def _trade_result(raw: Any, ticket: int = 0) -> dict[str, Any]:
        retcode = as_int(find(raw, "retcode", "return_code", "code"), 0)
        order = as_int(find(raw, "order", "order_ticket"), 0)
        deal = as_int(find(raw, "deal", "deal_ticket"), 0)
        ok = as_bool(find(raw, "ok", "success"), False)
        if retcode == 0 and (ok or order or deal):
            retcode = 10009  # TRADE_RETCODE_DONE
        return {
            "retcode": retcode,
            "retcode_text": as_text(
                find(raw, "retcode_text", "description", "message", "comment"),
                "done" if retcode == 10009 else "unknown",
            ),
            "order": order,
            "deal": deal,
            "ticket": ticket or deal or order,
            "volume": as_float(find(raw, "volume")),
            "price": as_float(find(raw, "price")),
            "comment": as_text(find(raw, "comment", "message")),
        }

    def _cmd_trade_open(
        self,
        symbol: str = "",
        side: str = "buy",
        volume: float = 0.01,
        sl: float = 0.0,
        tp: float = 0.0,
        comment: str = "trade-mcp",
        magic: int = 0,
        deviation: int = 0,
        **_: Any,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "symbol": symbol or self.default_symbol,
            "type": _side(side),
            "volume": float(volume),
        }
        if sl:
            args["sl"] = float(sl)
        if tp:
            args["tp"] = float(tp)
        if comment:
            args["comment"] = str(comment)[:31]
        raw = self.call("trade_send_market_order", args)
        self.last_raw["trade_send_market_order"] = raw
        return self._trade_result(raw)

    def _cmd_trade_modify(
        self, ticket: int = 0, sl: float = 0.0, tp: float = 0.0, **_: Any
    ) -> dict[str, Any]:
        positions, _ = self._positions_and_orders()
        target = next((pos for pos in positions if pos["ticket"] == int(ticket)), None)
        if target is None:
            raise BridgeError(f"position {ticket} not found", code="not_found")
        args: dict[str, Any] = {
            "symbol": target["symbol"],
            "position_ticket": str(int(ticket)),
        }
        if sl:
            args["sl"] = float(sl)
        if tp:
            args["tp"] = float(tp)
        raw = self.call("trade_modify_sl_tp", args)
        return self._trade_result(raw, int(ticket))

    def _cmd_trade_close(
        self, ticket: int = 0, volume: float = 0.0, deviation: int = 0, **_: Any
    ) -> dict[str, Any]:
        positions, _ = self._positions_and_orders()
        target = next((pos for pos in positions if pos["ticket"] == int(ticket)), None)
        if target is None:
            raise BridgeError(f"position {ticket} not found", code="not_found")
        open_volume = as_float(target.get("volume"))
        if volume and open_volume and float(volume) < open_volume - 1e-6:
            raise BridgeError(
                "the native MCP server can only close a whole position "
                f"(asked {volume} of {open_volume})",
                code="partial_close_unsupported",
            )
        raw = self.call(
            "trade_close_single_position",
            {"symbol": target["symbol"], "position_ticket": str(int(ticket))},
        )
        return self._trade_result(raw, int(ticket))
