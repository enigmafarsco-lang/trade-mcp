"""Unified Multi-Symbol All-Weather V5 Live Paper Monitor & Tick Recorder (ORDERS=0).

Runs the cross-market-proven, bug-fixed `NormalizedAllWeatherV5` (`COMBO_AW_V5_24H`) on non-Gold
broker symbols in a single terminal window, with optional simultaneous tick recording (`--record`).

Production V5 safeguards built into every symbol:
  - 60m/120m HTF counter-trend guard on VSNAP6 + MACRO_WAVE (B6_HTF120_DYN_L1),
  - 200-bar M1 startup warmup seeding 120m HTF & ZSCORE_MR from tick #1,
  - Scale-free M1 ATR & S/R analyzer (`analyze_scale_free_m1_context` — accurate for Silver/FX/Indices),
  - Cross-engine single-tick anti-teleport guard (4.5x scale / 90s cooldown across all 4 engines),
  - Stale-quote market-freeze detector (300s) + post-reopen gap warmup (120s),
  - Active-day spread anchoring for `price_scale` (immune to midnight rollover wide-spread startups),
  - 10s frozen-quote throttle on recorded ticks (prevents weekend/rollover bloat).

STRICTLY READ-ONLY / PAPER-ONLY: ORDERS=0 (`TRADE_MCP_ALLOW_TRADING=false`).

Usage:
  # Instant offline synthetic verification (no MT5 needed)
  python3 scripts/daytrade_multi_symbol_live.py --mock

  # Offline replay on all recorded non-Gold logs through the exact live V5 code path
  python3 scripts/daytrade_multi_symbol_live.py --replay --preset top4

  # Monday live paper trading (Window 2 alongside Gold V5 Window 1)
  python3 scripts/daytrade_multi_symbol_live.py --preset top4 --record
  python3 scripts/daytrade_multi_symbol_live.py --preset proven --record
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
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from audit_multi_symbol_waves import (  # noqa: E402
    discover_recorded_symbol_files,
    load_deduplicated_symbol_segments,
)
from daytrade_m1_paper_window import ReadOnlyNativeFeed  # noqa: E402
from daytrade_mt5_readonly_check import compact_bars, loopback_native  # noqa: E402
from record_multi_ticks_live import (  # noqa: E402
    _daily_tick_path,
    _fetch_all_symbols_rows,
    _fetch_batch_quotes,
    _format_tick_line,
    _open_private_append,
    _safe_float,
    _server_now,
    _write_atomic_json,
)

from trade_mcp.bridge.native import find  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.multi_symbol_aw_v4 import (  # noqa: E402
    GOLD_REF_SL_RISK_USD,
    GOLD_REF_SPREAD,
    AlpariSymbolSpec,
    NormalizedAllWeatherV5,
    analyze_scale_free_m1_context,
    compute_risk_parity_lot_scale,
    load_alpari_specs,
)


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# Verified active-session median spreads from full-week Alpari tick audits.
# Anchoring price_scale to active-session median spread prevents midnight wide-spread distortion.
ACTIVE_MEDIAN_SPREAD_DEFAULTS: dict[str, float] = {
    "NAS100_i": 3.90,   # S = 9.75 (168,854 active ticks)
    "XAGUSD_i": 0.028,  # S = 0.070 (194,312 active ticks)
    "US500_i": 1.30,    # S = 3.25 (49,510 active ticks)
    "GER40_i": 3.60,    # S = 9.00 (208,248 active ticks)
    "XAUUSD_i": 0.40,   # S = 1.00 (Gold reference)
}

PRESET_DEFINITIONS: dict[str, tuple[list[str], list[str]]] = {
    # (proven_active_symbols, watchlist_shadow_symbols)
    "proven": (["NAS100_i", "XAGUSD_i"], []),
    "top4": (["NAS100_i", "XAGUSD_i"], ["US500_i", "GER40_i"]),
}


def _fmt_usd(val: float) -> str:
    return f"+${val:,.2f}" if val >= 0 else f"-${abs(val):,.2f}"


def _ensure_in_reports_or_logs(path: Path) -> Path:
    resolved = path.resolve()
    allowed_roots = ((ROOT / "reports").resolve(), (ROOT / "logs").resolve())
    for base in allowed_roots:
        if resolved == base or base in resolved.parents:
            return resolved
    raise ValueError(f"Output path must stay inside reports/ or logs/: {path}")


def _load_reference_spreads() -> dict[str, float]:
    """Merge verified defaults with observed spreads from audit/scan JSON reports."""
    spreads = dict(ACTIVE_MEDIAN_SPREAD_DEFAULTS)
    scan_path = ROOT / "reports" / "paper_live" / "alpari_wave_symbols_scan.json"
    if scan_path.exists():
        try:
            payload = json.loads(scan_path.read_text(encoding="utf-8"))
            for row in payload.get("candidates", []):
                if not isinstance(row, dict):
                    continue
                sym = str(row.get("symbol") or "").strip()
                sp = _safe_float(row.get("spread_price"), 0.0)
                if sym and sp > 0.0 and sym not in spreads:
                    spreads[sym] = sp
        except (OSError, json.JSONDecodeError):
            pass
    audit_path = ROOT / "reports" / "paper_live" / "multi_symbol_wave_audit.json"
    if audit_path.exists():
        try:
            payload = json.loads(audit_path.read_text(encoding="utf-8"))
            for row in payload.get("symbols", []):
                if not isinstance(row, dict):
                    continue
                sym = str(row.get("symbol") or "").strip()
                ms = row.get("microstructure", {})
                sp = _safe_float(ms.get("median_spread"), 0.0) if isinstance(ms, dict) else 0.0
                if sym and sp > 0.0:
                    spreads[sym] = sp
        except (OSError, json.JSONDecodeError):
            pass
    return spreads


def _fetch_m1_bars_4h(feed: ReadOnlyNativeFeed, *, symbol: str, now_label: datetime, limit: int = 200) -> list[list[float]]:
    start = now_label - timedelta(hours=4)
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


def _override_specs_from_live_rows(
    specs: dict[str, AlpariSymbolSpec],
    live_rows: list[dict[str, Any]],
) -> dict[str, AlpariSymbolSpec]:
    out = dict(specs)
    for row in live_rows:
        if not isinstance(row, dict):
            continue
        sym = str(find(row, "name", "symbol", "symbol_name") or "").strip()
        if not sym:
            continue
        base = out.get(sym)
        csize = _safe_float(find(row, "trade_contract_size", "contract_size"), base.contract_size if base else 1.0)
        cprof = str(find(row, "currency_profit", "profit_currency") or (base.currency_profit if base else "USD")).upper()
        digits = int(_safe_float(find(row, "digits"), base.digits if base else 2))
        vmin = _safe_float(find(row, "volume_min"), base.volume_min if base else 0.01)
        vstep = _safe_float(find(row, "volume_step"), base.volume_step if base else 0.01)
        out[sym] = AlpariSymbolSpec(
            symbol=sym,
            asset_class=base.asset_class if base else "OTHER",
            contract_size=max(csize, 1e-6),
            currency_profit=cprof or "USD",
            digits=max(0, min(6, digits)),
            volume_min=max(vmin, 0.01),
            volume_step=max(vstep, 0.01),
            active_hours_srv=base.active_hours_srv if base else None,
        )
    return out


def resolve_symbol_plan(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """Return `(active_symbols, watchlist_symbols)` from CLI args."""
    if args.symbols:
        seen: set[str] = set()
        custom: list[str] = []
        for s in args.symbols:
            clean = s.strip()
            if clean and clean not in seen:
                seen.add(clean)
                custom.append(clean)
        return custom, []
    active, watch = PRESET_DEFINITIONS[args.preset]
    return list(active), list(watch)


def build_symbol_engine(
    symbol: str,
    *,
    spec: AlpariSymbolSpec,
    ref_spread: float,
    mid_price: float,
    capital_usd: float,
    risk_mult: float,
    session_hours_only: bool = False,
) -> tuple[NormalizedAllWeatherV5, dict[str, Any]]:
    """Build a risk-parity-calibrated `NormalizedAllWeatherV5` engine for one symbol."""
    price_scale = max(ref_spread / GOLD_REF_SPREAD, 1e-5)
    usd_per_unit = spec.usd_per_price_unit(mid_price)
    lot_scale, q_vsnap_lot, actual_sl_usd, min_lot_clamped = compute_risk_parity_lot_scale(
        price_scale=price_scale,
        usd_per_price_unit=usd_per_unit,
        target_vsnap_sl_usd=GOLD_REF_SL_RISK_USD * float(risk_mult),
        volume_min=spec.volume_min,
        volume_step=spec.volume_step,
    )
    allow_partial = q_vsnap_lot >= (2.0 * spec.volume_min - 1e-9)
    active_hrs = spec.active_hours_srv if session_hours_only else None
    eng = NormalizedAllWeatherV5(
        symbol=symbol,
        price_scale=price_scale,
        usd_per_price_unit=usd_per_unit,
        lot_scale=lot_scale,
        digits=spec.digits,
        initial_capital_usd=capital_usd,
        active_hours_srv=active_hrs,
        allow_partial_split=allow_partial,
        risk_multiplier=1.0,  # risk_mult is already baked cleanly into quantized lot_scale
    )
    meta = {
        "symbol": symbol,
        "asset_class": spec.asset_class,
        "ref_spread": round(ref_spread, max(2, spec.digits + 1)),
        "price_scale": round(price_scale, 5),
        "usd_per_unit": round(usd_per_unit, 4),
        "lot_scale": round(lot_scale, 4),
        "vsnap_lot": round(q_vsnap_lot, 2),
        "sl_risk_usd": round(actual_sl_usd, 2),
        "min_lot_clamped": min_lot_clamped,
        "allow_partial_split": allow_partial,
        "active_hours": list(active_hrs) if active_hrs is not None else "24H",
    }
    return eng, meta


def _print_trade_event(symbol: str, role: str, ev: dict[str, Any], *, ts: float, sym_realized: float) -> None:
    t_str = _utc_iso(ts)[11:19]
    etype = str(ev.get("type", ""))
    lbl = str(ev.get("engine_label", ev.get("engine", "")))
    side = str(ev.get("side", "")).upper()
    tag = f"[{role}:{symbol}]"
    if etype == "OPEN_L1":
        print(
            f"[{t_str}] {tag:16s} OPEN  {side:4s} {float(ev.get('leg_lot', 0.0)):.2f} lot @ {ev.get('entry')} "
            f"| SL {ev.get('stop')} | {lbl}",
            flush=True,
        )
    elif etype == "FIRE_ADD":
        print(
            f"[{t_str}] {tag:16s} ADD   {side:4s} +{float(ev.get('leg_lot', 0.0)):.2f} "
            f"(tot {float(ev.get('total_volume', 0.0)):.2f}) @ {ev.get('add_price')} | SL->{ev.get('stop')} | {lbl}",
            flush=True,
        )
    elif etype == "PARTIAL_TP":
        banked = float(ev.get("banked_pnl_usd", ev.get("partial_pnl_usd", 0.0)))
        print(
            f"[{t_str}] {tag:16s} P50%  {side:4s} -{float(ev.get('closed_volume', 0.0)):.2f} @ {ev.get('price')} "
            f"| Banked {_fmt_usd(banked)} | Rem {float(ev.get('remaining_volume', 0.0)):.2f} | SL->{ev.get('stop')} | {lbl}",
            flush=True,
        )
    elif etype == "CLOSE_ALL":
        w = ev.get("wave", {})
        if not isinstance(w, dict):
            return
        w_side = str(w.get("side", side)).upper()
        pnl = float(w.get("net_pnl_usd", 0.0))
        mark = "WIN " if pnl > 0 else "LOSS"
        print(
            f"[{t_str}] {tag:16s} CLOSE {mark} {w_side:4s} {float(w.get('total_volume', 0.0)):.2f} lot "
            f"{w.get('avg_entry')}->{w.get('exit_price')} | {_fmt_usd(pnl)} | "
            f"SymTot {_fmt_usd(sym_realized)} | {lbl} ({w.get('close_reason', w.get('exit_reason', ''))})",
            flush=True,
        )


def _compute_portfolio_totals(
    symbols: list[str],
    engines: dict[str, NormalizedAllWeatherV5],
    last_quotes: dict[str, tuple[float, float]],
) -> dict[str, Any]:
    realized = 0.0
    floating = 0.0
    trades = 0
    wins = 0
    losses = 0
    gross_w = 0.0
    gross_l = 0.0
    sum_dd = 0.0
    open_cnt = 0
    for sym in symbols:
        eng = engines.get(sym)
        if eng is None:
            continue
        bid, ask = last_quotes.get(sym, (0.0, 0.0))
        acct = eng.account_summary(bid, ask)
        realized += float(acct["net_profit_usd"])
        floating += float(acct["floating_pnl_usd"])
        trades += int(acct["trades_count"])
        wins += int(acct["wins"])
        losses += int(acct["losses"])
        sum_dd += float(acct["max_dd_usd"])
        open_cnt += len(eng.open_positions(bid, ask))
        for w in eng.waves_closed:
            p = float(w["net_pnl_usd"])
            if p > 0:
                gross_w += p
            else:
                gross_l += abs(p)
    pf = round(gross_w / gross_l, 2) if gross_l > 0 else (99.9 if gross_w > 0 else None)
    wr = round(100.0 * wins / trades, 1) if trades > 0 else 0.0
    return {
        "symbols": list(symbols),
        "realized_pnl_usd": round(realized, 2),
        "floating_pnl_usd": round(floating, 2),
        "total_pnl_usd": round(realized + floating, 2),
        "sum_symbol_max_dd_usd": round(sum_dd, 2),
        "trades_count": trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": wr,
        "pf": pf,
        "open_positions_count": open_cnt,
    }


def _unpack_tick(tick: Any) -> tuple[float, float, float]:
    """Unpack either `(ts, bid, ask, ...)` tuples or `{'ts','bid','ask'}` dicts."""
    if isinstance(tick, dict):
        return float(tick["ts"]), float(tick["bid"]), float(tick["ask"])
    return float(tick[0]), float(tick[1]), float(tick[2])


def _print_dashboard(
    *,
    active_syms: list[str],
    watch_syms: list[str],
    engines: dict[str, NormalizedAllWeatherV5],
    metas: dict[str, dict[str, Any]],
    last_quotes: dict[str, tuple[float, float]],
    tick_counts: dict[str, int],
    rec_counts: dict[str, int],
    now_ts: float,
    elapsed_min: float,
    recording: bool,
) -> None:
    del metas, tick_counts, rec_counts
    t_str = _utc_iso(now_ts)[:19].replace("T", " ")
    tot_active = _compute_portfolio_totals(active_syms, engines, last_quotes)
    rec_lbl = "REC=ON" if recording else "REC=OFF"
    print(f"\n{'=' * 68}", flush=True)
    print(
        f" MULTI-SYMBOL ALL-WEATHER V5 PAPER  |  {t_str}  |  ORDERS=0  |  {rec_lbl}",
        flush=True,
    )
    print("-" * 68, flush=True)
    print(f"{'SYMBOL':18s} {'PRICE':>10s} {'TRADES':>7s} {'PROFIT / LOSS':>16s}", flush=True)
    for role, syms in (("PROVEN", active_syms), ("WATCH", watch_syms)):
        for sym in syms:
            eng = engines.get(sym)
            if eng is None:
                continue
            bid, ask = last_quotes.get(sym, (0.0, 0.0))
            d = eng.digits
            acct = eng.account_summary(bid, ask)
            sym_total = float(acct["net_profit_usd"]) + float(acct["floating_pnl_usd"])
            label = sym if role == "PROVEN" else f"{sym} (watch)"
            print(
                f"{label:18s} {bid:10.{d}f} {acct['trades_count']:6d}   {_fmt_usd(sym_total):>14s}",
                flush=True,
            )
            for pos in eng.open_positions(bid, ask):
                print(
                    f"   OPEN {sym}: {pos['side']:4s} {pos['volume']:.2f} @ {pos['avg_entry']} "
                    f"| SL {pos['stop']} | {_fmt_usd(pos['pnl_usd'])}",
                    flush=True,
                )
    print("-" * 68, flush=True)
    print(f" PROFIT / LOSS: {_fmt_usd(tot_active['total_pnl_usd'])}", flush=True)
    print("=" * 68, flush=True)


def _build_snapshot(
    *,
    started_utc: str,
    preset: str,
    risk_mult: float,
    active_syms: list[str],
    watch_syms: list[str],
    engines: dict[str, NormalizedAllWeatherV5],
    metas: dict[str, dict[str, Any]],
    last_quotes: dict[str, tuple[float, float]],
    tick_counts: dict[str, int],
    rec_counts: dict[str, int],
    seeded_counts: dict[str, int],
    reconnect_count: int,
    consecutive_errors: int,
    mode: str,
) -> dict[str, Any]:
    per_sym: dict[str, Any] = {}
    for role, syms in (("proven", active_syms), ("watchlist", watch_syms)):
        for sym in syms:
            eng = engines.get(sym)
            if eng is None:
                continue
            bid, ask = last_quotes.get(sym, (0.0, 0.0))
            acct = eng.account_summary(bid, ask)
            per_sym[sym] = {
                "role": role,
                "meta": metas.get(sym, {}),
                "quote": {"bid": bid, "ask": ask, "spread": round(max(0.0, ask - bid), max(2, eng.digits + 1))},
                "account": acct,
                "open_positions": eng.open_positions(bid, ask),
                "ticks_processed": tick_counts.get(sym, 0),
                "ticks_recorded": rec_counts.get(sym, 0),
                "seeded_m1_bars": seeded_counts.get(sym, 0),
                "recent_trades": eng.waves_closed[-15:],
            }
    return {
        "schema": "multi_symbol_aw_v5_live_v1",
        "mode": mode,
        "paper_only": True,
        "orders_submitted": 0,
        "engine_version": "v5",
        "preset": preset,
        "risk_multiplier": risk_mult,
        "started_utc": started_utc,
        "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reconnect_count": reconnect_count,
        "consecutive_errors": consecutive_errors,
        "proven_portfolio": _compute_portfolio_totals(active_syms, engines, last_quotes),
        "watchlist_summary": _compute_portfolio_totals(watch_syms, engines, last_quotes),
        "symbols": per_sym,
    }


def _make_mock_symbol_ticks(symbol: str, *, base_px: float, spread: float, scale: float) -> list[tuple[float, float, float]]:
    """Generate a synthetic tick stream with a clean impulse wave + a bad-tick teleport spike."""
    t0 = 1795860000.0  # 2026-11-26 10:00 UTC
    ticks: list[tuple[float, float, float]] = []
    px = base_px
    # 15m quiet baseline
    for i in range(900):
        px = base_px + (0.15 * scale) * ((i % 7) - 3) / 3.0
        ticks.append((t0 + i, round(px, 4), round(px + spread, 4)))
    # Inject a single-tick 6x bad-tick spike that MUST be blocked by the V5 teleport guard
    spike_ts = t0 + 901
    ticks.append((spike_ts, round(base_px + 6.5 * scale, 4), round(base_px + 6.5 * scale + spread, 4)))
    ticks.append((spike_ts + 1, round(base_px, 4), round(base_px + spread, 4)))
    # 120s cooldown absorption, then clean multi-step bullish wave (+12x scale over 40s)
    for i in range(130):
        ticks.append((spike_ts + 2 + i, round(base_px, 4), round(base_px + spread, 4)))
    wave_t0 = spike_ts + 135
    for i in range(180):
        if i < 20:
            px = base_px + (0.35 * scale) * i
        elif i < 90:
            px = base_px + (7.0 * scale) + (0.12 * scale) * (i - 20)
        else:
            px = base_px + (15.4 * scale) - (0.06 * scale) * (i - 90)
        ticks.append((wave_t0 + i, round(px, 4), round(px + spread, 4)))
    return ticks


def _run_mock(args: argparse.Namespace, report_path: Path | None) -> int:
    active_syms, watch_syms = resolve_symbol_plan(args)
    all_syms = active_syms + watch_syms
    specs = load_alpari_specs(ROOT / "reports" / "paper_live" / "alpari_wave_symbols_scan.json")
    ref_spreads = _load_reference_spreads()

    mock_prices = {"NAS100_i": 25300.0, "XAGUSD_i": 44.500, "US500_i": 6820.0, "GER40_i": 23800.0}
    engines: dict[str, NormalizedAllWeatherV5] = {}
    metas: dict[str, dict[str, Any]] = {}
    last_quotes: dict[str, tuple[float, float]] = {}
    tick_counts: dict[str, int] = {}
    rec_counts: dict[str, int] = {}
    seeded_counts: dict[str, int] = {}
    started_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for sym in all_syms:
        spec = specs.get(sym, AlpariSymbolSpec(sym, "OTHER", 1.0, "USD", 2))
        ref_sp = ref_spreads.get(sym, 0.40)
        base_px = mock_prices.get(sym, 1000.0)
        eng, meta = build_symbol_engine(
            sym,
            spec=spec,
            ref_spread=ref_sp,
            mid_price=base_px,
            capital_usd=args.capital,
            risk_mult=args.risk_mult,
            session_hours_only=args.session_hours_only,
        )
        engines[sym] = eng
        metas[sym] = meta
        seeded_counts[sym] = 0
        ticks = _make_mock_symbol_ticks(sym, base_px=base_px, spread=ref_sp, scale=eng.price_scale)
        role = "PROVEN" if sym in active_syms else "WATCH"
        m1_ctx = {"bias": 1, "label": "BULL_TREND", "support": base_px - 2 * eng.price_scale, "resistance": base_px + 2 * eng.price_scale, "atr_m1": eng.price_scale}
        last_ts, last_bid, last_ask = 0.0, base_px, base_px + ref_sp
        for ts, bid, ask in ticks:
            evs = eng.on_tick(ts, bid, ask, m1_ctx)
            for ev in evs:
                _print_trade_event(sym, role, ev, ts=ts, sym_realized=eng.suite.realized_pnl_usd)
            last_ts, last_bid, last_ask = ts, bid, ask
        eng.close_open(last_ts, last_bid, last_ask, reason="eof_mock")
        last_quotes[sym] = (last_bid, last_ask)
        tick_counts[sym] = len(ticks)
        rec_counts[sym] = 0

    _print_dashboard(
        active_syms=active_syms,
        watch_syms=watch_syms,
        engines=engines,
        metas=metas,
        last_quotes=last_quotes,
        tick_counts=tick_counts,
        rec_counts=rec_counts,
        now_ts=1795861300.0,
        elapsed_min=20.0,
        recording=False,
    )
    if report_path is not None:
        snap = _build_snapshot(
            started_utc=started_utc,
            preset=args.preset,
            risk_mult=args.risk_mult,
            active_syms=active_syms,
            watch_syms=watch_syms,
            engines=engines,
            metas=metas,
            last_quotes=last_quotes,
            tick_counts=tick_counts,
            rec_counts=rec_counts,
            seeded_counts=seeded_counts,
            reconnect_count=0,
            consecutive_errors=0,
            mode="mock",
        )
        _write_atomic_json(report_path, snap)
    return 0


def _run_replay(args: argparse.Namespace, report_path: Path | None) -> int:
    active_syms, watch_syms = resolve_symbol_plan(args)
    all_syms = active_syms + watch_syms
    logs_dir = Path(args.logs_dir)
    specs = load_alpari_specs(ROOT / "reports" / "paper_live" / "alpari_wave_symbols_scan.json")
    ref_spreads = _load_reference_spreads()
    file_map = discover_recorded_symbol_files(logs_dir)

    engines: dict[str, NormalizedAllWeatherV5] = {}
    metas: dict[str, dict[str, Any]] = {}
    last_quotes: dict[str, tuple[float, float]] = {}
    tick_counts: dict[str, int] = {}
    rec_counts: dict[str, int] = {}
    seeded_counts: dict[str, int] = {}
    started_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    latest_ts = time.time()

    print("=" * 96)
    print(f"MULTI-SYMBOL ALL-WEATHER V5 OFFLINE REPLAY  |  preset={args.preset}  |  risk={args.risk_mult:.2g}x  |  ORDERS=0")
    print("=" * 96)

    for sym in all_syms:
        files = file_map.get(sym, [])
        if not files:
            print(f"  [SKIP] {sym}: no recorded logs/s1_ticks_{sym}_*.jsonl files found in {logs_dir}")
            continue
        segs, raw_cnt = load_deduplicated_symbol_segments(files, gap_split_sec=120.0, freeze_split_sec=300.0)
        all_t = [t for seg in segs for t in seg]
        if len(all_t) < 100:
            print(f"  [SKIP] {sym}: only {len(all_t)} active ticks after freeze stripping")
            continue

        spreads_obs = sorted(max(0.0, _unpack_tick(t)[2] - _unpack_tick(t)[1]) for t in all_t)
        med_sp = spreads_obs[len(spreads_obs) // 2]
        ref_sp = med_sp if med_sp > 0 else ref_spreads.get(sym, 0.40)
        _, mid_b, mid_a = _unpack_tick(all_t[len(all_t) // 2])
        mid_px = 0.5 * (mid_b + mid_a)
        spec = specs.get(sym, AlpariSymbolSpec(sym, "OTHER", 1.0, "USD", 2))
        eng, meta = build_symbol_engine(
            sym,
            spec=spec,
            ref_spread=ref_sp,
            mid_price=mid_px,
            capital_usd=args.capital,
            risk_mult=args.risk_mult,
            session_hours_only=args.session_hours_only,
        )
        engines[sym] = eng
        metas[sym] = meta
        seeded_counts[sym] = 0
        role = "PROVEN" if sym in active_syms else "WATCH"

        # Causal scale-free M1 bar builder during offline replay
        m1_bars: list[list[float]] = []
        cur_min: int | None = None
        bo = bh = bl = bc = 0.0
        cur_ctx: dict[str, Any] = {"bias": 0, "label": "INSUFFICIENT_M1", "support": None, "resistance": None, "atr_m1": 0.0}
        last_ts, last_bid, last_ask = _unpack_tick(all_t[0])

        for si, seg in enumerate(segs):
            if si > 0:
                eng.reset_segment()
                cur_min = None
            for t in seg:
                ts, bid, ask = _unpack_tick(t)
                mid = 0.5 * (bid + ask)
                m = int(ts // 60) * 60
                if cur_min is None:
                    cur_min, bo, bh, bl, bc = m, mid, mid, mid, mid
                elif m != cur_min:
                    m1_bars.append([float(cur_min), bo, bh, bl, bc, 1.0])
                    if len(m1_bars) > 220:
                        m1_bars = m1_bars[-220:]
                    cur_ctx = analyze_scale_free_m1_context(m1_bars)
                    cur_min, bo, bh, bl, bc = m, mid, mid, mid, mid
                else:
                    if mid > bh:
                        bh = mid
                    if mid < bl:
                        bl = mid
                    bc = mid

                evs = eng.on_tick(ts, bid, ask, cur_ctx)
                for ev in evs:
                    if args.verbose or ev.get("type") == "CLOSE_ALL":
                        _print_trade_event(sym, role, ev, ts=ts, sym_realized=eng.suite.realized_pnl_usd)
                last_ts, last_bid, last_ask = ts, bid, ask
            eng.close_open(last_ts, last_bid, last_ask, reason="eof_seg")

        last_quotes[sym] = (last_bid, last_ask)
        tick_counts[sym] = len(all_t)
        rec_counts[sym] = raw_cnt
        latest_ts = max(latest_ts, last_ts)

    _print_dashboard(
        active_syms=[s for s in active_syms if s in engines],
        watch_syms=[s for s in watch_syms if s in engines],
        engines=engines,
        metas=metas,
        last_quotes=last_quotes,
        tick_counts=tick_counts,
        rec_counts=rec_counts,
        now_ts=latest_ts,
        elapsed_min=0.0,
        recording=False,
    )
    if report_path is not None:
        snap = _build_snapshot(
            started_utc=started_utc,
            preset=args.preset,
            risk_mult=args.risk_mult,
            active_syms=[s for s in active_syms if s in engines],
            watch_syms=[s for s in watch_syms if s in engines],
            engines=engines,
            metas=metas,
            last_quotes=last_quotes,
            tick_counts=tick_counts,
            rec_counts=rec_counts,
            seeded_counts=seeded_counts,
            reconnect_count=0,
            consecutive_errors=0,
            mode="replay",
        )
        _write_atomic_json(report_path, snap)
        print(f"Saved replay report -> {report_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Unified Multi-Symbol All-Weather V5 Live Paper Monitor & Tick Recorder (ORDERS=0)"
    )
    parser.add_argument(
        "--preset",
        default="proven",
        choices=tuple(PRESET_DEFINITIONS.keys()),
        help="Symbol preset: 'proven' (NAS100_i+XAGUSD_i) or 'top4' (trades NAS100_i+XAGUSD_i & monitors/records US500_i+GER40_i)",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=[],
        help="Optional custom list of broker symbols (overrides --preset)",
    )
    parser.add_argument("--capital", type=float, default=1000.0, help="Starting paper balance per symbol USD (default 1000)")
    parser.add_argument(
        "--risk-mult",
        type=float,
        default=1.0,
        help="Risk-parity lot multiplier across symbols (default 1.0; e.g. 1.5 or 2.0)",
    )
    parser.add_argument(
        "--session-hours-only",
        action="store_true",
        help="Restrict entries to each symbol's primary exchange session hours (default: 24h with V5 HTF+freeze guards)",
    )
    parser.add_argument("--poll-seconds", type=float, default=0.50, help="Batch quote poll interval (default 0.50s)")
    parser.add_argument("--status-seconds", type=float, default=15.0, help="Dashboard print interval (default 15s)")
    parser.add_argument("--report-seconds", type=float, default=30.0, help="JSON snapshot interval (default 30s)")
    parser.add_argument("--minutes", type=float, default=0.0, help="Run duration in minutes (0 = until Ctrl+C)")
    parser.add_argument("--record", action="store_true", help="Simultaneously record throttled ticks to logs/s1_ticks_<SYM>_<DATE>.jsonl")
    parser.add_argument("--report-file", default="", help="Atomic JSON status path (defaults to reports/paper_live/multi_symbol_aw_v5_live.json)")
    parser.add_argument("--trades-log", default="", help="Closed trades JSONL path (defaults to logs/multi_symbol_aw_v5_trades.jsonl)")
    parser.add_argument("--logs-dir", default=str(ROOT / "logs"), help="Tick logs directory for --replay (default logs/)")
    parser.add_argument("--replay", action="store_true", help="Offline replay on recorded non-Gold tick logs (no MT5 needed)")
    parser.add_argument("--mock", action="store_true", help="Instant synthetic smoke test verifying V5 + teleport guard (no MT5 needed)")
    parser.add_argument("--verbose", action="store_true", help="Print OPEN/ADD/P50 events during --replay")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    if args.report_file:
        report_path = _ensure_in_reports_or_logs(Path(args.report_file))
    elif not args.mock and not args.replay:
        rm_sfx = "" if abs(args.risk_mult - 1.0) < 1e-6 else f"_r{args.risk_mult:.2g}x".replace(".", "p")
        report_path = _ensure_in_reports_or_logs(
            ROOT / "reports" / "paper_live" / f"multi_symbol_aw_v5_live{rm_sfx}.json"
        )
    else:
        report_path = None

    if args.mock:
        return _run_mock(args, report_path)
    if args.replay:
        return _run_replay(args, report_path)

    trades_log_path = _ensure_in_reports_or_logs(
        Path(args.trades_log) if args.trades_log else (ROOT / "logs" / "multi_symbol_aw_v5_trades.jsonl")
    )

    active_syms, watch_syms = resolve_symbol_plan(args)
    all_syms = active_syms + watch_syms
    specs = load_alpari_specs(ROOT / "reports" / "paper_live" / "alpari_wave_symbols_scan.json")
    ref_spreads = _load_reference_spreads()

    stop_requested = False

    def _handle_signal(_sig: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    load_dotenv(ROOT / ".env")
    cfg = Config.from_env()
    started_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    engines: dict[str, NormalizedAllWeatherV5] = {}
    metas: dict[str, dict[str, Any]] = {}
    m1_ctxs: dict[str, dict[str, Any]] = {s: {"bias": 0, "label": "WARMUP", "support": None, "resistance": None, "atr_m1": 0.0} for s in all_syms}
    last_quotes: dict[str, tuple[float, float]] = {}
    tick_counts: dict[str, int] = {s: 0 for s in all_syms}
    rec_counts: dict[str, int] = {s: 0 for s in all_syms}
    seeded_counts: dict[str, int] = {s: 0 for s in all_syms}
    last_rec_quote: dict[str, tuple[float, float] | None] = {s: None for s in all_syms}
    last_rec_change_ts: dict[str, float] = {s: 0.0 for s in all_syms}
    last_rec_write_ts: dict[str, float] = {s: 0.0 for s in all_syms}
    tick_fhs: dict[str, Any] = {}
    open_day = ""

    trades_fh = _open_private_append(trades_log_path)
    reconnect_count = 0
    consecutive_errors = 0
    start_mono = time.monotonic()
    last_status_mono = 0.0
    last_report_mono = 0.0
    last_m1_mono = 0.0
    clock_offset = 0.0

    def _rotate_tick_files(utc_day: str) -> None:
        nonlocal open_day
        if not args.record or utc_day == open_day:
            return
        for fh in tick_fhs.values():
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass
        tick_fhs.clear()
        for sym in all_syms:
            p = _daily_tick_path(sym, utc_day)
            tick_fhs[sym] = _open_private_append(p)
        open_day = utc_day
        print(f"[REC] Recording ticks for {len(all_syms)} symbols into logs/s1_ticks_*_{utc_day}.jsonl", flush=True)

    print("=" * 96)
    print(f"MULTI-SYMBOL ALL-WEATHER V5 LIVE PAPER  |  preset={args.preset}  |  risk={args.risk_mult:.2g}x  |  ORDERS=0")
    print(f"  PROVEN ACTIVE : {', '.join(active_syms)}")
    if watch_syms:
        print(f"  WATCH SHADOW  : {', '.join(watch_syms)}")
    print(f"  REPORT FILE   : {report_path}")
    print(f"  TRADES LOG    : {trades_log_path}")
    print("=" * 96, flush=True)

    try:
        while not stop_requested:
            feed: ReadOnlyNativeFeed | None = None
            try:
                with loopback_native():
                    feed = ReadOnlyNativeFeed(cfg.mcp_url, cfg.mcp_token, all_syms[0], timeout=20.0)
                    feed.client.connect()
                    srv_ts, srv_dt = _server_now(feed)
                    clock_offset = srv_ts - time.time()

                    # First-time engine calibration using live MT5 contract specs & 200 M1 bars
                    if not engines:
                        live_rows = _fetch_all_symbols_rows(feed, include_hidden=True, limit=1000)
                        specs = _override_specs_from_live_rows(specs, live_rows)
                        quotes0 = _fetch_batch_quotes(feed, all_syms)
                        for sym in all_syms:
                            q0 = quotes0.get(sym)
                            bid0 = float(q0["bid"]) if q0 else 0.0
                            ask0 = float(q0["ask"]) if q0 else 0.0
                            mid0 = 0.5 * (bid0 + ask0) if bid0 > 0 else 100.0
                            live_sp = max(0.0, ask0 - bid0) if bid0 > 0 else 0.0
                            ref_sp = ref_spreads.get(sym, live_sp if live_sp > 0 else 0.40)
                            spec = specs.get(sym, AlpariSymbolSpec(sym, "OTHER", 1.0, "USD", 2))
                            eng, meta = build_symbol_engine(
                                sym,
                                spec=spec,
                                ref_spread=ref_sp,
                                mid_price=mid0,
                                capital_usd=args.capital,
                                risk_mult=args.risk_mult,
                                session_hours_only=args.session_hours_only,
                            )
                            engines[sym] = eng
                            metas[sym] = meta
                            if bid0 > 0 and ask0 >= bid0:
                                last_quotes[sym] = (bid0, ask0)
                            try:
                                bars = _fetch_m1_bars_4h(feed, symbol=sym, now_label=srv_dt, limit=200)
                                m1_ctxs[sym] = analyze_scale_free_m1_context(bars, server_now_ts=srv_ts)
                                seeded_counts[sym] = eng.seed_m1_bars(bars, server_now_ts=srv_ts, spread=ref_sp)
                            except Exception as exc:
                                print(f"[WARN] {sym} M1 seed failed: {exc}", flush=True)
                            role = "PROVEN" if sym in active_syms else "WATCH"
                            print(
                                f"  [INIT {role:6s}] {sym:10s} | refSP={meta['ref_spread']} | S={meta['price_scale']} "
                                f"| lot={meta['vsnap_lot']:.2f} (~${meta['sl_risk_usd']:.2f}/SL) "
                                f"| seeded_M1={seeded_counts[sym]} | M1={m1_ctxs[sym]['label']}",
                                flush=True,
                            )
                        last_m1_mono = time.monotonic()

                    while not stop_requested:
                        loop_t0 = time.monotonic()
                        elapsed_min = (loop_t0 - start_mono) / 60.0
                        if args.minutes > 0.0 and elapsed_min >= args.minutes:
                            stop_requested = True
                            break

                        try:
                            if (loop_t0 - last_m1_mono) >= 20.0:
                                srv_ts, srv_dt = _server_now(feed)
                                clock_offset = srv_ts - time.time()
                                for sym in all_syms:
                                    try:
                                        bars = _fetch_m1_bars_4h(feed, symbol=sym, now_label=srv_dt, limit=200)
                                        m1_ctxs[sym] = analyze_scale_free_m1_context(bars, server_now_ts=srv_ts)
                                    except Exception:
                                        pass
                                last_m1_mono = loop_t0

                            quotes = _fetch_batch_quotes(feed, all_syms)
                            now_ts = time.time() + clock_offset
                            utc_day = datetime.now(timezone.utc).strftime("%Y%m%d")
                            _rotate_tick_files(utc_day)
                            consecutive_errors = 0

                            for sym in all_syms:
                                q = quotes.get(sym)
                                if not q:
                                    continue
                                bid_now, ask_now = float(q["bid"]), float(q["ask"])
                                if bid_now <= 0.0 or ask_now < bid_now:
                                    continue
                                last_quotes[sym] = (bid_now, ask_now)
                                tick_counts[sym] += 1

                                # Throttled tick recording (10s throttle after 30s of identical frozen quote)
                                if args.record and sym in tick_fhs:
                                    q_rec = (bid_now, ask_now)
                                    should_write = True
                                    if q_rec != last_rec_quote[sym]:
                                        last_rec_quote[sym] = q_rec
                                        last_rec_change_ts[sym] = now_ts
                                    elif (now_ts - last_rec_change_ts[sym]) >= 30.0 and (now_ts - last_rec_write_ts[sym]) < 10.0:
                                        should_write = False
                                    if should_write:
                                        tick_fhs[sym].write(_format_tick_line(now_ts, bid_now, ask_now, m1_ctxs[sym]))
                                        last_rec_write_ts[sym] = now_ts
                                        rec_counts[sym] += 1
                                        if rec_counts[sym] % 20 == 0:
                                            tick_fhs[sym].flush()

                                eng = engines.get(sym)
                                if eng is None:
                                    continue
                                role = "PROVEN" if sym in active_syms else "WATCH"
                                evs = eng.on_tick(now_ts, bid_now, ask_now, m1_ctxs[sym])
                                for ev in evs:
                                    _print_trade_event(sym, role, ev, ts=now_ts, sym_realized=eng.suite.realized_pnl_usd)
                                    if ev.get("type") == "CLOSE_ALL":
                                        w = dict(ev["wave"])
                                        w["role"] = role
                                        w["recorded_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                                        trades_fh.write(json.dumps(w, ensure_ascii=False) + "\n")
                                        trades_fh.flush()

                            if (loop_t0 - last_status_mono) >= args.status_seconds:
                                _print_dashboard(
                                    active_syms=active_syms,
                                    watch_syms=watch_syms,
                                    engines=engines,
                                    metas=metas,
                                    last_quotes=last_quotes,
                                    tick_counts=tick_counts,
                                    rec_counts=rec_counts,
                                    now_ts=now_ts,
                                    elapsed_min=elapsed_min,
                                    recording=args.record,
                                )
                                last_status_mono = loop_t0

                            if report_path is not None and (loop_t0 - last_report_mono) >= args.report_seconds:
                                snap = _build_snapshot(
                                    started_utc=started_utc,
                                    preset=args.preset,
                                    risk_mult=args.risk_mult,
                                    active_syms=active_syms,
                                    watch_syms=watch_syms,
                                    engines=engines,
                                    metas=metas,
                                    last_quotes=last_quotes,
                                    tick_counts=tick_counts,
                                    rec_counts=rec_counts,
                                    seeded_counts=seeded_counts,
                                    reconnect_count=reconnect_count,
                                    consecutive_errors=consecutive_errors,
                                    mode="live",
                                )
                                _write_atomic_json(report_path, snap)
                                last_report_mono = loop_t0

                        except Exception as exc:
                            consecutive_errors += 1
                            print(f"[WARN] poll error ({consecutive_errors}): {exc}", flush=True)
                            if consecutive_errors >= 5:
                                raise

                        sleep_left = args.poll_seconds - (time.monotonic() - loop_t0)
                        if sleep_left > 0.0:
                            time.sleep(sleep_left)

            except Exception as exc:
                if stop_requested:
                    break
                reconnect_count += 1
                print(f"[RECONNECT #{reconnect_count}] Feed error: {exc} — retrying in 3s...", flush=True)
                time.sleep(3.0)
            finally:
                if feed is not None:
                    with contextlib.suppress(Exception):
                        feed.client.close()
    finally:
        # Final atomic snapshot + clean file handles (never leave half-written JSON)
        now_ts = time.time() + clock_offset
        if report_path is not None and engines:
            try:
                snap = _build_snapshot(
                    started_utc=started_utc,
                    preset=args.preset,
                    risk_mult=args.risk_mult,
                    active_syms=active_syms,
                    watch_syms=watch_syms,
                    engines=engines,
                    metas=metas,
                    last_quotes=last_quotes,
                    tick_counts=tick_counts,
                    rec_counts=rec_counts,
                    seeded_counts=seeded_counts,
                    reconnect_count=reconnect_count,
                    consecutive_errors=consecutive_errors,
                    mode="live_stopped",
                )
                _write_atomic_json(report_path, snap)
            except Exception:
                pass
        for fh in tick_fhs.values():
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass
        try:
            trades_fh.flush()
            trades_fh.close()
        except Exception:
            pass
        if engines:
            _print_dashboard(
                active_syms=active_syms,
                watch_syms=watch_syms,
                engines=engines,
                metas=metas,
                last_quotes=last_quotes,
                tick_counts=tick_counts,
                rec_counts=rec_counts,
                now_ts=now_ts,
                elapsed_min=(time.monotonic() - start_mono) / 60.0,
                recording=args.record,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
