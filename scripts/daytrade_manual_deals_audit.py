#!/usr/bin/env python3
"""Strictly read-only audit of manual closed deals on MT5 (e.g., 1-second Demo scalps).

Reads closed MT5 deals via `get_trading_history_orders` (include_deals=True) and
pairs `in`/`out` deals by `position_id` to measure:
  - exact hold duration in seconds (min / median / mean / max),
  - real broker profit, commission, fee, swap, and net USD,
  - empirical $/price-unit/lot multiplier (verifying contract_size=100 vs tick_value=10),
  - win rate, payoff ratio, profit factor, max drawdown, and surrounding M1 bar context.

Strictly read-only: NEVER places, modifies, closes, or proposes any orders.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from daytrade_m1_paper_window import _save_private_json  # noqa: E402
from daytrade_mt5_readonly_check import Blocked, loopback_native  # noqa: E402

from trade_mcp.bridge.native import (  # noqa: E402
    as_float,
    as_int,
    as_text,
    deal_entry,
    deal_side,
    deal_ticket,
    financial,
    find,
    iso,
    records,
    to_epoch,
)
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.mcpclient import McpError, McpHttpClient  # noqa: E402

READ_ONLY_TOOLS = frozenset({
    "get_time_information",
    "get_symbol_information",
    "get_trading_history_orders",
    "get_chart_history",
})


def _iso_utc(ep: float) -> str:
    return datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_deal_time(row: dict[str, Any]) -> int:
    for key in ("time", "datetime", "deal_time", "execution_time", "timestamp", "date", "time_utc", "time_msc"):
        val = find(row, key)
        if val is None or val == "":
            continue
        if isinstance(val, (int, float)) and float(val) > 1e12:
            return int(float(val) // 1000)
        ep = to_epoch(val)
        if ep > 0:
            return ep
    return 0


def _parse_deals(raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    sample_keys: list[str] = []
    for row in records(raw, "deals"):
        if not sample_keys and isinstance(row, dict):
            sample_keys = sorted(str(k) for k in row)
        ticket = deal_ticket(row)
        if ticket in seen:
            continue
        seen.add(ticket)
        px = as_float(find(row, "price", "deal_price", "execution_price", "price_open", "open_price", "fill_price"))
        out.append({
            "ticket": ticket,
            "position_id": as_int(find(row, "position_id", "position")),
            "symbol": as_text(find(row, "symbol", "name")),
            "type": deal_side(row),
            "entry": deal_entry(row),
            "volume": as_float(find(row, "volume", "lots")),
            "price": px,
            "profit": float(financial(find(row, "profit")) or 0.0),
            "swap": float(financial(find(row, "swap", "swaps")) or 0.0),
            "commission": float(financial(find(row, "commission")) or 0.0),
            "fee": float(financial(find(row, "fee")) or 0.0),
            "magic": as_int(find(row, "magic", "magic_number")),
            "time": _parse_deal_time(row),
        })
    out.sort(key=lambda d: (d["time"], d["ticket"]))
    return out, sample_keys


def analyze_manual_deals(
    deals: list[dict[str, Any]],
    *,
    symbol: str = "XAUUSD_i",
    m1_bars: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pair in/out MT5 deals by position_id and compute objective scalping analytics."""
    sym_deals = [d for d in deals if not symbol or d.get("symbol") == symbol]
    by_pos: dict[int, list[dict[str, Any]]] = {}
    for d in sym_deals:
        pid = int(d.get("position_id") or 0)
        if pid <= 0:
            continue
        by_pos.setdefault(pid, []).append(d)

    m1_by_min: dict[int, dict[str, Any]] = {}
    for b in m1_bars or []:
        t = int(b.get("time") or 0)
        if t > 0:
            m1_by_min[t - (t % 60)] = b

    closed_trades: list[dict[str, Any]] = []
    for pid, rows in sorted(by_pos.items(), key=lambda kv: min(r["time"] for r in kv[1])):
        ins = [r for r in rows if r.get("entry") == "in"]
        outs = [r for r in rows if r.get("entry") in ("out", "out_by", "inout")]
        if not ins or not outs:
            continue
        in_vol = sum(float(r["volume"]) for r in ins)
        out_vol = sum(float(r["volume"]) for r in outs)
        if in_vol <= 0 or out_vol <= 0:
            continue
        entry_px = sum(float(r["price"]) * float(r["volume"]) for r in ins) / in_vol
        exit_px = sum(float(r["price"]) * float(r["volume"]) for r in outs) / out_vol
        side = str(ins[0]["type"]).lower()
        opened_at = min(int(r["time"]) for r in ins)
        closed_at = max(int(r["time"]) for r in outs)
        hold_sec = max(0, closed_at - opened_at)

        signed_move = (exit_px - entry_px) if side == "buy" else (entry_px - exit_px)
        gross_profit = sum(float(r["profit"]) for r in rows)
        commission = sum(float(r["commission"]) for r in rows)
        fee = sum(float(r["fee"]) for r in rows)
        swap = sum(float(r["swap"]) for r in rows)
        net_usd = gross_profit + commission + fee + swap

        implied_mult: float | None = None
        if abs(signed_move) >= 0.05 and out_vol > 0 and abs(gross_profit) > 0:
            implied_mult = round(abs(gross_profit) / (abs(signed_move) * out_vol), 2)

        comm_per_lot = round(abs(commission + fee) / out_vol, 2) if out_vol > 0 else 0.0

        m1_ctx: dict[str, Any] | None = None
        bar_min = opened_at - (opened_at % 60)
        bar = m1_by_min.get(bar_min)
        if bar is not None:
            o, h, low, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
            m1_dir = "buy" if c > o else ("sell" if c < o else "flat")
            b_prev1 = m1_by_min.get(bar_min - 60)
            b_prev5 = m1_by_min.get(bar_min - 300)
            b_prev15 = m1_by_min.get(bar_min - 900)
            ref_c = float(b_prev1["close"]) if b_prev1 is not None else o
            pre5 = round(ref_c - float(b_prev5["open"]), 2) if b_prev5 is not None else None
            pre15 = round(ref_c - float(b_prev15["open"]), 2) if b_prev15 is not None else None
            m1_ctx = {
                "m1_range_price": round(h - low, 4),
                "m1_body_price": round(abs(c - o), 4),
                "m1_dir": m1_dir,
                "aligned_with_m1_bar": m1_dir == side,
                "m1_tick_volume": float(bar.get("tick_volume") or 0.0),
                "pre5m_move": pre5,
                "pre15m_move": pre15,
            }

        closed_trades.append({
            "position_id": pid,
            "side": side,
            "volume": round(out_vol, 4),
            "entry_price": round(entry_px, 4),
            "exit_price": round(exit_px, 4),
            "signed_price_move": round(signed_move, 4),
            "opened_utc": _iso_utc(opened_at),
            "closed_utc": _iso_utc(closed_at),
            "hold_seconds": hold_sec,
            "gross_profit_usd": round(gross_profit, 2),
            "commission_usd": round(commission, 2),
            "fee_usd": round(fee, 2),
            "swap_usd": round(swap, 2),
            "net_usd": round(net_usd, 2),
            "implied_multiplier": implied_mult,
            "commission_per_lot_usd": comm_per_lot,
            "m1_context": m1_ctx,
        })

    n = len(closed_trades)
    wins = [t for t in closed_trades if t["net_usd"] > 0]
    losses = [t for t in closed_trades if t["net_usd"] <= 0]
    gp = sum(t["net_usd"] for t in wins)
    gl = abs(sum(t["net_usd"] for t in losses))
    net_total = gp - gl
    pf = round(gp / gl, 3) if gl > 0 else (99.0 if gp > 0 else None)
    avg_w = round(gp / len(wins), 2) if wins else None
    avg_l = round(gl / len(losses), 2) if losses else None
    payoff = round(avg_w / avg_l, 3) if (avg_w is not None and avg_l and avg_l > 0) else None

    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in closed_trades:
        eq += t["net_usd"]
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

    holds = [t["hold_seconds"] for t in closed_trades]
    mults = [t["implied_multiplier"] for t in closed_trades if t["implied_multiplier"] is not None]
    comms = [t["commission_per_lot_usd"] for t in closed_trades]
    m1_aligned = [t for t in closed_trades if t["m1_context"] and t["m1_context"]["aligned_with_m1_bar"]]
    m1_known = [t for t in closed_trades if t["m1_context"]]

    hold_stats = {
        "min_sec": min(holds) if holds else None,
        "median_sec": round(float(statistics.median(holds)), 1) if holds else None,
        "mean_sec": round(sum(holds) / len(holds), 1) if holds else None,
        "max_sec": max(holds) if holds else None,
        "pct_le_5s": round(100.0 * sum(1 for h in holds if h <= 5) / n, 1) if n else 0.0,
        "pct_le_15s": round(100.0 * sum(1 for h in holds if h <= 15) / n, 1) if n else 0.0,
        "pct_le_60s": round(100.0 * sum(1 for h in holds if h <= 60) / n, 1) if n else 0.0,
    }

    notes: list[str] = []
    if n == 0:
        notes.append("No closed deals found in the requested window for this symbol.")
    elif n < 20:
        notes.append(f"Small sample ({n} trades): useful to verify broker math & style, not yet statistically conclusive (need 30+).")
    if hold_stats["median_sec"] is not None and hold_stats["median_sec"] <= 10.0:
        notes.append(
            "Sub-10-second ultra-fast scalping detected: M1/M5 bar-close scripts cannot see intra-second bursts; "
            "automation would require tick-level streaming inside MQL5 (not HTTP MCP bar polling)."
        )
    if mults:
        med_m = round(float(statistics.median(mults)), 2)
        notes.append(
            f"Empirical broker P&L multiplier from real closed deals = {med_m} USD per $1 move per 1.00 lot "
            f"(confirms contract_size=100 if ~100, or tick_value=10 if ~10)."
        )

    return {
        "symbol": symbol,
        "deals_scanned": len(sym_deals),
        "trades_closed": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / n, 1) if n else 0.0,
        "gross_won_usd": round(gp, 2),
        "gross_lost_usd": round(gl, 2),
        "total_commission_usd": round(sum(t["commission_usd"] for t in closed_trades), 2),
        "total_fee_usd": round(sum(t["fee_usd"] for t in closed_trades), 2),
        "total_swap_usd": round(sum(t["swap_usd"] for t in closed_trades), 2),
        "net_pnl_usd": round(net_total, 2),
        "profit_factor": pf,
        "avg_win_usd": avg_w,
        "avg_loss_usd": avg_l,
        "payoff_ratio": payoff,
        "max_drawdown_usd": round(max_dd, 2),
        "hold_seconds_stats": hold_stats,
        "empirical_contract_multiplier_median": round(float(statistics.median(mults)), 2) if mults else None,
        "empirical_commission_per_lot_median": round(float(statistics.median(comms)), 2) if comms else None,
        "m1_bar_alignment_pct": round(100.0 * len(m1_aligned) / len(m1_known), 1) if m1_known else None,
        "feasibility_notes": notes,
        "trades": closed_trades,
    }


def _format_summary(report: dict[str, Any]) -> str:
    a = report["analysis"]
    h = a["hold_seconds_stats"]
    lines = [
        f"OK={report['ok']} ORDERS={report['orders_submitted']} SYMBOL={a['symbol']} HOURS={report['hours_requested']}",
        f"TRADES: {a['trades_closed']} ({a['wins']}W/{a['losses']}L, WR={a['win_rate_pct']}%) "
        f"NET=${a['net_pnl_usd']:+.2f} PF={a['profit_factor']} DD=${a['max_drawdown_usd']}",
        f"AVG_WIN=${a['avg_win_usd']} AVG_LOSS=${a['avg_loss_usd']} PAYOFF={a['payoff_ratio']} "
        f"COMM=${a['total_commission_usd']} FEE=${a['total_fee_usd']} SWAP=${a['total_swap_usd']}",
        f"HOLD_SEC: min={h['min_sec']}s med={h['median_sec']}s mean={h['mean_sec']}s max={h['max_sec']}s "
        f"(<=5s: {h['pct_le_5s']}%, <=15s: {h['pct_le_15s']}%, <=60s: {h['pct_le_60s']}%)",
        f"EMPIRICAL_BROKER: multiplier={a['empirical_contract_multiplier_median']} "
        f"comm_per_lot=${a['empirical_commission_per_lot_median']} "
        f"m1_aligned={a['m1_bar_alignment_pct']}%",
    ]
    for note in a["feasibility_notes"]:
        lines.append(f"NOTE: {note}")
    if a["trades"]:
        lines.append("\n--- LAST TRADES (up to 30) ---")
        for t in a["trades"][-30:]:
            m1 = t["m1_context"]
            if m1:
                pre_parts = []
                if m1.get("pre5m_move") is not None:
                    pre_parts.append(f"pre5m={m1['pre5m_move']:+.2f}")
                if m1.get("pre15m_move") is not None:
                    pre_parts.append(f"pre15m={m1['pre15m_move']:+.2f}")
                pre_s = (" " + " ".join(pre_parts)) if pre_parts else ""
                m1_s = f"m1_rng=${m1['m1_range_price']} aligned={m1['aligned_with_m1_bar']}{pre_s}"
            else:
                m1_s = "m1=n/a"
            lines.append(
                f"  {t['opened_utc']} #{t['position_id']} {t['side'].upper():4s} {t['volume']:.2f}lot "
                f"{t['entry_price']:.2f}->{t['exit_price']:.2f} ({t['signed_price_move']:+.2f}) "
                f"hold={t['hold_seconds']:4d}s net=${t['net_usd']:+7.2f} "
                f"(gross=${t['gross_profit_usd']:+7.2f} comm=${t['commission_usd']:.2f}) "
                f"mult={t['implied_multiplier']} {m1_s}"
            )
    lines.append(
        f"\nTOTAL ({report['hours_requested']}h): {a['trades_closed']}t ({a['wins']}W/{a['losses']}L, WR={a['win_rate_pct']}%) | "
        f"NET=${a['net_pnl_usd']:+.2f} | PF={a['profit_factor']} | DD=${a['max_drawdown_usd']} | "
        f"AVG_W=${a['avg_win_usd']} AVG_L=${a['avg_loss_usd']} PAYOFF={a['payoff_ratio']}"
    )
    if report.get("sample_deal_keys"):
        lines.append(f"RAW_DEAL_KEYS: {report['sample_deal_keys']}")
    if report.get("log_path"):
        lines.append(f"SAVED: {report['log_path']}")
    return "\n".join(lines)


def _mock_deals_and_bars() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base = 1795400000
    deals = [
        {"ticket": 1, "position_id": 101, "symbol": "XAUUSD_i", "type": "buy", "entry": "in",
         "volume": 0.01, "price": 3620.00, "profit": 0.0, "commission": 0.0, "fee": 0.0, "swap": 0.0, "time": base},
        {"ticket": 2, "position_id": 101, "symbol": "XAUUSD_i", "type": "sell", "entry": "out",
         "volume": 0.01, "price": 3621.20, "profit": 1.20, "commission": 0.0, "fee": 0.0, "swap": 0.0, "time": base + 2},
        {"ticket": 3, "position_id": 102, "symbol": "XAUUSD_i", "type": "sell", "entry": "in",
         "volume": 0.01, "price": 3621.00, "profit": 0.0, "commission": 0.0, "fee": 0.0, "swap": 0.0, "time": base + 70},
        {"ticket": 4, "position_id": 102, "symbol": "XAUUSD_i", "type": "buy", "entry": "out",
         "volume": 0.01, "price": 3621.60, "profit": -0.60, "commission": 0.0, "fee": 0.0, "swap": 0.0, "time": base + 74},
    ]
    bars = [
        {"time": base - (base % 60), "open": 3619.5, "high": 3622.0, "low": 3619.2, "close": 3621.8, "tick_volume": 180.0},
        {"time": (base + 70) - ((base + 70) % 60), "open": 3621.5, "high": 3622.1, "low": 3620.5, "close": 3621.9, "tick_volume": 140.0},
    ]
    return deals, bars


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="XAUUSD_i", help="exact broker symbol (default: XAUUSD_i)")
    p.add_argument("--hours", type=int, default=48, help="lookback window in hours (1..720, default 48)")
    p.add_argument("--out", default="", help="optional .json path inside ignored logs/")
    p.add_argument("--summary", action="store_true", help="print compact human-readable report to stdout")
    p.add_argument("--mock", action="store_true", help="run offline smoke test with synthetic deals")
    return p


def main(argv: list[str] | None = None) -> int:
    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"
    args = build_parser().parse_args(argv)

    if not (1 <= args.hours <= 720):
        raise SystemExit("--hours must be between 1 and 720")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = Path(args.out) if args.out else Path("logs") / f"{stamp}-manual-deals-audit.json"
    if not args.out and out_path.exists():
        out_path = Path("logs") / f"{stamp}-{int(time.time() * 1000) % 10000}-manual-deals-audit.json"

    try:
        tool_calls: dict[str, int] = {}
        chart_warnings: list[str] = []
        sample_keys: list[str] = []
        if args.mock:
            deals, m1_bars = _mock_deals_and_bars()
        else:
            load_dotenv()
            cfg = Config.from_env()
            if cfg.backend != "native":
                raise Blocked("native_backend_required")
            with loopback_native():
                client = McpHttpClient(cfg.mcp_url, cfg.mcp_token, timeout=30.0,
                                       client_name="trade-mcp-manual-audit", client_version="0.1.0")
                client.connect()

                def _call(tool: str, arguments: dict[str, Any] | None = None) -> Any:
                    if tool not in READ_ONLY_TOOLS:
                        raise Blocked(f"readonly_violation:{tool}")
                    res = client.call_tool(tool, arguments or {})
                    if res.get("isError"):
                        raise McpError(McpHttpClient.text_content(res) or f"{tool} failed")
                    tool_calls[tool] = tool_calls.get(tool, 0) + 1
                    return McpHttpClient.structured_content(res) or {}

                # Use broker clock offset so deals and M1 bars align cleanly even when broker != UTC.
                tinfo = _call("get_time_information")
                utc_now = to_epoch(find(tinfo, "utc_time")) or int(time.time())
                srv_now = to_epoch(find(tinfo, "trade_server_time")) or to_epoch(
                    find(tinfo, "trade_server_last_known_time")
                ) or utc_now
                end_dt = datetime.fromtimestamp(max(utc_now, srv_now) + 3600, tz=timezone.utc)
                start_dt = end_dt - timedelta(hours=args.hours + 4)

                raw_hist = _call(
                    "get_trading_history_orders",
                    {
                        "datetime_from": iso(start_dt),
                        "datetime_to": iso(end_dt),
                        "include_orders": False,
                        "include_deals": True,
                        "limit": 10000,
                    },
                )
                deals, sample_keys = _parse_deals(raw_hist)

                m1_bars = []
                valid_times = [
                    int(d["time"]) for d in deals
                    if int(d.get("time") or 0) > 0 and (not args.symbol or d.get("symbol") == args.symbol)
                ]
                if not valid_times:
                    valid_times = [int(d["time"]) for d in deals if int(d.get("time") or 0) > 0]
                if valid_times:
                    now_cap = int(max(utc_now, srv_now))
                    d_max = min(max(valid_times) + 300, now_cap)
                    d_min = min(valid_times) - 1800
                    d_min = max(d_min, d_max - 48 * 3600)
                    if d_min >= d_max:
                        d_min = d_max - 3600
                    # Fetch M1 context in <=23h chunks so broker limit/range rules never reject the request.
                    seen_m1: set[int] = set()
                    cur_end = d_max
                    while cur_end > d_min:
                        cur_start = max(d_min, cur_end - 23 * 3600)
                        try:
                            raw_m1 = _call(
                                "get_chart_history",
                                {
                                    "datetime_from": datetime.fromtimestamp(cur_start, tz=timezone.utc).isoformat(timespec="seconds"),
                                    "datetime_to": datetime.fromtimestamp(cur_end, tz=timezone.utc).isoformat(timespec="seconds"),
                                    "symbol": args.symbol,
                                    "period": "M1",
                                    "limit": 1440,
                                },
                            )
                        except McpError as exc:
                            chart_warnings.append(f"M1 context chunk skipped ({cur_start}..{cur_end}): {exc}")
                            break
                        for row in records(raw_m1, "candles", "rates", "bars", "history", "data"):
                            t_ep = to_epoch(find(row, "time", "datetime", "date", "timestamp"))
                            if t_ep > 0 and t_ep not in seen_m1:
                                seen_m1.add(t_ep)
                                m1_bars.append({
                                    "time": t_ep,
                                    "open": as_float(find(row, "open")),
                                    "high": as_float(find(row, "high")),
                                    "low": as_float(find(row, "low")),
                                    "close": as_float(find(row, "close")),
                                    "tick_volume": as_float(find(row, "tick_volume", "volume", "tick_count")),
                                })
                        cur_end = cur_start

        analysis = analyze_manual_deals(deals, symbol=args.symbol, m1_bars=m1_bars)
        report: dict[str, Any] = {
            "ok": True,
            "paper": True,
            "mode": "manual_deals_readonly_audit",
            "executable": False,
            "execution_authorized": False,
            "orders_submitted": 0,
            "hours_requested": args.hours,
            "analysis": analysis,
            "sample_deal_keys": sample_keys if any(d["time"] == 0 or d["price"] == 0 for d in deals) else [],
            "mt5_tool_calls": tool_calls,
            "warnings": [
                "Strictly read-only deal audit: zero orders were created, modified, or submitted.",
                "Use Demo account only for manual scalping experiments.",
                *chart_warnings,
            ],
        }
        saved = _save_private_json(out_path, report)
        report["log_path"] = str(saved or out_path)
        if args.summary:
            print(_format_summary(report))
        else:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    except Blocked as exc:
        print(json.dumps({"ok": False, "orders_submitted": 0, "blocked": exc.reason}, indent=2))
        return 1
    except (McpError, ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "orders_submitted": 0, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
