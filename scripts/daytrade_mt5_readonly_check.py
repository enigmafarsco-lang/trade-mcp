#!/usr/bin/env python3
"""One bounded, read-only MT5/Day Trader smoke check, with private raw evidence.

Uses the local native MT5 MCP endpoint only. The only terminal tools allowed are
terminal_get_info, get_time_information, get_marketwatch_symbols (twice), and
get_chart_history (once). No account, order, position, symbol-selection, model,
notification or journal operation is called. Advisory MCP tools run in-process
on the captured data. An isolated loopback-only mock console also consumes those
inputs; neither check proves a running OpenCode or workstation console was updated.
See docs/OPENCODE-DAYTRADE-LIVE.md for the exact interpretation and limitations.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import ipaddress
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from publish_runtime_report import sanitize  # noqa: E402

from trade_mcp.bridge.native import find, records, terminal_connection, to_epoch  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.daytrade import m1_rows  # noqa: E402
from trade_mcp.mcpclient import McpError, McpHttpClient  # noqa: E402

RAW_LIMITS = {"terminal_get_info": 1, "get_time_information": 1,
              "get_marketwatch_symbols": 2, "get_chart_history": 1}
MCP_TOOLS = {
    "daytrade_entry_signal", "daytrade_exit_plan", "daytrade_historical_replay",
    "daytrade_time_machine", "daytrade_check_alert", "daytrade_scan", "daytrade_alert_rules",
    "daytrade_review_limits", "daytrade_parse_news", "daytrade_stock_facts",
    "daytrade_stock_disclosures",
}
QUOTE_CHECKS = ("daytrade_live_exit_plan", "daytrade_live_sizing", "daytrade_live_alert_snapshot",
                "daytrade_live_alert_rules", "daytrade_live_risk_limits")
BAR_CHECKS = ("daytrade_live_entry_buy", "daytrade_live_entry_sell", "daytrade_live_scan_patterns",
              "daytrade_live_scan_range", "daytrade_live_historical_replay", "daytrade_live_time_machine")
FIXTURE_CHECKS = ("daytrade_fixture_news", "daytrade_fixture_stock_facts",
                  "daytrade_fixture_stock_disclosures")
HTTP_OPERATIONS = {
    "daytrade_entry_signal": "entry", "daytrade_exit_plan": "plan",
    "daytrade_historical_replay": "history", "daytrade_time_machine": "time-machine",
    "daytrade_check_alert": "alerts", "daytrade_scan": "scan", "daytrade_alert_rules": "alert-rules",
    "daytrade_review_limits": "review", "daytrade_parse_news": "news",
    "daytrade_stock_facts": "stock-facts", "daytrade_stock_disclosures": "stock-disclosures",
}


class Blocked(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def private_file(path: Path, text: str) -> None:
    # The directory is newly created and owner-only; never overwrite prior evidence.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)


def private_json(path: Path, value: Any) -> None:
    # Raw provider payloads may themselves contain non-JSON floats. Preserve them
    # privately, then reject them during numeric validation instead of inventing data.
    private_file(path, json.dumps(value, indent=2, default=str) + "\n")


def local_terminal_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                    and not parsed.username and not parsed.password and not parsed.query
                    and not parsed.fragment and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        return False


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        # Do not forward a local terminal Bearer header to any redirected host.
        return None


def number(value: Any) -> float:
    if value is None or isinstance(value, bool):
        raise Blocked("invalid_data")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise Blocked("invalid_data") from None
    if not math.isfinite(result):
        raise Blocked("invalid_data")
    return result


def timestamp(value: Any) -> datetime | None:
    # Numbers and timezone-less strings are not assumed to be UTC.
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


def aware(value: datetime | None) -> bool:
    return value is not None and value.tzinfo is not None and value.utcoffset() is not None


def explicit_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0"}:
        return value.strip().lower() in {"true", "1"}
    return None


def exact_symbol(body: Any, symbol: str) -> dict[str, Any]:
    matches = [row for row in records(body, "symbols", "market_watch")
               if str(find(row, "name", "symbol", "symbol_name") or "").upper() == symbol.upper()]
    if len(matches) != 1:
        raise Blocked("symbol_unavailable")
    return matches[0]


def quote(row: dict[str, Any]) -> dict[str, float]:
    bid, ask = number(find(row, "bid")), number(find(row, "ask"))
    if not 0 < bid <= ask <= 1e12:
        raise Blocked("invalid_data")
    return {"bid": bid, "ask": ask}


def reported_spec(row: dict[str, Any]) -> dict[str, float]:
    # No NativeBridge defaults: all values here must actually be reported.
    aliases = {"trade_tick_size": ("trade_tick_size", "tick_size"),
               "trade_tick_value": ("trade_tick_value", "tick_value"),
               "volume_step": ("volume_step", "volume_step_size"),
               "volume_min": ("volume_min", "min_volume"),
               "volume_max": ("volume_max", "max_volume"),
               "point": ("point",), "trade_stops_level": ("trade_stops_level", "stops_level")}
    try:
        spec = {key: number(find(row, *names)) for key, names in aliases.items()}
    except Blocked:
        raise Blocked("missing_broker_spec") from None
    if (any(value <= 0 for key, value in spec.items() if key != "trade_stops_level")
            or spec["trade_stops_level"] < 0):
        raise Blocked("missing_broker_spec")
    return spec


def compact_bars(body: Any) -> tuple[list[list[float]], list[Any]]:
    raw = records(body, "candles", "rates", "bars", "history", "data")
    if not raw:
        raise Blocked("empty_data")
    if len(raw) > 1500:
        raise Blocked("invalid_data")
    converted, raw_times, kinds = [], [], set()
    try:
        for row in raw:
            raw_time = find(row, "time", "datetime", "date", "timestamp")
            dt = timestamp(raw_time)
            kinds.add("aware" if aware(dt) else "naive" if dt is not None else "opaque")
            stamp = to_epoch(raw_time)
            vals = [number(find(row, "open")), number(find(row, "high")),
                    number(find(row, "low")), number(find(row, "close")),
                    number(find(row, "tick_volume", "volume", "tick_count"))]
            raw_times.append(raw_time)
            converted.append([stamp, *vals])
        if len(kinds) != 1:
            raise Blocked("timestamp_unverified")
        clean = m1_rows(converted)
    except (ValueError, TypeError, OverflowError):
        raise Blocked("invalid_data") from None
    if len(clean) < 2:
        raise Blocked("insufficient_history")
    return clean, raw_times


def contiguous_tail(rows: list[list[float]]) -> list[list[float]]:
    start = len(rows) - 1
    while start > 0 and int(rows[start][0] - rows[start - 1][0]) == 60:
        start -= 1
    return rows[start:]


def broker_window(clock: Any) -> dict[str, str | int]:
    value = find(clock, "trade_server_time")
    if value is None:
        value = find(clock, "trade_server_last_known_time")
    dt = timestamp(value)
    if dt is None:
        raise Blocked("timestamp_unverified")
    # History's request uses the displayed broker wall-clock label, not a guessed
    # host/UTC offset. A last-known clock can be stale; freshness is checked separately.
    label = dt.replace(tzinfo=None)
    start, end = label - timedelta(hours=24), label + timedelta(minutes=1)
    return {"datetime_from": start.isoformat(timespec="seconds"),
            "datetime_to": end.isoformat(timespec="seconds"), "period": "M1", "limit": 1500}


def freshness(clock: Any, raw_last_time: Any, observed_at: float, count: int) -> tuple[str, str | None]:
    utc, last = timestamp(find(clock, "utc_time")), timestamp(raw_last_time)
    if count >= 1500 or not aware(utc) or not aware(last):
        return "blocked", "timestamp_unverified"
    assert utc is not None and last is not None
    if abs(utc.timestamp() - observed_at) > 30:
        return "blocked", "clock_mismatch"
    age = observed_at - last.timestamp()
    if age < -5:
        return "blocked", "clock_mismatch"
    return ("pass", None) if age <= 120 else ("fail", "stale_data")


def no_network(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("Network access is forbidden while exercising advisory tools")


@contextlib.contextmanager
def offline_advisory() -> Any:
    from trade_mcp import app

    if app._APP is not None:
        raise RuntimeError("An in-process app was unexpectedly initialized")
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(app, "build_app", side_effect=no_network))
        for name in ("connect", "connect_ex", "sendto", "sendmsg"):
            if hasattr(socket.socket, name):
                stack.enter_context(patch.object(socket.socket, name, no_network))
        for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            stack.enter_context(patch.object(socket, name, no_network))
        yield


@contextlib.contextmanager
def loopback_native() -> Any:
    original_connect = socket.socket.connect

    def connect(sock: Any, address: Any) -> Any:
        try:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_loopback:
                raise ValueError
        except (TypeError, ValueError, IndexError):
            raise RuntimeError("Native terminal traffic must resolve to loopback") from None
        return original_connect(sock, address)

    with patch.object(socket.socket, "connect", connect):
        yield


@contextlib.contextmanager
def only_loopback_port(port: int) -> Any:
    original_connect, original_lookup = socket.socket.connect, socket.getaddrinfo

    def connect(sock: Any, address: Any) -> Any:
        if address != ("127.0.0.1", port):
            raise RuntimeError("Only the isolated test server is reachable")
        return original_connect(sock, address)

    def lookup(host: Any, service: Any, *args: Any, **kwargs: Any) -> Any:
        if host != "127.0.0.1" or int(service) != port:
            raise RuntimeError("Only the isolated test server may be resolved")
        return original_lookup(host, service, *args, **kwargs)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(socket.socket, "connect", connect))
        stack.enter_context(patch.object(socket, "getaddrinfo", lookup))
        for name in ("connect_ex", "sendto", "sendmsg"):
            if hasattr(socket.socket, name):
                stack.enter_context(patch.object(socket.socket, name, no_network))
        for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            stack.enter_context(patch.object(socket, name, no_network))
        yield


class NoAppIO:
    def __getattr__(self, _name: str) -> Any:
        raise RuntimeError("The isolated HTTP app has no broker, journal or model")


def failure_reason(exc: Exception) -> str:
    if isinstance(exc, Blocked):
        return exc.reason
    if isinstance(exc, McpError) and exc.status in {401, 403}:
        return "authentication_failed"
    return "connection_unavailable"


class Runner:
    def __init__(self, output: Path, commit: str, symbol: str):
        self.output, self.symbol = output, symbol
        self.report: dict[str, Any] = {"commit": commit, "checks": {}, "counts": {}, "reasons": {}}
        self.calls = {name: 0 for name in RAW_LIMITS}
        self.cases: list[tuple[str, dict[str, Any], dict[str, Any], str]] = []
        self.tools: dict[str, Any] = {}
        self.client: Any = None

    def state(self, name: str, value: str, reason: str | None = None) -> None:
        self.report["checks"][name] = value
        if reason is None:
            self.report["reasons"].pop(name, None)
        else:
            self.report["reasons"][name] = reason

    def block(self, names: Any, reason: str) -> None:
        for name in names:
            if name not in self.report["checks"]:
                self.state(name, "blocked", reason)

    def exception(self, label: str) -> None:
        private_file(self.output / f"{label}-exception.txt", traceback.format_exc())

    def native(self, name: str, args: dict[str, Any], label: str) -> Any:
        if name not in RAW_LIMITS or self.calls[name] >= RAW_LIMITS[name]:
            raise Blocked("unsafe_operation_excluded")
        descriptor = self.tools.get(name)
        if not isinstance(descriptor, dict):
            raise Blocked("tool_unavailable")
        schema = descriptor.get("inputSchema")
        if not isinstance(schema, dict) or not isinstance(schema.get("properties", {}), dict):
            raise Blocked("schema_mismatch")
        props, required = schema.get("properties", {}), schema.get("required", [])
        if (not isinstance(required, list) or not all(isinstance(x, str) for x in required)
                or not set(args) <= set(props) or not set(required) <= set(args)):
            raise Blocked("schema_mismatch")
        self.calls[name] += 1  # Count the attempt, including errors; there is no retry.
        result = self.client.call_tool(name, args)
        private_json(self.output / f"native-{label}.json", {"arguments": args, "response": result})
        data = McpHttpClient.structured_content(result)
        if (not isinstance(result, dict) or result.get("isError")
                or isinstance(data, dict) and (data.get("ok") is False or bool(data.get("error")))):
            raise Blocked("tool_error")
        if not isinstance(data, (dict, list)):
            raise Blocked("invalid_data")
        return data

    def mcp(self, tool: str, payload: dict[str, Any], label: str) -> dict[str, Any]:
        # The advisory phase runs with network and any app/MT5 initialization
        # forbidden. A new journal or the workstation's guardian cannot start.
        from trade_mcp import server

        if tool not in MCP_TOOLS:
            raise RuntimeError("Unsafe advisory tool")
        with offline_advisory():
            wire = asyncio.run(server.mcp.call_tool(tool, payload))
        response = wire[1] if isinstance(wire, tuple) else json.loads(wire[0].text)
        private_json(self.output / f"advisory-{label}.json", {"input": payload, "response": response})
        if (not isinstance(response, dict) or response.get("ok") is not True
                or response.get("execution_authorized") is not False
                or response.get("executable") is not False):
            raise AssertionError("Advisory result failed its safety contract")
        self.cases.append((tool, payload, response, label))
        return response

    def exercise(self, check: str, tool: str, payload: dict[str, Any], label: str, predicate: Any) -> Any:
        try:
            result = self.mcp(tool, payload, label)
            if not predicate(result):
                raise AssertionError("Advisory result did not satisfy the smoke assertion")
            self.state(check, "pass")
            return result
        except Exception:
            self.exception(label)
            self.state(check, "fail", "assertion_failed")
            return None

    def exercise_group(self, check: str, cases: list[tuple[Any, ...]], label: str) -> list[dict[str, Any]] | None:
        try:
            results = []
            for tool, payload, suffix, predicate in cases:
                result = self.mcp(tool, payload, suffix)
                if not predicate(result):
                    raise AssertionError("Advisory result did not satisfy the smoke assertion")
                results.append(result)
            self.state(check, "pass")
            return results
        except Exception:
            self.exception(label)
            self.state(check, "fail", "assertion_failed")
            return None

    def discover_and_fixtures(self) -> None:
        try:
            from trade_mcp import server

            with offline_advisory():
                names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
            private_json(self.output / "advisory-discovery.json", {"expected_present": sorted(names & MCP_TOOLS)})
            if MCP_TOOLS - names:
                raise AssertionError("An expected Day Trader tool is missing")
            self.state("daytrade_mcp_discovery", "pass")
        except Exception:
            self.exception("advisory-discovery")
            self.state("daytrade_mcp_discovery", "fail", "dependency_unavailable")
            self.block((*FIXTURE_CHECKS, *QUOTE_CHECKS, *BAR_CHECKS), "dependency_unavailable")
            return

        news = {"feeds": [{"kind": "rss", "symbol": "XAUUSD_i", "source": "Synthetic",
                           "xml": "<rss><channel><item><title>Fed inflation report</title><guid>x</guid>"
                                  "<link>https://example.test/x</link>"
                                  "<pubDate>Tue, 02 Jan 2024 11:00:00 GMT</pubDate>"
                                  "</item></channel></rss>"}],
                "watch": {"XAUUSD_i": "gold"}, "now": "2024-01-02T12:00:00Z"}
        facts = {"companyfacts": {"cik": 123, "entityName": "Synthetic company", "facts": {"us-gaap": {
            "Revenues": {"units": {"USD": [{"start": "2024-01-01", "end": "2024-03-31",
                "filed": "2024-05-01", "form": "10-Q", "val": 100, "fy": 2024,
                "fp": "Q1", "accn": "x"}]}}}}}, "as_of": "2024-06-01"}
        disclosure = {"kind": "index", "year": 2024, "as_of": "2024-06-01", "members": ["Example"],
                      "text": "<FinancialDisclosure><Member><First>Alex</First><Last>Example</Last>"
                              "<Year>2024</Year><DocID>123</DocID><FilingType>P</FilingType>"
                              "<FilingDate>05/01/2024</FilingDate></Member></FinancialDisclosure>"}
        self.exercise("daytrade_fixture_news", "daytrade_parse_news", news, "fixture-news",
                      lambda r: len(r["items"]) == 1 and not r["automatic_fetch"])
        self.exercise("daytrade_fixture_stock_facts", "daytrade_stock_facts", facts, "fixture-facts",
                      lambda r: r["result"]["rows"][0]["revenue"] == 100)
        self.exercise("daytrade_fixture_stock_disclosures", "daytrade_stock_disclosures", disclosure,
                      "fixture-disclosures", lambda r: len(r["filings"]) == 1)

    def connect(self, cfg: Config, client_factory: Any) -> bool:
        try:
            if cfg.backend != "native" or not local_terminal_url(cfg.mcp_url) or not cfg.mcp_token:
                raise Blocked("client_unavailable")
            self.client = client_factory(cfg.mcp_url, cfg.mcp_token, timeout=min(30.0, max(1.0, cfg.timeout)))
            if isinstance(self.client, McpHttpClient):
                # The starting URL is loopback-only. Do not follow redirects to
                # another origin and never route this connection via a proxy.
                self.client._opener = build_opener(ProxyHandler({}), NoRedirect())
            self.client.connect()
            advertised = self.client.list_tools()
            private_json(self.output / "native-tools.json", advertised)
            for name in RAW_LIMITS:
                matches = [x for x in advertised if x.get("name") == name]
                if len(matches) == 1:
                    self.tools[name] = matches[0]
            info = self.native("terminal_get_info", {}, "terminal")
            connected = terminal_connection(info)
            if connected is not True:
                raise Blocked("terminal_disconnected" if connected is False else "invalid_data")
            self.state("daytrade_mt5_terminal", "pass")
            return True
        except Exception as exc:
            self.exception("native-connect")
            reason = failure_reason(exc)
            self.state("daytrade_mt5_terminal", "blocked", reason)
            self.block(("daytrade_mt5_symbol", "daytrade_mt5_session", "daytrade_mt5_quote",
                        "daytrade_mt5_quote_update", "daytrade_mt5_quote_freshness", "daytrade_mt5_m1",
                        "daytrade_mt5_m1_freshness", *QUOTE_CHECKS, *BAR_CHECKS), reason)
            return False

    def live(self, cfg: Config, client_factory: Any, pause: Any, clock_now: Any) -> None:
        with loopback_native():
            self._live(cfg, client_factory, pause, clock_now)

    def _live(self, cfg: Config, client_factory: Any, pause: Any, clock_now: Any) -> None:
        if not self.connect(cfg, client_factory):
            return
        # Terminal connection and moving prices do not establish the broker's
        # trading schedule. This probe does not read a documented session schedule.
        self.state("daytrade_mt5_session", "blocked", "session_unverified")
        clock_failure = None
        try:
            clock = self.native("get_time_information", {}, "clock")
        except Exception as exc:
            self.exception("native-clock")
            clock, clock_failure = None, failure_reason(exc)

        try:
            args = {"symbol": self.symbol, "limit": 10, "include_hidden": True}
            first = exact_symbol(self.native("get_marketwatch_symbols", args, "symbol-1"), self.symbol)
            self.state("daytrade_mt5_symbol", "pass")
        except Exception as exc:
            self.exception("native-symbol")
            reason = failure_reason(exc)
            self.state("daytrade_mt5_symbol", "blocked", reason)
            self.block(("daytrade_mt5_quote", "daytrade_mt5_quote_update", "daytrade_mt5_quote_freshness",
                        "daytrade_mt5_m1", "daytrade_mt5_m1_freshness", *QUOTE_CHECKS, *BAR_CHECKS), reason)
            return

        snapshots = []
        sample_failure = None
        try:
            snapshots.append(quote(first))
        except Blocked as exc:
            sample_failure = exc.reason
        try:
            pause(5)  # exactly two bounded observations; unchanged is not proof of closure
            second = exact_symbol(self.native("get_marketwatch_symbols", args, "symbol-2"), self.symbol)
            snapshots.append(quote(second))
        except Exception as exc:
            self.exception("native-symbol-2")
            sample_failure = failure_reason(exc)
        self.report["counts"]["daytrade_mt5_quote_samples"] = len(snapshots)
        if snapshots:
            self.state("daytrade_mt5_quote", "pass")
            if len(snapshots) >= 2 and snapshots[0] != snapshots[-1]:
                self.state("daytrade_mt5_quote_update", "pass")
            elif len(snapshots) >= 2:
                self.state("daytrade_mt5_quote_update", "blocked", "unchanged_quotes")
            else:
                self.state("daytrade_mt5_quote_update", "blocked", sample_failure or "invalid_data")
            # The endpoint supplies no independently verified broker tick timestamp.
            self.state("daytrade_mt5_quote_freshness", "blocked", "timestamp_unverified")
            if self.report["checks"].get("daytrade_mcp_discovery") == "pass":
                try:
                    self.exercise_quote(first, snapshots[-1], int(clock_now()))
                except Exception:
                    self.exception("advisory-quote")
                    self.block(QUOTE_CHECKS, "unexpected_error")
        else:
            self.state("daytrade_mt5_quote", "blocked", sample_failure or "invalid_data")
            self.block(("daytrade_mt5_quote_update", "daytrade_mt5_quote_freshness", *QUOTE_CHECKS),
                       sample_failure or "invalid_data")

        try:
            if clock is None:
                raise Blocked(clock_failure or "timestamp_unverified")
            args = {**broker_window(clock), "symbol": self.symbol}
            data = self.native("get_chart_history", args, "m1")
            bars, raw_times = compact_bars(data)
        except Exception as exc:
            self.exception("native-m1")
            reason = failure_reason(exc)
            self.state("daytrade_mt5_m1", "blocked", reason)
            self.block(("daytrade_mt5_m1_freshness", *BAR_CHECKS), reason)
            return
        self.report["counts"]["daytrade_mt5_m1_bars"] = len(bars)
        self.state("daytrade_mt5_m1", "pass")
        try:
            status, reason = freshness(clock, raw_times[-1], clock_now(), len(bars))
            self.state("daytrade_mt5_m1_freshness", status, reason)
        except Exception:
            self.exception("native-m1-freshness")
            self.state("daytrade_mt5_m1_freshness", "blocked", "unexpected_error")
        if self.report["checks"].get("daytrade_mcp_discovery") == "pass":
            try:
                self.exercise_bars(bars)
            except Exception:
                self.exception("advisory-bars")
                self.block(BAR_CHECKS, "unexpected_error")

    def exercise_quote(self, symbol_row: dict[str, Any], sample: dict[str, float], now: int) -> None:
        # Hypothetical plans use the entry-side quote; this is still not a fill.
        buy = {"side": "buy", "entry": sample["ask"], "stop_rule": "percent", "stop_value": 0.5, "target_r": 2.0}
        sell = {**buy, "side": "sell", "entry": sample["bid"]}
        plans = self.exercise_group("daytrade_live_exit_plan", [
            ("daytrade_exit_plan", buy, "live-exit-buy",
             lambda r: r["stop"] < buy["entry"] < r["target"] and r["sizing"] is None),
            ("daytrade_exit_plan", sell, "live-exit-sell",
             lambda r: r["target"] < sell["entry"] < r["stop"] and r["sizing"] is None),
        ], "live-exit-plan")
        if plans is None:
            self.block(("daytrade_live_sizing", "daytrade_live_alert_snapshot", "daytrade_live_alert_rules",
                        "daytrade_live_risk_limits"), "assertion_failed")
            return
        long, short = plans
        buy_status = ("stop_touched" if sample["bid"] <= long["stop"] else
                      "target_touched" if sample["bid"] >= long["target"] else "neither")
        sell_status = ("stop_touched" if sample["ask"] >= short["stop"] else
                       "target_touched" if sample["ask"] <= short["target"] else "neither")
        self.exercise_group("daytrade_live_alert_snapshot", [
            ("daytrade_check_alert", {"side": "buy", "stop": long["stop"], "target": long["target"], **sample},
             "live-alert-buy", lambda r: r["status"] == buy_status and r["observed_exit_side"] == "bid"
             and not r["quote_freshness_verified"]),
            ("daytrade_check_alert", {"side": "sell", "stop": short["stop"], "target": short["target"], **sample},
             "live-alert-sell", lambda r: r["status"] == sell_status and r["observed_exit_side"] == "ask"
             and not r["quote_freshness_verified"]),
        ], "live-alert-snapshot")
        rules = [{"id": "long-stop", "type": "stop", "symbol": self.symbol, "side": "buy",
                  "price": sample["bid"], "rearm": True},
                 {"id": "short-target", "type": "target", "symbol": self.symbol, "side": "sell",
                  "price": sample["ask"], "rearm": True}]
        args = {"rules": rules, "quotes": {self.symbol: sample}, "now": now}
        try:
            first = self.mcp("daytrade_alert_rules", args, "live-alert-rules-1")
            again = self.mcp("daytrade_alert_rules", {**args, "now": now + 1, "state": first["state"]},
                             "live-alert-rules-2")
            if ({e["id"] for e in first["events"]} != {"long-stop", "short-target"}
                    or again["events"] or first["quote_freshness_verified"]):
                raise AssertionError("Alert latch was not exercised")
            self.state("daytrade_live_alert_rules", "pass")
        except Exception:
            self.exception("live-alert-rules")
            self.state("daytrade_live_alert_rules", "fail", "assertion_failed")
        try:
            spec = reported_spec(symbol_row)
        except Blocked as exc:
            self.block(("daytrade_live_sizing", "daytrade_live_risk_limits"), exc.reason)
            return
        sizing = {"spec": spec, "budget": 100.0, "risk_percent": 1.0}
        self.exercise_group("daytrade_live_sizing", [
            ("daytrade_exit_plan", {**params, "sizing": sizing}, f"live-sizing-{params['side']}",
             lambda r: r["sizing"]["status"] == "costs_unknown" and r["sizing"]["volume"] is None)
            for params in (buy, sell)
        ], "live-sizing")
        self.exercise_group("daytrade_live_risk_limits", [
            ("daytrade_review_limits", {"side": params["side"], "entry": params["entry"], "stop": plan["stop"],
                                        "target": plan["target"], "sizing": sizing, "limits": {}},
             f"live-risk-{params['side']}",
             lambda r: r["status"] == "incomplete" and r["plan"]["sizing"]["volume"] is None)
            for params, plan in ((buy, long), (sell, short))
        ], "live-risk-limits")

    def exercise_bars(self, bars: list[list[float]]) -> None:
        tail = contiguous_tail(bars)
        if len(tail) < 7:
            self.block(BAR_CHECKS, "insufficient_history")
            return
        recent = tail[-min(60, len(tail)):]
        for side in ("buy", "sell"):
            self.exercise(f"daytrade_live_entry_{side}", "daytrade_entry_signal",
                          {"ohlcv": recent, "side": side, "start_time": int(recent[0][0]),
                           "opening_range_bars": 5}, f"live-entry-{side}",
                          lambda r: r["last_supplied_bar_excluded"] and
                          r["status"] in {"triggered", "waiting", "range_forming", "expired"})
        markets = [{"symbol": self.symbol, "ohlcv": recent}]
        if len(recent) >= 31:
            self.exercise("daytrade_live_scan_patterns", "daytrade_scan", {"markets": markets}, "live-scan-patterns",
                          lambda r: r["counts"]["unusable"] == 0 and r["results"][0]["status"] == "scanned")
        else:
            self.state("daytrade_live_scan_patterns", "blocked", "insufficient_history")
        self.exercise("daytrade_live_scan_range", "daytrade_scan",
                      {"markets": markets, "mode": "opening_range", "side": "buy",
                       "start_time": int(recent[0][0]), "opening_range_bars": 5}, "live-scan-range",
                      lambda r: r["counts"]["unusable"] == 0 and r["last_supplied_bar_excluded"])
        closed = tail[:-1]  # never use the last, potentially forming candle
        segment_count = min(20, len(closed) // 10)
        if not segment_count:
            self.block(("daytrade_live_historical_replay", "daytrade_live_time_machine"), "insufficient_history")
            return
        usable = closed[-segment_count * 10:]
        segments = [usable[i:i + 10] for i in range(0, len(usable), 10)]
        decision = int(tail[-1][0])
        self.report["counts"]["daytrade_mt5_segments"] = len(segments)
        self.exercise("daytrade_live_historical_replay", "daytrade_historical_replay",
                      {"sessions": segments, "decision_time": decision, "side": "buy",
                       "opening_range_bars": 3, "horizon_bars": 3}, "live-history",
                      lambda r: r["historical_segments"] == len(segments) and r["net_profit"] is None
                      and not r["costs_verified"] and sum(r["counts"].values()) == len(segments))
        if len(segments) < 20:
            self.state("daytrade_live_time_machine", "blocked", "insufficient_history")
            return
        entry = float(closed[-1][4])
        machine = self.exercise("daytrade_live_time_machine", "daytrade_time_machine",
                                {"sessions": segments, "decision_time": decision, "side": "buy",
                                 "entry": entry, "stop": entry * .995, "target": entry * 1.01,
                                 "entry_slot": 0, "horizon_bars": 3, "replays": 20,
                                 "seed": 19, "window": 0}, "live-time-machine",
                                lambda r: r["replays"] == 20 and sum(r["counts"].values()) == 20
                                and r["net_profit"] is None and not r["costs_verified"])
        if machine is not None:
            self.report["counts"]["daytrade_mt5_bootstrap_paths"] = machine["replays"]

    def exercise_http(self) -> None:
        # This is a disposable in-process loopback server with NO broker or
        # journal, not the workstation's existing console. It consumes only the
        # already-captured inputs; its guardian, scheduler and cycle never start.
        if not any(label.startswith("live-") for _, _, _, label in self.cases):
            self.state("daytrade_broker_input_http", "blocked", "not_exercisable")
            return
        if len(self.cases) > 32:
            self.state("daytrade_broker_input_http", "blocked", "unsafe_operation_excluded")
            return
        self.report["counts"]["daytrade_http_cases"] = 0
        server = thread = None
        started = False
        try:
            from trade_mcp.console import ConsoleServer

            app = SimpleNamespace(cfg=Config(backend="mock", allow_trading=False, demo_cash_policy=False),
                                  mt5=NoAppIO(), store=NoAppIO())
            server = ConsoleServer(app, host="127.0.0.1", port=0)
            port = server.httpd.server_port
            thread = threading.Thread(target=server.httpd.serve_forever, daemon=True)
            thread.start()
            started = True
            opener = build_opener(ProxyHandler({}), NoRedirect())
            with only_loopback_port(port):
                for tool, payload, expected, label in self.cases:
                    operation = HTTP_OPERATIONS[tool]
                    request = Request(f"http://127.0.0.1:{port}/api/daytrade/{operation}",
                                      data=json.dumps(payload, allow_nan=False).encode(),
                                      headers={"Content-Type": "application/json"}, method="POST")
                    with opener.open(request, timeout=3) as response:
                        actual = json.load(response)
                    private_json(self.output / f"http-{label}.json", {"input": payload, "response": actual})
                    if (not isinstance(actual, dict) or actual.get("ok") is not True
                            or actual.get("execution_authorized") is not False
                            or actual.get("executable") is not False
                            or ("status" in expected and actual.get("status") != expected["status"])
                            or ("counts" in expected and actual.get("counts") != expected["counts"])):
                        raise AssertionError("Isolated HTTP response disagrees with advisory contract")
                    self.report["counts"]["daytrade_http_cases"] += 1
            self.state("daytrade_broker_input_http", "pass")
        except Exception:
            self.exception("isolated-http")
            self.state("daytrade_broker_input_http", "fail" if started else "blocked",
                       "assertion_failed" if started else "dependency_unavailable")
        finally:
            if server is not None:
                if started:
                    server.httpd.shutdown()
                    assert thread is not None
                    thread.join(timeout=3)
                server.httpd.server_close()

    def finish(self) -> dict[str, Any]:
        # Validate before saving. Unknown fields stay absent; the publisher emits
        # not_run/null defaults and never copies raw evidence.
        sanitize(self.report)
        private_json(self.output / "native-call-counts.json", self.calls)
        private_json(self.output / "report-input.json", self.report)
        return self.report


def git_commit() -> str:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
    commit = git("rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Cannot verify the loaded commit")
    if subprocess.run(["git", "diff", "--quiet"], cwd=ROOT, check=False).returncode != 0 or subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=ROOT, check=False
    ).returncode != 0:
        raise ValueError("Tracked or staged changes prevent a verifiable check")
    return commit


def new_private_output(path: Path) -> Path:
    logs = ROOT / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    requested = path if path.is_absolute() else ROOT / path
    if requested.parent.resolve() != logs.resolve() or requested.name in {"", ".", ".."}:
        raise ValueError("Output must be a new direct child of the ignored logs directory")
    requested.mkdir(mode=0o700, exist_ok=False)
    os.chmod(requested, 0o700)  # only the directory this invocation just created
    return requested


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new unique direct child of ignored logs/")
    parser.add_argument("--symbol", default="XAUUSD_i", help="exact already-available MT5 symbol")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.symbol):
        raise SystemExit("Invalid exact symbol")
    try:
        commit = git_commit()
        output = new_private_output(args.output)
    except Exception:
        raise SystemExit("Preflight failed: check the clean tracked revision and unique ignored output directory") from None
    runner = Runner(output, commit, args.symbol)
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            # Existing config is read in this process only; nothing is persisted or activated.
            load_dotenv()
            cfg = Config.from_env()
            runner.discover_and_fixtures()
            runner.live(cfg, McpHttpClient, time.sleep, time.time)
            runner.exercise_http()
            report = runner.finish()
    except Exception:
        runner.exception("runner")
        raise SystemExit("Probe stopped; diagnostic details remain private") from None
    finally:
        if captured.getvalue():
            private_file(output / "private-process-output.txt", captured.getvalue())
    print(json.dumps({"checks": report["checks"], "counts": report["counts"],
                      "reasons": report["reasons"], "execution_authorized": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
