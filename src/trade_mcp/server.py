"""MCP server: the tools OpenCode (or Claude Desktop, or any MCP client) sees."""

from __future__ import annotations

import functools
import logging
import math
import time
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import analysis, execution
from .app import get_app
from .bridge.base import BridgeError
from .config import VALID_TIMEFRAMES, sizing_balance
from .risk import loss_per_lot, lots_for_risk

LOG = logging.getLogger(__name__)

INSTRUCTIONS = """
You are connected to a real MetaTrader 5 terminal through the "trade" MCP server.

How to work:
1. Start every session with `mt5_status`. If the EA is not connected, stop and tell the
   user how to attach TradeMCPConnector - do not guess prices.
2. Never invent quotes, P&L, account numbers or indicator values. Every number you report
   must come from a tool result. If a call fails, say what failed.
3. Trading is two-step on purpose: `propose_order` / `propose_manage_position` create a
   proposal and change nothing. `confirm_proposal` sends it to the broker. Call it ONLY
   after the human explicitly agreed to that exact proposal. Never confirm on your own
   initiative, never chain propose -> confirm in one step, and never confirm a proposal
   the user has not seen.
4. Respect the guard rails. If a response has "blocked": true, explain the blockers and
   offer a smaller size / wider stop instead of trying to work around them.
5. Use `analyze_market` (deterministic indicators + a priced scenario) as evidence, then
   interpret it: what supports the idea, what contradicts it, what invalidates it.
6. Prefer concrete numbers: entry zone, stop, targets, R multiple, lot size, risk in
   account currency and % of equity.
7. If the user writes in Persian, answer in Persian, but keep symbol names, prices and
   field names as returned by the tools.
8. You are not a financial adviser and you cannot promise outcomes. Say clearly when the
   data is insufficient (closed market, wide spread, conflicting timeframes).
""".strip()

mcp = FastMCP("trade-mcp", instructions=INSTRUCTIONS)


# --------------------------------------------------------------------- helpers
def _safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except BridgeError as exc:
            return exc.to_dict()
        except Exception as exc:  # tools must never crash the session
            LOG.exception("tool %s failed", fn.__name__)
            return {
                "ok": False,
                "error": "internal_error",
                "message": f"{type(exc).__name__}: {exc}",
                "tool": fn.__name__,
            }

    return wrapper


def _iso(ts: float | int | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


# ----------------------------------------------------------------------- tools
@mcp.tool()
@_safe
def mt5_status() -> dict[str, Any]:
    """Health check: is the EA connected, which terminal/account is on the other side,
    and which guard rails are active. Call this first."""
    app = get_app()
    cfg = app.cfg
    out: dict[str, Any] = {
        "ok": True,
        "bridge": app.mt5.status(),
        "config": {
            "backend": cfg.backend,
            "default_symbol": cfg.default_symbol,
            "timeframes": list(cfg.timeframes),
            "allow_trading": cfg.allow_trading,
            "max_lot": cfg.max_lot,
            "max_risk_percent": cfg.max_risk_percent,
            "max_open_positions": cfg.max_open_positions,
            "max_spread_points": cfg.max_spread_points,
            "allowed_symbols": list(cfg.allowed_symbols),
            "proposal_ttl_seconds": cfg.proposal_ttl,
            "journal": cfg.journal_path,
        },
    }
    try:
        out["terminal"] = app.mt5.terminal_info()
        out["ping"] = app.mt5.ping()
    except BridgeError as exc:
        out["ok"] = False
        out["error"] = exc.code
        out["message"] = exc.message
        out["hint"] = (
            "Open MetaTrader 5, open a chart, and drag TradeMCPConnector onto it. "
            "The Experts tab must show 'connected'."
        )
        return out
    try:
        account = app.mt5.account_info(fresh=True)
        out["account"] = {
            k: account.get(k)
            for k in ("login", "server", "currency", "balance", "equity", "margin_free", "leverage")
        }
    except BridgeError:
        pass
    out["ready_for_trading"] = bool(cfg.allow_trading and out.get("terminal", {}).get("trade_allowed"))
    return out


@mcp.tool()
@_safe
def account_info() -> dict[str, Any]:
    """Balance, equity, free margin, margin level and leverage of the connected account."""
    app = get_app()
    account = app.mt5.account_info(fresh=True)
    positions = app.mt5.positions()
    account["open_positions"] = len(positions)
    account["floating_profit"] = round(sum(float(p.get("profit") or 0) for p in positions), 2)
    return {"ok": True, "account": account, "trading_enabled": app.cfg.allow_trading}


@mcp.tool()
@_safe
def find_symbol(query: str = "XAUUSD", limit: int = 10) -> dict[str, Any]:
    """Find the broker's exact symbol name (suffixes like XAUUSD.a / XAUUSDm matter).
    Searches Market Watch and the full broker list."""
    app = get_app()
    hits = app.mt5.find_symbol(query, limit=limit)
    return {
        "ok": True,
        "query": query,
        "count": len(hits),
        "matches": hits,
        "note": "Pass the 'symbol' value from here to the other tools.",
    }


@mcp.tool()
@_safe
def symbol_spec(symbol: str = "") -> dict[str, Any]:
    """Contract specification for a symbol: digits, point, tick value/size, contract size,
    volume min/max/step, spread, stops level, swap. Needed for any sizing maths."""
    app = get_app()
    resolved = app.mt5.resolve_symbol(symbol or app.cfg.default_symbol)
    info = app.mt5.symbol_info(resolved, fresh=True)
    tick = app.mt5.tick(resolved)
    one_lot_per_unit = loss_per_lot(info, 1.0)
    return {
        "ok": True,
        "requested": symbol or app.cfg.default_symbol,
        "symbol": resolved,
        "info": info,
        "quote": tick,
        "derived": {
            "account_currency_loss_per_1_price_unit_per_lot": round(one_lot_per_unit, 4),
            "spread_cost_per_lot": round(
                loss_per_lot(info, float(info.get("spread_points") or 0) * float(info.get("point") or 0)),
                2,
            ),
            "min_stop_distance": round(
                max(float(info.get("trade_stops_level") or 0), float(info.get("spread_points") or 0))
                * float(info.get("point") or 0),
                6,
            ),
        },
        "allowed_by_config": app.cfg.is_symbol_allowed(resolved),
    }


@mcp.tool()
@_safe
def get_candles(
    symbol: str = "", timeframe: str = "M15", count: int = 120
) -> dict[str, Any]:
    """OHLC bars from MT5 as compact column arrays.
    `data` rows are [time_unix, open, high, low, close, tick_volume, spread_points].
    Valid timeframes: M1 M5 M15 M30 H1 H4 D1 W1 MN1 (among others)."""
    app = get_app()
    tf = timeframe.upper()
    if tf not in VALID_TIMEFRAMES:
        return {
            "ok": False,
            "error": "bad_timeframe",
            "message": f"timeframe must be one of {', '.join(VALID_TIMEFRAMES)}",
        }
    count = max(2, min(int(count), 1000))
    resolved = app.mt5.resolve_symbol(symbol or app.cfg.default_symbol)
    bars = app.mt5.candles(resolved, tf, count)
    return {
        "ok": True,
        "symbol": resolved,
        "timeframe": tf,
        "count": len(bars),
        "columns": ["time", "open", "high", "low", "close", "tick_volume", "spread_points"],
        "data": [
            [
                int(b.get("time") or 0),
                b.get("open"),
                b.get("high"),
                b.get("low"),
                b.get("close"),
                b.get("tick_volume"),
                b.get("spread"),
            ]
            for b in bars
        ],
        "last_bar": {**bars[-1], "time_iso": _iso(bars[-1].get("time"))} if bars else None,
    }


@mcp.tool()
@_safe
def market_snapshot(
    symbol: str = "", timeframes: str = "", bars: int = 300
) -> dict[str, Any]:
    """Per-timeframe indicator read-out (EMA stack, RSI, ATR/regime, ADX/DI, Bollinger,
    Donchian, swing structure, trend score) plus the live quote. No interpretation."""
    app = get_app()
    tfs = app.cfg.normalise_timeframes(timeframes or None)
    return analysis.snapshot_symbol(
        mt5=app.mt5, cfg=app.cfg, symbol=symbol or None, timeframes=tfs, bars_count=int(bars)
    )


@mcp.tool()
@_safe
def analyze_market(
    symbol: str = "",
    timeframes: str = "",
    bars: int = 400,
    risk_percent: float | None = None,
) -> dict[str, Any]:
    """Full evidence pack for one instrument: multi-timeframe trend/volatility, bias with
    per-timeframe votes, key levels, and a fully priced scenario (entry zone, stop,
    two targets, R multiples, confidence, suggested lot size for your risk budget).
    This is the tool to use before proposing any trade."""
    app = get_app()
    tfs = app.cfg.normalise_timeframes(timeframes or None)
    return analysis.analyze_symbol(
        mt5=app.mt5,
        cfg=app.cfg,
        symbol=symbol or None,
        timeframes=tfs,
        bars_count=max(120, int(bars)),
        risk_percent=risk_percent,
    )


@mcp.tool()
@_safe
def position_size(
    symbol: str = "",
    risk_percent: float = 0.5,
    entry: float = 0.0,
    stop: float = 0.0,
    balance: float | None = None,
) -> dict[str, Any]:
    """Lot size for a given risk budget and stop distance, with the resulting cash risk
    and margin requirement."""
    app = get_app()
    resolved = app.mt5.resolve_symbol(symbol or app.cfg.default_symbol)
    info = app.mt5.symbol_info(resolved)
    if not entry:
        entry = float(app.mt5.tick(resolved).get("ask") or 0.0)
    if not stop:
        return {"ok": False, "error": "missing_stop", "message": "a stop price is required"}
    account = app.mt5.account_info(fresh=True)
    effective = sizing_balance(account, app.cfg)
    if balance is not None:
        requested_balance = float(balance)
        if not math.isfinite(requested_balance) or requested_balance < 0:
            return {"ok": False, "error": "invalid_balance"}
        effective = min(effective, requested_balance)
    if not math.isfinite(float(risk_percent)) or float(risk_percent) <= 0:
        return {"ok": False, "error": "invalid_risk_percent"}
    pct = min(float(risk_percent), app.cfg.max_risk_percent)
    sizing = lots_for_risk(
        balance=effective, risk_percent=pct, entry=entry, stop=stop, info=info, max_lot=app.cfg.max_lot
    )
    if sizing.get("ok"):
        try:
            margin = app.mt5.calc_order(resolved, "buy", sizing["volume"]).get("margin")
            sizing["margin_required"] = margin
        except BridgeError:
            pass
    return {
        "ok": bool(sizing.get("ok")),
        "symbol": resolved,
        "equity_used": round(effective, 2),
        "effective_sizing_balance": effective,
        "account_equity": account.get("equity"),
        "training_budget": app.cfg.training_budget,
        "requested_balance": balance,
        "requested_risk_percent": risk_percent,
        "applied_risk_percent": pct,
        "sizing": sizing,
        "cap_note": f"risk above {app.cfg.max_risk_percent}% is clamped; sizing balance cannot exceed equity or the training budget",
    }


@mcp.tool()
@_safe
def list_positions(include_pending: bool = True) -> dict[str, Any]:
    """Open positions (ticket, side, volume, entry, SL/TP, floating P&L) and pending orders."""
    app = get_app()
    positions = app.mt5.positions()
    out: dict[str, Any] = {
        "ok": True,
        "count": len(positions),
        "positions": positions,
        "total_profit": round(sum(float(p.get("profit") or 0) for p in positions), 2),
    }
    if include_pending:
        try:
            out["pending_orders"] = app.mt5.orders()
        except BridgeError:
            out["pending_orders"] = []
    return out


@mcp.tool()
@_safe
def trade_history(days: int = 30) -> dict[str, Any]:
    """Closed-deal statistics for the last N days: win rate, profit factor, expectancy,
    per-symbol breakdown, plus the raw deals and the trades this MCP executed."""
    app = get_app()
    days = max(1, min(int(days), 365))
    now = int(time.time())
    deals = app.mt5.deals(now - days * 86400, now + 86400)
    stats = analysis.history_stats(deals, days=days)
    stats["timezone"] = "UTC"
    stats["deals_sample"] = sorted(deals, key=lambda d: d.get("time") or 0, reverse=True)[:40]
    stats["mcp_executions"] = app.store.list_trades(limit=20)
    return {"ok": True, **stats}


@mcp.tool()
@_safe
def risk_report() -> dict[str, Any]:
    """Current exposure: margin usage, worst-case loss if every stop is hit, symbol
    concentration, and any breach of the configured limits."""
    app = get_app()
    cfg = app.cfg
    account = app.mt5.account_info(fresh=True)
    positions = app.mt5.positions()
    equity = float(account.get("equity") or 0.0)
    free = float(account.get("margin_free") or 0.0)

    rows: list[dict[str, Any]] = []
    worst_case = 0.0
    missing_stops = 0
    by_symbol: dict[str, float] = {}
    for pos in positions:
        symbol = pos.get("symbol") or ""
        try:
            info = app.mt5.symbol_info(symbol)
        except BridgeError:
            info = {}
        volume = float(pos.get("volume") or 0)
        sl = float(pos.get("sl") or 0)
        entry = float(pos.get("price_open") or 0)
        side = "buy" if str(pos.get("type", "")).lower() in {"buy", "0"} else "sell"
        if sl:
            risk_cash = loss_per_lot(info, abs(entry - sl)) * volume
            worst_case += risk_cash
        else:
            missing_stops += 1
            risk_cash = None
        by_symbol[symbol] = by_symbol.get(symbol, 0.0) + volume
        rows.append(
            {
                "ticket": pos.get("ticket"),
                "symbol": symbol,
                "side": side,
                "volume": volume,
                "entry": entry,
                "sl": sl or None,
                "tp": float(pos.get("tp") or 0) or None,
                "profit": pos.get("profit"),
                "risk_if_stopped": round(risk_cash, 2) if risk_cash is not None else None,
            }
        )

    warnings: list[str] = []
    margin_level = float(account.get("margin_level") or 0)
    if margin_level and margin_level < 300:
        warnings.append(f"margin level {margin_level:.0f}% is low - positions are fragile")
    if equity and worst_case / equity * 100 > cfg.max_risk_percent:
        warnings.append(
            f"if every stop is hit you lose {worst_case / equity * 100:.2f}% of equity "
            f"(limit {cfg.max_risk_percent:.2f}%)"
        )
    if missing_stops:
        warnings.append(f"{missing_stops} position(s) have no stop loss")
    if len(positions) >= cfg.max_open_positions:
        warnings.append(
            f"{len(positions)} open positions - at the configured limit of {cfg.max_open_positions}"
        )
    if len(by_symbol) == 1 and positions:
        warnings.append("all exposure is concentrated in one symbol")

    return {
        "ok": True,
        "equity": equity,
        "free_margin": free,
        "margin_level": margin_level or None,
        "open_positions": len(positions),
        "worst_case_loss_if_all_stops_hit": round(worst_case, 2),
        "worst_case_percent_of_equity": round(worst_case / equity * 100, 2) if equity else None,
        "volume_by_symbol": by_symbol,
        "positions": rows,
        "limits": {
            "max_risk_percent": cfg.max_risk_percent,
            "max_lot": cfg.max_lot,
            "max_open_positions": cfg.max_open_positions,
            "max_spread_points": cfg.max_spread_points,
            "allow_trading": cfg.allow_trading,
        },
        "warnings": warnings,
    }


@mcp.tool()
@_safe
def propose_order(
    side: str,
    symbol: str = "",
    volume: float | None = None,
    risk_percent: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    comment: str = "trade-mcp",
) -> dict[str, Any]:
    """Step 1 of trading: build a market-order proposal and validate it. NOTHING is sent
    to the broker. Give `stop_loss` so the volume can be derived from `risk_percent`
    (default = the configured cap). Returns a proposal_id to confirm."""
    app = get_app()
    return execution.propose_open(
        mt5=app.mt5,
        cfg=app.cfg,
        store=app.store,
        symbol=symbol or None,
        side=side,
        volume=volume,
        risk_percent=risk_percent,
        stop_loss=stop_loss,
        take_profit=take_profit,
        comment=comment,
    )


@mcp.tool()
@_safe
def propose_manage_position(
    action: str,
    ticket: int,
    sl: float | None = None,
    tp: float | None = None,
    volume: float | None = None,
) -> dict[str, Any]:
    """Step 1 for an existing position: propose closing it (action="close", optional
    partial `volume`) or moving its stop/target (action="modify"). Widening a stop is
    refused; nothing is sent until confirm_proposal."""
    app = get_app()
    return execution.propose_manage(
        mt5=app.mt5,
        cfg=app.cfg,
        store=app.store,
        action=action,
        ticket=int(ticket),
        sl=sl,
        tp=tp,
        volume=volume,
    )


@mcp.tool()
@_safe
def confirm_proposal(proposal_id: str) -> dict[str, Any]:
    """Step 2: send a previously shown proposal to MetaTrader. Only call this after the
    human explicitly agreed to that exact proposal. Refuses when trading is disabled,
    when the proposal expired, or when price has moved more than 1R."""
    app = get_app()
    return execution.confirm(
        mt5=app.mt5, cfg=app.cfg, store=app.store, proposal_id=proposal_id
    )


@mcp.tool()
@_safe
def list_proposals(limit: int = 10, status: str = "") -> dict[str, Any]:
    """Recent proposals and what happened to them (pending / executed / cancelled /
    expired / failed)."""
    app = get_app()
    items = app.store.list_proposals(limit=max(1, min(int(limit), 50)))
    if status:
        items = [i for i in items if i.get("status") == status]
    return {
        "ok": True,
        "count": len(items),
        "proposals": [
            {
                "id": i["id"],
                "status": i["status"],
                "created": _iso(i["created_at"]),
                "expires": _iso(i["expires_at"]),
                "symbol": i.get("symbol"),
                "kind": (i.get("payload") or {}).get("kind"),
                "side": (i.get("payload") or {}).get("side"),
                "volume": (i.get("payload") or {}).get("volume"),
                "sl": (i.get("payload") or {}).get("sl"),
                "tp": (i.get("payload") or {}).get("tp"),
            }
            for i in items
        ],
    }


@mcp.tool()
@_safe
def cancel_proposal(proposal_id: str) -> dict[str, Any]:
    """Cancel a pending proposal. Use when the user declines."""
    app = get_app()
    return execution.cancel(store=app.store, proposal_id=proposal_id)


@mcp.tool()
@_safe
def journal_add(text: str, symbol: str = "", kind: str = "note") -> dict[str, Any]:
    """Save a note, idea or post-trade observation in the local SQLite journal."""
    app = get_app()
    return {
        "ok": True,
        "note": app.store.add_note(text=text, symbol=symbol or None, kind=kind),
        "journal": app.cfg.journal_path,
    }


@mcp.tool()
@_safe
def journal_list(limit: int = 20, symbol: str = "") -> dict[str, Any]:
    """Read back journal notes (most recent first)."""
    app = get_app()
    notes = app.store.list_notes(limit=max(1, min(int(limit), 100)), symbol=symbol or None)
    return {"ok": True, "count": len(notes), "notes": notes}


# Web agents share the journal with this MCP process when journal paths match.
@mcp.tool()
@_safe
def list_web_agents() -> dict[str, Any]:
    """List persisted agents and their last reports, also visible in the web console."""
    return {"ok": True, "agents": get_app().store.list_agents()}


@mcp.tool()
@_safe
def create_web_agent(name: str, symbols: list[str], max_risk: float = 0.5,
                     max_lot: float = 0.01) -> dict[str, Any]:
    """Create a disabled advisory web agent. Never starts a loop or places a trade."""
    app = get_app()
    if not symbols or not all(math.isfinite(v) and v > 0 for v in (max_risk, max_lot)):
        return {"ok": False, "error": "invalid_agent_limits"}
    agent = app.store.create_agent({
        "name": name[:60], "symbols": [s.strip().upper() for s in symbols[:8]],
        "max_risk": min(max_risk, app.cfg.max_risk_percent),
        "max_lot": min(max_lot, app.cfg.max_lot), "mode": "advisory", "enabled": False,
    })
    return {"ok": True, "agent": agent}


@mcp.tool()
@_safe
def run_web_agent(agent_id: str) -> dict[str, Any]:
    """Run one OpenCode-backed analysis, producing proposals only. Never confirms.

    Requires OPENCODE_URL in this MCP process. View the saved report and any pending
    proposals in the web console using the same journal. No scheduler is enabled.
    """
    from .agents import AgentEngine
    app = get_app()
    if not app.cfg.opencode_url:
        return {"ok": False, "error": "opencode_not_configured_in_this_process"}
    agent = app.store.get_agent(agent_id)
    if not agent:
        return {"ok": False, "error": "unknown_agent"}
    report = AgentEngine(cfg=app.cfg, mt5=app.mt5, store=app.store).run({**agent, "mode": "advisory"})
    return {"ok": True, "report": report}


# ---------------------------------------------------------------------- prompts
@mcp.prompt()
def gold_session_briefing(symbol: str = "XAUUSD") -> str:
    """بریفینگ کامل طلا: تحلیل مولتی‌تایم‌فریم + سناریوی معاملاتی + ریسک"""
    return f"""یک بریفینگ معاملاتی برای {symbol} آماده کن. مراحل دقیق:

1. `mt5_status` را صدا بزن. اگر EA وصل نیست، همان‌جا متوقف شو و بگو چه کاری لازم است.
2. `analyze_market` را برای {symbol} با تایم‌فریم‌های M15,H1,H4 صدا بزن (حدود ۴۰۰ کندل).
3. `risk_report` و `account_info` را بگیر تا وضعیت حساب و ریسک فعلی را بدانی.
4. خروجی را در این قالب فارسی بنویس:
   - وضعیت بازار: قیمت، اسپرد، جلسه معاملاتی، نوسان (ATR) و رژیم آن
   - روند هر تایم‌فریم با عدد (ADX، شیب، موقعیت در رنج) و نتیجه‌گیری کلی
   - سناریوی اصلی: ناحیه ورود، حد ضرر، دو هدف، نسبت R، اطمینان و دلیل هر عدد
   - سناریوی جایگزین (اگر روند فعلی شکست)
   - چه چیزی این تحلیل را باطل می‌کند
   - حجم پیشنهادی بر اساس ریسک مجاز و مقدار ریسک به دلار
5. اگر کاربر خواست، با `propose_order` یک پیش‌نویس بساز و پیش‌نمایش را نشان بده.
   تا وقتی کاربر صریحاً تأیید نکند `confirm_proposal` را صدا نزن.
"""


@mcp.prompt()
def trade_review(days: str = "30") -> str:
    """مرور معاملات و انضباط ریسک در N روز گذشته"""
    return f"""یک مرور حرفه‌ای معاملات {days} روز گذشته انجام بده:

1. `trade_history(days={days})` برای آمار بسته‌شده‌ها.
2. `journal_list` برای یادداشت‌های قبلی.
3. `risk_report` برای وضعیت فعلی ریسک.

سپس به فارسی گزارش بده:
- عملکرد: تعداد معامله، وین‌ریت، پروفیت فاکتور، انتظار ریاضی هر معامله
- بهترین و بدترین نماد
- الگوهای تکرارشونده (حد ضررهای خورده، ورودهای زودهنگام، بزرگ بودن حجم)
- سه اقدام مشخص برای بهبود انضباط ریسک در هفته آینده
اگر داده کافی نیست، صریح بگو و حدس نزن.
"""
