#!/usr/bin/env python3
"""Run a bounded live-feed Paper simulation (default 10 minutes) using Day Trader M1.

Strictly read-only against MT5: never sends orders, never creates broker proposals,
never changes settings, never reads account/trade history, and never calls a cloud
model. Live progress is printed to stderr; a sanitized JSON summary is printed to
stdout; full paper event details are saved owner-only (0600) under ignored logs/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from daytrade_mt5_readonly_check import (  # noqa: E402
    Blocked,
    compact_bars,
    exact_symbol,
    local_terminal_url,
    loopback_native,
    reported_spec,
    timestamp,
)
from daytrade_mt5_readonly_check import quote as parse_quote  # noqa: E402

from trade_mcp.bridge.native import find, terminal_connection  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.daytrade_paper import (  # noqa: E402
    HARD_MAX_LOT,
    HARD_MAX_STOP_USD,
    MAX_DURATION_SECONDS,
    MAX_TARGET_USD,
    MIN_DURATION_SECONDS,
    MIN_TARGET_USD,
    DaytradePaperSession,
    small_profit_guard_defaults,
)
from trade_mcp.mcpclient import McpError, McpHttpClient  # noqa: E402

ALLOWED_READ_TOOLS = frozenset({
    "terminal_get_info",
    "get_time_information",
    "get_marketwatch_symbols",
    "get_chart_history",
})


def enrich_spec(base: dict[str, float], row: dict[str, Any]) -> dict[str, Any]:
    """Attach optional read-only symbol economics fields reported by MT5."""
    spec: dict[str, Any] = dict(base)
    optional = {
        "trade_contract_size": ("trade_contract_size", "contract_size"),
        "currency_profit": ("currency_profit", "profit_currency"),
        "trade_calc_mode": ("trade_calc_mode", "calc_mode", "calculation_mode"),
        "digits": ("digits",),
        "swap_long": ("swap_long",),
        "swap_short": ("swap_short",),
    }
    for key, aliases in optional.items():
        val = find(row, *aliases)
        if val is not None:
            spec[key] = val
    return spec


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> Any:
        raise Blocked("unsafe_redirect")


class ReadOnlyNativeFeed:
    """Loopback-only native MT5 reader restricted to 4 read-only tools."""

    def __init__(self, url: str, token: str, symbol: str, timeout: float = 15.0):
        if not local_terminal_url(url) or not token:
            raise Blocked("client_unavailable")
        self.symbol = symbol
        self.client = McpHttpClient(url, token, timeout=timeout)
        self.client._opener = build_opener(ProxyHandler({}), NoRedirect())
        self._clock_cache: tuple[float, Any] | None = None
        self.tool_calls: dict[str, int] = {k: 0 for k in ALLOWED_READ_TOOLS}

    def _call(self, name: str, args: dict[str, Any]) -> Any:
        if name not in ALLOWED_READ_TOOLS:
            raise Blocked("unsafe_operation_excluded")
        self.tool_calls[name] += 1
        res = self.client.call_tool(name, args)
        body = McpHttpClient.structured_content(res)
        if not isinstance(res, dict) or res.get("isError"):
            raise Blocked("tool_error")
        if isinstance(body, dict) and (body.get("ok") is False or bool(body.get("error"))):
            raise Blocked("tool_error")
        if not isinstance(body, (dict, list)):
            raise Blocked("invalid_data")
        return body

    def connect(self) -> tuple[dict[str, Any], dict[str, float]]:
        self.client.connect()
        info = self._call("terminal_get_info", {})
        if terminal_connection(info) is not True:
            raise Blocked("terminal_disconnected")
        row = exact_symbol(
            self._call("get_marketwatch_symbols", {"symbol": self.symbol, "limit": 10, "include_hidden": True}),
            self.symbol,
        )
        return enrich_spec(reported_spec(row), row), parse_quote(row)

    def fetch_quote(self) -> dict[str, float]:
        row = exact_symbol(
            self._call("get_marketwatch_symbols", {"symbol": self.symbol, "limit": 10, "include_hidden": True}),
            self.symbol,
        )
        return parse_quote(row)

    def fetch_m1_bars(self, *, minutes: int = 90, limit: int = 120) -> list[list[float]]:
        now = time.monotonic()
        if self._clock_cache is None or now - self._clock_cache[0] >= 30.0:
            self._clock_cache = (now, self._call("get_time_information", {}))
        clock = self._clock_cache[1]
        value = find(clock, "trade_server_time")
        if value is None:
            value = find(clock, "trade_server_last_known_time")
        dt = timestamp(value)
        if dt is None:
            raise Blocked("timestamp_unverified")
        label = dt.replace(tzinfo=None)
        start, end = label - timedelta(minutes=minutes), label + timedelta(minutes=2)
        data = self._call("get_chart_history", {
            "symbol": self.symbol,
            "period": "M1",
            "datetime_from": start.isoformat(timespec="seconds"),
            "datetime_to": end.isoformat(timespec="seconds"),
            "limit": limit,
        })
        bars, _ = compact_bars(data)
        return bars


class MockFeed:
    """Deterministic synthetic M1 + Bid/Ask feed for offline smoke verification."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.step = 0
        self.base_time = 1700000040
        self.spec: dict[str, Any] = {
            "trade_tick_size": 0.01,
            "trade_tick_value": 1.0,
            "trade_contract_size": 100.0,
            "currency_profit": "USD",
            "trade_calc_mode": "cfd",
            "volume_step": 0.01,
            "volume_min": 0.01,
            "volume_max": 10.0,
            "point": 0.01,
            "trade_stops_level": 5.0,
        }
        # Base 6 bars forming a tight range [2650.00..2651.00]
        self.bars: list[list[float]] = [
            [self.base_time + i * 60, 2650.2, 2650.9, 2650.0, 2650.5, 100.0]
            for i in range(6)
        ]
        self.bid = 2650.50
        self.ask = 2650.70

    def connect(self) -> tuple[dict[str, Any], dict[str, float]]:
        return dict(self.spec), {"bid": self.bid, "ask": self.ask}

    def fetch_quote(self) -> dict[str, float]:
        self.step += 1
        if self.step == 2:
            # Close a bullish breakout M1 bar above 2650.90
            t = int(self.bars[-1][0])
            self.bars[-1] = [t, 2650.6, 2651.4, 2650.5, 2651.3, 150.0]
            self.bars.append([t + 60, 2651.3, 2651.4, 2651.2, 2651.3, 40.0])
            self.bid, self.ask = 2651.30, 2651.50
        elif self.step >= 4:
            # Move Bid to 2651.90 so buy at 2651.50 (+$0.40 at 0.06 lot = +$2.40) hits +$2 target
            self.bid, self.ask = 2651.90, 2652.10
        return {"bid": self.bid, "ask": self.ask}

    def fetch_m1_bars(self, *, minutes: int = 90, limit: int = 120) -> list[list[float]]:
        return [list(r) for r in self.bars]


def _save_private_json(path: Path, payload: dict[str, Any]) -> None:
    logs_dir = (ROOT / "logs").resolve()
    logs_dir.mkdir(mode=0o700, exist_ok=True)
    target = path if path.is_absolute() else ROOT / path
    if target.parent.resolve() != logs_dir or not target.name.endswith(".json"):
        raise ValueError("Output path must be a direct .json file inside ignored logs/")
    if target.is_symlink() or target.exists():
        raise ValueError("Refusing to overwrite existing output file or symlink")
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def _log(msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", file=sys.stderr, flush=True)


def run_window_loop(
    *,
    feed: Any,
    session: DaytradePaperSession,
    poll_seconds: float,
    bars_poll_seconds: float,
    clock: Any = time.monotonic,
    wall: Any = time.time,
    sleep: Any = time.sleep,
    quiet: bool = False,
) -> dict[str, Any]:
    """Run the bounded read-only quote/M1 loop until deadline or interrupt."""
    start = clock()
    deadline = start + session.duration_seconds
    baseline = session.seed_baseline(feed.fetch_m1_bars())
    if not quiet:
        guard_tag = ""
        if session.profit_protect_trigger_usd is not None or session.stale_profit_seconds is not None:
            guard_tag = (
                f" guard[trig={session.profit_protect_trigger_usd} "
                f"floor={session.profit_protect_floor_usd} "
                f"stale={session.stale_profit_seconds}s@>=${session.stale_min_profit_usd:g} "
                f"opp={session.exit_on_opposite_if_profit} end={session.close_profitable_at_end}]"
            )
        _log(
            f"Paper M1 window started: symbol={session.symbol} duration={session.duration_seconds}s "
            f"target=+${session.target_usd:g} stop=-${session.stop_usd:g} "
            f"range={session.opening_range_bars}m max_lot={session.max_lot} "
            f"basis={session.pricing_basis} baseline_bar={baseline}{guard_tag} (NO broker orders)"
        )
        if session.spec_audit.get("warning"):
            _log(f"SPEC WARNING: {session.spec_audit['warning']}")

    last_bars_at = start - bars_poll_seconds
    last_quote: dict[str, float] | None = None
    consecutive_errors = 0
    interrupted = False
    stop_reason = "completed"

    try:
        while clock() < deadline:
            loop_started = clock()
            now_wall = wall()
            try:
                q = feed.fetch_quote()
                last_quote = q
                mark = session.mark_quote(bid=q["bid"], ask=q["ask"], now=now_wall)
                if mark.get("closed") and not quiet:
                    tr = mark["trade"]
                    _log(
                        f"PAPER CLOSE #{tr['trade_id']} {tr['side'].upper()} {tr['volume']} lot "
                        f"reason={tr['exit_reason']} net=${tr['net_pnl_usd']:+.2f} "
                        f"hold={tr['hold_seconds']}s balance=${session.balance:.2f}"
                        + (" [3-LOSS LATCH]" if mark.get("latched") else "")
                    )

                if loop_started - last_bars_at >= bars_poll_seconds:
                    last_bars_at = loop_started
                    bars = feed.fetch_m1_bars()
                    ev = session.on_bars(bars=bars, bid=q["bid"], ask=q["ask"], now=now_wall)
                    if ev.get("opened") and not quiet:
                        tr = ev["trade"]
                        _log(
                            f"PAPER OPEN  #{tr['trade_id']} {tr['side'].upper()} {tr['volume']} lot "
                            f"entry={tr['entry']} SL={tr['stop']} TP={tr['target']} "
                            f"spread=${tr['entry_spread_usd']:.2f} ({tr['signal_reason']})"
                        )
                    elif ev.get("closed_by_opposite") and not quiet:
                        tr = ev["trade"]
                        _log(
                            f"PAPER CLOSE #{tr['trade_id']} {tr['side'].upper()} {tr['volume']} lot "
                            f"reason={tr['exit_reason']} net=${tr['net_pnl_usd']:+.2f} "
                            f"hold={tr['hold_seconds']}s balance=${session.balance:.2f}"
                        )
                    elif ev.get("action") in ("buy", "sell") and not ev.get("opened") and not quiet:
                        _log(f"SIGNAL SKIPPED {ev['action'].upper()}: {ev.get('reason')}")
                    elif ev.get("action") == "wait" and ev.get("closed_bar_time") and not quiet:
                        _log(f"M1 bar {ev['closed_bar_time']} closed -> WAIT ({ev.get('reason')})")

                consecutive_errors = 0
            except (Blocked, McpError, ValueError, OSError) as exc:
                consecutive_errors += 1
                if not quiet:
                    _log(f"Read warning ({consecutive_errors}/5): {type(exc).__name__}")
                if consecutive_errors >= 5:
                    stop_reason = "consecutive_read_errors"
                    break

            remaining = deadline - clock()
            if remaining <= 0:
                break
            sleep(min(poll_seconds, remaining))
    except KeyboardInterrupt:
        interrupted = True
        stop_reason = "interrupted_by_user"
        if not quiet:
            _log("Stopped early by Ctrl-C; writing paper summary.")

    ended_wall = wall()
    if session.position is not None and session.close_profitable_at_end and last_quote is not None:
        fin = session.finalize_window(bid=last_quote["bid"], ask=last_quote["ask"], now=ended_wall)
        if fin.get("closed") and not quiet:
            tr = fin["trade"]
            _log(
                f"PAPER CLOSE #{tr['trade_id']} {tr['side'].upper()} {tr['volume']} lot "
                f"reason={tr['exit_reason']} net=${tr['net_pnl_usd']:+.2f} "
                f"hold={tr['hold_seconds']}s balance=${session.balance:.2f}"
            )

    summary = session.summary(ended_at=ended_wall)
    summary["stop_reason"] = stop_reason
    summary["interrupted"] = interrupted
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="XAUUSD_i", help="exact broker symbol (default: XAUUSD_i)")
    p.add_argument("--duration", type=int, default=600,
                   help=f"window duration in seconds ({MIN_DURATION_SECONDS}..{MAX_DURATION_SECONDS}, default 600 = 10 min)")
    p.add_argument("--target-usd", type=float, default=2.0,
                   help=f"profit target in USD ({MIN_TARGET_USD}..{MAX_TARGET_USD}, default 2.0)")
    p.add_argument("--stop-usd", type=float, default=10.0,
                   help=f"max planned stop loss in USD (1..{HARD_MAX_STOP_USD}, default 10.0)")
    p.add_argument("--opening-range-bars", type=int, default=3,
                   help="closed M1 bars forming the breakout range (2..15, default 3)")
    p.add_argument("--max-lot", type=float, default=HARD_MAX_LOT,
                   help=f"hard paper lot cap (<= {HARD_MAX_LOT}, default {HARD_MAX_LOT})")
    p.add_argument("--commission-per-lot", type=float, default=0.0,
                   help="optional round-trip commission USD/lot assumption (default 0.0)")
    p.add_argument("--pricing-basis", choices=("auto", "tick_value", "contract_size"), default="auto",
                   help="lot PnL multiplier basis (default: auto = conservative max of contract_size vs tick_value)")
    p.add_argument("--small-profit-guard", action="store_true",
                   help="enable default floating small-profit guards (profit floor, 180s stale exit, opposite exit, window-end profit close)")
    p.add_argument("--protect-trigger-usd", type=float, default=None,
                   help="arm profit floor once floating net P&L reaches this USD amount")
    p.add_argument("--protect-floor-usd", type=float, default=None,
                   help="close if armed profit pulls back to or below this USD floor")
    p.add_argument("--stale-seconds", type=float, default=None,
                   help="after this many seconds open, close if floating net P&L >= --stale-min-profit-usd")
    p.add_argument("--stale-min-profit-usd", type=float, default=None,
                   help="minimum small net profit USD for stale/opposite/window-end exits (default 0.50)")
    p.add_argument("--exit-on-opposite-if-profit", action="store_true",
                   help="close profitable floating trade when an opposite M1 breakout fires")
    p.add_argument("--close-profitable-at-end", action="store_true",
                   help="close profitable floating trade at window end (losing trades remain honestly open)")
    p.add_argument("--poll", type=float, default=2.0, help="quote poll interval seconds (1..10, default 2)")
    p.add_argument("--bars-poll", type=float, default=5.0, help="M1 bar poll interval seconds (2..30, default 5)")
    p.add_argument("--out", default="", help="optional .json filename inside ignored logs/")
    p.add_argument("--mock", action="store_true", help="run offline smoke test with synthetic M1 feed")
    return p


def _guard_kwargs_from_args(args: argparse.Namespace) -> dict[str, Any]:
    cfg: dict[str, Any] = (
        small_profit_guard_defaults(args.target_usd)
        if args.small_profit_guard
        else {
            "profit_protect_trigger_usd": None,
            "profit_protect_floor_usd": None,
            "stale_profit_seconds": None,
            "stale_min_profit_usd": 0.5,
            "exit_on_opposite_if_profit": False,
            "close_profitable_at_end": False,
        }
    )
    if args.protect_trigger_usd is not None:
        cfg["profit_protect_trigger_usd"] = args.protect_trigger_usd
    if args.protect_floor_usd is not None:
        cfg["profit_protect_floor_usd"] = args.protect_floor_usd
    if args.stale_seconds is not None:
        cfg["stale_profit_seconds"] = args.stale_seconds
    if args.stale_min_profit_usd is not None:
        cfg["stale_min_profit_usd"] = args.stale_min_profit_usd
    if args.exit_on_opposite_if_profit:
        cfg["exit_on_opposite_if_profit"] = True
    if args.close_profitable_at_end:
        cfg["close_profitable_at_end"] = True
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.symbol):
        raise SystemExit("Invalid symbol format")
    if not 1.0 <= args.poll <= 10.0 or not 2.0 <= args.bars_poll <= 30.0:
        raise SystemExit("Poll intervals out of allowed bounds")

    # Hard in-process safety override: never enable broker trading.
    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    out_path = Path(args.out) if args.out else Path("logs") / f"daytrade-paper-{uuid.uuid4().hex[:12]}.json"
    guard_kwargs = _guard_kwargs_from_args(args)

    try:
        if args.mock:
            feed: Any = MockFeed(args.symbol)
            spec, _ = feed.connect()
            session = DaytradePaperSession(
                symbol=args.symbol,
                spec=spec,
                duration_seconds=args.duration,
                target_usd=args.target_usd,
                stop_usd=args.stop_usd,
                opening_range_bars=args.opening_range_bars,
                max_lot=args.max_lot,
                commission_per_lot=args.commission_per_lot,
                pricing_basis=args.pricing_basis,
                started_at=time.time(),
                **guard_kwargs,
            )
            summary = run_window_loop(
                feed=feed, session=session,
                poll_seconds=args.poll, bars_poll_seconds=args.bars_poll,
            )
        else:
            load_dotenv()
            cfg = Config.from_env()
            if cfg.backend != "native":
                raise Blocked("native_backend_required")
            with loopback_native():
                feed = ReadOnlyNativeFeed(cfg.mcp_url, cfg.mcp_token, args.symbol)
                spec, _ = feed.connect()
                session = DaytradePaperSession(
                    symbol=args.symbol,
                    spec=spec,
                    duration_seconds=args.duration,
                    target_usd=args.target_usd,
                    stop_usd=args.stop_usd,
                    opening_range_bars=args.opening_range_bars,
                    max_lot=args.max_lot,
                    commission_per_lot=args.commission_per_lot,
                    pricing_basis=args.pricing_basis,
                    started_at=time.time(),
                    **guard_kwargs,
                )
                summary = run_window_loop(
                    feed=feed, session=session,
                    poll_seconds=args.poll, bars_poll_seconds=args.bars_poll,
                )
        summary["private_log_file"] = out_path.name
        _save_private_json(out_path, {
            "summary": summary,
            "events": session.events,
            "tool_calls": getattr(feed, "tool_calls", None),
        })
    except (Blocked, McpError, ValueError, OSError) as exc:
        reason = exc.reason if isinstance(exc, Blocked) else type(exc).__name__
        print(json.dumps({
            "ok": False,
            "paper": True,
            "executable": False,
            "execution_authorized": False,
            "orders_submitted": 0,
            "error": reason,
        }, indent=2))
        return 1

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
