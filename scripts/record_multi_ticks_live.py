#!/usr/bin/env python3
"""Read-only Multi-Symbol Alpari Wave-Surfing Scanner & 24/7 Second-by-Second Tick Recorder (ORDERS=0).

Two modes (strictly read-only, ZERO broker orders ever submitted):
  1. `--scan`      : Queries live Alpari MT5 Market Watch (including hidden symbols),
                     discovers the EXACT broker symbol names (e.g. *_i), measures live
                     Bid/Ask, Spread, M1 ATR(14), and ATR/Spread wave-surfing score,
                     prints a ranked table + ready-to-run command, and saves a compact
                     git-tracked JSON report to `reports/paper_live/alpari_wave_symbols_scan.json`.
  2. Recording     : Records second-by-second Bid/Ask + M1 S/R context simultaneously for
                     multiple symbols in ONE lightweight terminal (`logs/s1_ticks_<SYM>_YYYYMMDD.jsonl`),
                     plus writes a compact live health report to `reports/paper_live/multi_ticks_recorder.json`.

You do NOT need separate MT5 connectors per chart — ONE MT5 terminal with the existing MCP
connector streams all Market Watch symbols simultaneously.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from daytrade_m1_paper_window import ReadOnlyNativeFeed, parse_quote  # noqa: E402
from daytrade_mt5_readonly_check import (  # noqa: E402
    Blocked,
    compact_bars,
    loopback_native,
    timestamp,
)

from trade_mcp.bridge.native import McpError, find, records, to_epoch  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.s1_velocity_surfer import analyze_m1_sr_context  # noqa: E402

# Wave-surfing asset groups in priority order (excluding XAUUSD which is already recorded separately)
WAVE_CANDIDATE_GROUPS: list[tuple[str, str, tuple[str, ...]]] = [
    ("NASDAQ", "Index (NY wave king)", ("USTEC", "USTECH", "NAS100", "US100", "NQ100", "NDX100", "NDX", "TECH100", "NASDAQ")),
    ("US30", "Index (NY momentum)", ("US30", "DJ30", "WS30", "DJI30", "WALLST30", "DOW")),
    ("DAX", "Index (London open)", ("GER40", "DE40", "DAX40", "GER30", "DE30", "DAX", "GDAXI")),
    ("SILVER", "Metal (Gold twin)", ("XAGUSD", "SILVER")),
    ("GBPJPY", "FX Cross (24h Dragon)", ("GBPJPY",)),
    ("USDJPY", "FX Major (Tokyo+NY)", ("USDJPY",)),
    ("OIL", "Energy (NY bursts)", ("XTIUSD", "USOIL", "WTI", "UKOIL", "XBRUSD", "BRENT", "CRUDE")),
    ("BTC", "Crypto (24/7 incl wknd)", ("BTCUSD", "BITCOIN")),
    ("EURJPY", "FX Cross (Ldn+Tokyo)", ("EURJPY",)),
    ("GBPUSD", "FX Major (London+NY)", ("GBPUSD",)),
    ("SP500", "Index (NY trend)", ("SPX500", "US500", "SP500")),
    ("ETH", "Crypto (24/7)", ("ETHUSD", "ETHEREUM")),
]


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        x = float(val)
        return x if (x == x and abs(x) != float("inf")) else default
    except (TypeError, ValueError):
        return default


def _daily_tick_path(symbol: str, utc_day: str, *, logs_dir: Path | None = None) -> Path:
    base = (ROOT / "logs" if logs_dir is None else logs_dir).resolve()
    safe_sym = "".join(ch if (ch.isalnum() or ch in ("_", "-")) else "_" for ch in symbol)
    target = (base / f"s1_ticks_{safe_sym}_{utc_day}.jsonl").resolve()
    if target.parent != base:
        raise ValueError(f"Invalid tick path outside logs/: {target}")
    return target


def _open_private_append(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    fh = os.fdopen(fd, "a", encoding="utf-8")
    os.chmod(path, 0o600)
    return fh


def _write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _match_symbol_for_prefixes(
    all_names: list[str],
    prefixes: tuple[str, ...],
) -> str | None:
    """Pick the best exact broker symbol matching any prefix (prefers Alpari `_i` suffix)."""
    matches: list[str] = []
    for sym in all_names:
        s = sym.strip()
        if not s:
            continue
        up = s.upper()
        for pref in prefixes:
            if (
                up == pref
                or up.startswith(pref + "_")
                or up.startswith(pref + ".")
                or up.startswith(pref + "-")
                or up.startswith(pref + "#")
                or (up.startswith(pref) and len(up) <= len(pref) + 3 and not up[len(pref) : len(pref) + 1].isalpha())
            ):
                matches.append(s)
                break
    if not matches:
        return None
    # Prefer Alpari `_i` suffix, then shortest exact name
    matches.sort(key=lambda nm: (0 if nm.endswith("_i") else 1, len(nm), nm))
    return matches[0]


def discover_wave_symbols(
    all_rows: list[dict[str, Any]],
    *,
    include_gold: bool = False,
    max_symbols: int = 8,
) -> list[dict[str, Any]]:
    """Discover exact broker symbols matching wave-surfing asset groups."""
    by_name: dict[str, dict[str, Any]] = {}
    all_names: list[str] = []
    for r in all_rows:
        if not isinstance(r, dict):
            continue
        nm = find(r, "name", "symbol", "symbol_name")
        if isinstance(nm, str) and nm.strip():
            s = nm.strip()
            if s not in by_name:
                by_name[s] = r
                all_names.append(s)

    groups = list(WAVE_CANDIDATE_GROUPS)
    if include_gold:
        groups.insert(0, ("GOLD", "Metal (Benchmark)", ("XAUUSD",)))

    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tag, role, prefixes in groups:
        best = _match_symbol_for_prefixes(all_names, prefixes)
        if best is None or best in seen:
            continue
        seen.add(best)
        raw = by_name[best]
        bid = _safe_float(find(raw, "bid"))
        ask = _safe_float(find(raw, "ask"))
        spread_px = round(max(0.0, ask - bid), 6) if (bid > 0 and ask >= bid) else 0.0
        digits = int(_safe_float(find(raw, "digits"), 2))
        point = _safe_float(find(raw, "point"), 10.0 ** (-digits))
        spread_pts = round(spread_px / point, 1) if (point > 0 and spread_px > 0) else _safe_float(find(raw, "spread"))
        found.append({
            "tag": tag,
            "role": role,
            "symbol": best,
            "visible": bool(find(raw, "visible", "select") is True),
            "bid": bid,
            "ask": ask,
            "spread_price": spread_px,
            "spread_points": spread_pts,
            "digits": digits,
            "contract_size": _safe_float(find(raw, "trade_contract_size", "contract_size"), 0.0),
            "tick_value": _safe_float(find(raw, "trade_tick_value", "tick_value"), 0.0),
            "currency_profit": str(find(raw, "currency_profit", "profit_currency") or ""),
            "description": str(find(raw, "description", "display_name") or ""),
        })
        if len(found) >= max_symbols:
            break
    return found


def _server_now(feed: ReadOnlyNativeFeed) -> tuple[float, datetime]:
    clock = feed._call("get_time_information", {})
    raw_time = find(clock, "trade_server_time") or find(clock, "trade_server_last_known_time")
    dt = timestamp(raw_time)
    if dt is None:
        raise Blocked("timestamp_unverified")
    return float(to_epoch(raw_time)), dt.replace(tzinfo=None)


def _fetch_m1_bars(feed: ReadOnlyNativeFeed, *, symbol: str, now_label: datetime, limit: int = 120) -> list[list[float]]:
    start = now_label - timedelta(hours=3)
    end = now_label + timedelta(minutes=2)
    body = feed._call(
        "get_chart_history",
        {
            "symbol": symbol,
            "period": "M1",
            "datetime_from": start.isoformat(timespec="seconds"),
            "datetime_to": end.isoformat(timespec="seconds"),
            "limit": limit,
        },
    )
    bars, _ = compact_bars(body)
    return bars


def _compute_m1_wave_metrics(bars: list[list[float]], *, spread_price: float) -> dict[str, Any]:
    """Compute M1 ATR(14), 5m average range, and ATR/Spread wave-surfing efficiency."""
    if len(bars) < 16:
        return {"atr_m1": 0.0, "atr_to_spread": 0.0, "max_5m_move": 0.0, "wave_grade": "NO_BARS"}
    recent = bars[-60:]
    trs: list[float] = []
    for i in range(1, len(recent)):
        h, lo, pc = float(recent[i][2]), float(recent[i][3]), float(recent[i - 1][4])
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    atr14 = sum(trs[-14:]) / max(1, len(trs[-14:]))
    max_5m = 0.0
    for i in range(5, len(recent)):
        mv = abs(float(recent[i][4]) - float(recent[i - 5][4]))
        if mv > max_5m:
            max_5m = mv
    ratio = (atr14 / spread_price) if spread_price > 1e-9 else 0.0
    if ratio >= 5.0:
        grade = "A_EXCELLENT"
    elif ratio >= 2.8:
        grade = "B_GOOD"
    elif ratio >= 1.6:
        grade = "C_MARGINAL"
    else:
        grade = "D_SPREAD_HEAVY"
    return {
        "atr_m1": round(atr14, 5),
        "atr_to_spread": round(ratio, 2),
        "max_5m_move": round(max_5m, 5),
        "wave_grade": grade,
    }


def _fetch_all_symbols_rows(feed: ReadOnlyNativeFeed, *, include_hidden: bool = True, limit: int = 1000) -> list[dict[str, Any]]:
    raw = feed._call("get_marketwatch_symbols", {"limit": limit, "include_hidden": include_hidden})
    return [r for r in records(raw, "symbols", "market_watch", "data", "items") if isinstance(r, dict)]


def _fetch_batch_quotes(feed: ReadOnlyNativeFeed, symbols: list[str]) -> dict[str, dict[str, float]]:
    """Fetch live Bid/Ask for all target symbols in 1 batch call + fallback per missing symbol."""
    out: dict[str, dict[str, float]] = {}
    target_set = set(symbols)
    try:
        rows = _fetch_all_symbols_rows(feed, include_hidden=False, limit=300)
        for r in rows:
            nm = str(find(r, "name", "symbol", "symbol_name") or "").strip()
            if nm in target_set:
                try:
                    q = parse_quote(r)
                    if q["bid"] > 0 and q["ask"] >= q["bid"]:
                        out[nm] = q
                except Exception:
                    pass
    except Exception:
        pass

    # Fallback individual lookup for any symbol not visible in Market Watch
    for sym in symbols:
        if sym in out:
            continue
        try:
            raw = feed._call("get_marketwatch_symbols", {"symbol": sym, "limit": 5, "include_hidden": True})
            for r in records(raw, "symbols", "market_watch", "data", "items"):
                if not isinstance(r, dict):
                    continue
                nm = str(find(r, "name", "symbol", "symbol_name") or "").strip()
                if nm == sym:
                    q = parse_quote(r)
                    if q["bid"] > 0 and q["ask"] >= q["bid"]:
                        out[sym] = q
                    break
        except Exception:
            continue
    return out


def _format_tick_line(now_ts: float, bid: float, ask: float, m1_ctx: dict[str, Any]) -> str:
    return (
        json.dumps(
            {
                "ts": round(now_ts, 3),
                "bid": bid,
                "ask": ask,
                "m1": {
                    "bias": m1_ctx["bias"],
                    "label": m1_ctx["label"],
                    "support": m1_ctx.get("support"),
                    "resistance": m1_ctx.get("resistance"),
                    "atr_m1": m1_ctx.get("atr_m1"),
                },
            },
            ensure_ascii=False,
        )
        + "\n"
    )


def _run_mock(args: argparse.Namespace) -> int:
    mock_rows = [
        {"name": "XAUUSD_i", "bid": 3572.00, "ask": 3572.40, "digits": 2, "point": 0.01, "visible": True, "trade_contract_size": 100.0},
        {"name": "USTEC_i", "bid": 20980.0, "ask": 20981.8, "digits": 1, "point": 0.1, "visible": True, "trade_contract_size": 1.0},
        {"name": "GER40_i", "bid": 19420.0, "ask": 19421.5, "digits": 1, "point": 0.1, "visible": True, "trade_contract_size": 1.0},
        {"name": "GBPJPY_i", "bid": 194.800, "ask": 194.822, "digits": 3, "point": 0.001, "visible": True, "trade_contract_size": 100000.0},
        {"name": "USDJPY_i", "bid": 153.200, "ask": 153.212, "digits": 3, "point": 0.001, "visible": True, "trade_contract_size": 100000.0},
        {"name": "XAGUSD_i", "bid": 31.420, "ask": 31.452, "digits": 3, "point": 0.001, "visible": False, "trade_contract_size": 5000.0},
        {"name": "BTCUSD_i", "bid": 95200.0, "ask": 95245.0, "digits": 1, "point": 0.1, "visible": False, "trade_contract_size": 1.0},
    ]
    found = discover_wave_symbols(mock_rows, include_gold=args.include_gold, max_symbols=args.max_symbols)
    print(f"[MOCK | ORDERS=0] Discovered {len(found)} wave-surfing symbols: {[f['symbol'] for f in found]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Multi-Symbol Alpari Wave Scanner & 24/7 Tick Recorder (ORDERS=0)"
    )
    parser.add_argument("--scan", action="store_true", help="Scan live Alpari MT5 symbols, spreads & M1 ATR/Spread wave score")
    parser.add_argument("--auto-wave", action="store_true", help="Auto-pick top wave-surfing Alpari symbols and record ticks")
    parser.add_argument(
        "--wave-top3",
        action="store_true",
        help="Record ONLY the top 3 spread-viable non-Gold Alpari wave candidates (NAS100_i XAGUSD_i GER40_i)",
    )
    parser.add_argument(
        "--wave-top4",
        action="store_true",
        help="Record ONLY the top 4 spread-viable non-Gold Alpari wave candidates (NAS100_i GER40_i US500_i XAGUSD_i)",
    )
    parser.add_argument("--symbols", nargs="*", default=[], help="Exact Alpari symbols to record (e.g. NAS100_i XAGUSD_i GER40_i)")
    parser.add_argument("--include-gold", action="store_true", help="Include XAUUSD_i in auto-discovery (off by default if Gold already runs)")
    parser.add_argument("--max-symbols", type=int, default=6, help="Max auto-discovered wave symbols to record (default 6)")
    parser.add_argument("--poll-seconds", type=float, default=0.40, help="Batch quote poll interval seconds (default 0.40)")
    parser.add_argument("--m1-refresh-seconds", type=float, default=30.0, help="M1 context refresh interval seconds (default 30)")
    parser.add_argument("--status-seconds", type=float, default=60.0, help="Console & JSON health refresh seconds (default 60)")
    parser.add_argument("--minutes", type=float, default=0.0, help="Max runtime in minutes (0 = forever until Ctrl+C)")
    parser.add_argument("--mock", action="store_true", help="Offline smoke test (no MT5 needed)")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    if args.mock:
        return _run_mock(args)

    stop_requested = False

    def _handle_sig(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    try:
        load_dotenv(ROOT / ".env")
        app_cfg = Config.from_env()
        if app_cfg.backend != "native":
            raise Blocked("native_backend_required")

        with loopback_native():
            feed = ReadOnlyNativeFeed(app_cfg.mcp_url, app_cfg.mcp_token, "XAUUSD_i", timeout=20.0)
            feed.client.connect()
            srv_ts0, srv_dt0 = _server_now(feed)
            clock_offset = srv_ts0 - time.time()

            # 1) SCAN MODE: discover exact Alpari symbols, live spreads, and M1 ATR/Spread wave score
            if args.scan or (not args.symbols and not args.auto_wave):
                all_rows = _fetch_all_symbols_rows(feed, include_hidden=True, limit=1500)
                found = discover_wave_symbols(all_rows, include_gold=True, max_symbols=14)
                visible_names = sorted(
                    str(find(r, "name", "symbol", "symbol_name") or "").strip()
                    for r in all_rows
                    if find(r, "visible", "select") is True
                )

                enriched: list[dict[str, Any]] = []
                for item in found:
                    sym = item["symbol"]
                    # Refresh single-symbol quote if batch omitted bid/ask
                    if item["bid"] <= 0.0:
                        qmap = _fetch_batch_quotes(feed, [sym])
                        if sym in qmap:
                            item["bid"] = qmap[sym]["bid"]
                            item["ask"] = qmap[sym]["ask"]
                            item["spread_price"] = round(max(0.0, item["ask"] - item["bid"]), 6)
                    try:
                        bars = _fetch_m1_bars(feed, symbol=sym, now_label=srv_dt0, limit=100)
                        wm = _compute_m1_wave_metrics(bars, spread_price=item["spread_price"])
                    except Exception:
                        wm = {"atr_m1": 0.0, "atr_to_spread": 0.0, "max_5m_move": 0.0, "wave_grade": "NO_BARS"}
                    rec = {**item, **wm}
                    enriched.append(rec)

                # Rank by ATR/Spread ratio descending (best wave-surfing candidates first!)
                enriched.sort(key=lambda r: (r["atr_to_spread"], 1 if r["visible"] else 0), reverse=True)

                print(f"\n=== ALPARI MT5 WAVE-SURFING SYMBOL SCAN (TOTAL={len(all_rows)}, ORDERS=0) ===")
                print(f"Visible in Market Watch ({len(visible_names)}): {', '.join(visible_names[:20])}")
                print("-" * 102)
                print(
                    f"{'SYMBOL':14s} | {'GROUP':8s} | {'VIS':3s} | {'BID':10s} | {'SPREAD':8s} | "
                    f"{'M1_ATR':8s} | {'ATR/SP':6s} | {'MAX5M':8s} | {'GRADE':14s} | ROLE"
                )
                print("-" * 102)
                for r in enriched:
                    vis_s = "YES" if r["visible"] else "no"
                    print(
                        f"{r['symbol']:14s} | {r['tag']:8s} | {vis_s:3s} | {r['bid']:10.3f} | "
                        f"{r['spread_price']:8.4f} | {r['atr_m1']:8.4f} | {r['atr_to_spread']:6.2f}x | "
                        f"{r['max_5m_move']:8.4f} | {r['wave_grade']:14s} | {r['role']}"
                    )

                rec_syms = [r["symbol"] for r in enriched if not r["symbol"].upper().startswith("XAUUSD")][:6]
                # Also list non-FX index/commodity/crypto names found on the broker in case of custom Alpari naming
                extra_broker_names = sorted(
                    {
                        str(find(r, "name", "symbol", "symbol_name") or "").strip()
                        for r in all_rows
                        if any(
                            kw in str(find(r, "name", "symbol", "symbol_name") or "").upper()
                            for kw in ("100", "30", "40", "500", "OIL", "WTI", "BRN", "XBR", "XTI", "BTC", "ETH", "XAG", "DAX", "NAS", "SPX")
                        )
                    }
                )
                scan_path = ROOT / "reports" / "paper_live" / "alpari_wave_symbols_scan.json"
                _write_atomic_json(
                    scan_path,
                    {
                        "schema": "alpari_wave_symbols_scan_v1",
                        "orders_submitted": 0,
                        "scanned_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "server_time": srv_dt0.strftime("%Y-%m-%d %H:%M:%S"),
                        "total_broker_symbols": len(all_rows),
                        "visible_marketwatch_symbols": visible_names,
                        "recommended_wave_symbols": rec_syms,
                        "extra_index_commodity_crypto_names": extra_broker_names[:60],
                        "candidates": enriched,
                    },
                )
                print("-" * 102)
                if extra_broker_names:
                    print(f"Broker Indices/Oil/Metals/Crypto names: {', '.join(extra_broker_names[:30])}")
                print(f"SAVED={scan_path.relative_to(ROOT)}")
                if rec_syms:
                    print("\nREADY-TO-RUN MULTI-SYMBOL TICK RECORDER:")
                    print(f"  python3 scripts/record_multi_ticks_live.py --symbols {' '.join(rec_syms)}")
                return 0

            # 2) MULTI-SYMBOL RECORDING MODE
            if args.symbols:
                target_symbols = [s.strip() for s in args.symbols if s.strip()]
            elif args.wave_top4:
                target_symbols = ["NAS100_i", "GER40_i", "US500_i", "XAGUSD_i"]
                if args.include_gold:
                    target_symbols.insert(0, "XAUUSD_i")
            elif args.wave_top3:
                target_symbols = ["NAS100_i", "XAGUSD_i", "GER40_i"]
                if args.include_gold:
                    target_symbols.insert(0, "XAUUSD_i")
            else:
                all_rows = _fetch_all_symbols_rows(feed, include_hidden=True, limit=1500)
                found = discover_wave_symbols(
                    all_rows,
                    include_gold=args.include_gold,
                    max_symbols=max(1, int(args.max_symbols)),
                )
                target_symbols = [f["symbol"] for f in found]

            if not target_symbols:
                print("No target symbols resolved. Run with --scan first.", file=sys.stderr)
                return 2

            started_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
            t_start = time.monotonic()
            deadline = (t_start + args.minutes * 60.0) if args.minutes > 0 else None
            cur_day = datetime.now(timezone.utc).strftime("%Y%m%d")

            fhs: dict[str, Any] = {}
            paths: dict[str, Path] = {}
            m1_ctxs: dict[str, dict[str, Any]] = {}
            stats: dict[str, dict[str, Any]] = {}
            for sym in target_symbols:
                p = _daily_tick_path(sym, cur_day)
                paths[sym] = p
                fhs[sym] = _open_private_append(p)
                m1_ctxs[sym] = {"bias": 0, "label": "WARMING_UP", "support": None, "resistance": None, "atr_m1": None}
                stats[sym] = {
                    "ticks_total": 0,
                    "ticks_day": 0,
                    "frozen_skipped": 0,
                    "last_bid": 0.0,
                    "last_ask": 0.0,
                    "last_change_ts": 0.0,
                    "spread_sum": 0.0,
                    "low": None,
                    "high": None,
                }

            report_path = ROOT / "reports" / "paper_live" / "multi_ticks_recorder.json"
            print(
                f"[START | MULTI-SYMBOL TICK RECORDER | ORDERS=0] ({len(target_symbols)} symbols): "
                f"{', '.join(target_symbols)} | poll={args.poll_seconds}s | report={report_path.relative_to(ROOT)}",
                flush=True,
            )

            last_m1_mono = 0.0
            last_status_mono = 0.0
            reconnects = 0
            consecutive_errors = 0

            def _write_health(status_label: str) -> None:
                sym_rows: dict[str, Any] = {}
                for sym in target_symbols:
                    st = stats[sym]
                    n = max(1, int(st["ticks_total"]))
                    lo, hi = st["low"], st["high"]
                    avg_sp = round(st["spread_sum"] / n, 5) if st["ticks_total"] > 0 else 0.0
                    rng = round(hi - lo, 5) if (lo is not None and hi is not None) else 0.0
                    sym_rows[sym] = {
                        "file": paths[sym].name,
                        "ticks_total": st["ticks_total"],
                        "last_bid": st["last_bid"],
                        "last_ask": st["last_ask"],
                        "avg_spread": avg_sp,
                        "session_range": rng,
                        "range_to_spread": round(rng / avg_sp, 2) if avg_sp > 1e-9 else 0.0,
                        "m1_label": m1_ctxs[sym]["label"],
                        "m1_atr": m1_ctxs[sym].get("atr_m1"),
                    }
                _write_atomic_json(
                    report_path,
                    {
                        "schema": "multi_ticks_recorder_v1",
                        "paper_only": True,
                        "orders_submitted": 0,
                        "status": status_label,
                        "started_utc": started_utc,
                        "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "elapsed_minutes": round((time.monotonic() - t_start) / 60.0, 2),
                        "reconnect_count": reconnects,
                        "consecutive_errors": consecutive_errors,
                        "symbols": sym_rows,
                    },
                )

            try:
                while not stop_requested:
                    loop_t0 = time.monotonic()
                    try:
                        day_now = datetime.now(timezone.utc).strftime("%Y%m%d")
                        if day_now != cur_day:
                            for sym in target_symbols:
                                fhs[sym].flush()
                                fhs[sym].close()
                                p = _daily_tick_path(sym, day_now)
                                paths[sym] = p
                                fhs[sym] = _open_private_append(p)
                                stats[sym]["ticks_day"] = 0
                            cur_day = day_now
                            print(f"[ROTATE] Switched all {len(target_symbols)} tick logs to UTC day {cur_day}", flush=True)

                        if (loop_t0 - last_m1_mono) >= args.m1_refresh_seconds or last_m1_mono == 0.0:
                            srv_ts, srv_dt = _server_now(feed)
                            clock_offset = srv_ts - time.time()
                            for sym in target_symbols:
                                try:
                                    bars = _fetch_m1_bars(feed, symbol=sym, now_label=srv_dt, limit=150)
                                    m1_ctxs[sym] = analyze_m1_sr_context(bars, server_now_ts=srv_ts)
                                except Exception:
                                    pass
                            last_m1_mono = loop_t0

                        quotes = _fetch_batch_quotes(feed, target_symbols)
                        now_ts = time.time() + clock_offset
                        consecutive_errors = 0
                        for sym in target_symbols:
                            q = quotes.get(sym)
                            if q is None:
                                continue
                            bid, ask = float(q["bid"]), float(q["ask"])
                            if bid <= 0.0 or ask < bid:
                                continue
                            st = stats[sym]
                            if st["ticks_total"] > 0 and bid == st["last_bid"] and ask == st["last_ask"]:
                                if (now_ts - float(st["last_change_ts"])) >= 10.0:
                                    st["frozen_skipped"] += 1
                                    continue
                            else:
                                st["last_change_ts"] = now_ts
                            fhs[sym].write(_format_tick_line(now_ts, bid, ask, m1_ctxs[sym]))
                            st["ticks_total"] += 1
                            st["ticks_day"] += 1
                            st["last_bid"] = bid
                            st["last_ask"] = ask
                            st["spread_sum"] += (ask - bid)
                            mid = 0.5 * (bid + ask)
                            st["low"] = mid if st["low"] is None else min(st["low"], mid)
                            st["high"] = mid if st["high"] is None else max(st["high"], mid)
                            if st["ticks_total"] % 25 == 0:
                                fhs[sym].flush()

                        if (loop_t0 - last_status_mono) >= args.status_seconds:
                            for fh in fhs.values():
                                fh.flush()
                            _write_health("running")
                            now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S")
                            brief = " | ".join(
                                f"{sym}:{stats[sym]['ticks_total']}t(sp={stats[sym]['last_ask'] - stats[sym]['last_bid']:.4g})"
                                for sym in target_symbols
                            )
                            print(f"[{now_utc}Z] [MULTI-REC | ORDERS=0] {brief}", flush=True)
                            last_status_mono = loop_t0
                    except (Blocked, McpError, OSError, TimeoutError, ValueError) as exc:
                        consecutive_errors += 1
                        reconnects += 1
                        backoff = min(15.0, float(consecutive_errors))
                        print(f"[WARN] Multi-recorder transient error ({exc}); retry #{consecutive_errors} in {backoff:.0f}s...", file=sys.stderr, flush=True)
                        time.sleep(backoff)
                        with contextlib.suppress(Exception):
                            feed.client.connect()

                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    sleep_left = max(0.05, args.poll_seconds - (time.monotonic() - loop_t0))
                    time.sleep(sleep_left)
            finally:
                for fh in fhs.values():
                    fh.flush()
                    fh.close()
                _write_health("stopped")

            print(f"=== MULTI-SYMBOL TICK RECORDER STOPPED CLEANLY (ORDERS=0) | REPORT={report_path.relative_to(ROOT)} ===", flush=True)
            return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "orders_submitted": 0, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
