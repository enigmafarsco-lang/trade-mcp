#!/usr/bin/env python3
"""Causal Structural-Risk A/B Audit Lab — Gold (Multi-Day IS vs OOS NY) + Cross-Market (NAS100_i) (ORDERS=0).

Strictly read-only / paper-only diagnostic tool (`ORDERS=0`). Does NOT modify production defaults
or touch any running live paper session.

Tests ONLY clean, first-principles structural risk rules (ZERO per-day curve-fitting):
  A0_PROD_V4           : Exact shipped COMBO_AW_V4_24H (Control)
  B1_FIX_L1            : Fixed 1x Leg-1 lot on unproven initial entries (VSNAP6 + MACRO_WAVE);
                         Leg 2+ house-money winner pyramiding remains 100% enabled.
  B2_HTF60_ONLY        : Causal 60m macro counter-trend guard (VSNAP6 60m=$10, MACRO_WAVE 30m=$8 & 60m=$10);
                         Leg-1 dynamic sizing unchanged.
  B3_FIX_L1+HTF60      : Combined B1 + B2 (Fixed 1x Leg-1 + 60m=$10 HTF guard).
  B4_FIX_L1+HTF_TIGHT  : Sensitivity neighbor (Fixed 1x Leg-1 + 60m=$8 & 120m=$12) — anti-overfit check.
  B5_FIX_L1+HTF_WIDE   : Sensitivity neighbor (Fixed 1x Leg-1 + 60m=$12 & 120m=$16; matches ZSCORE_MR 60m=$12).

Evaluates:
  1. Gold (`XAUUSD_i`) day-by-day IN-SAMPLE (`11-25`, `11-26_DAY`, `11-27_AM`) vs OUT-OF-SAMPLE (`11-27_NY`).
  2. Side-by-side trade forensics on `11-27_NY` (`A0_PROD_V4` vs `B3_FIX_L1+HTF60`) at exact live risk multiplier.
  3. Independent cross-market out-of-sample check on `NAS100_i` (`V4_GUARD_24H` and `V4_GUARD_VOL`) using the
     EXACT SAME Gold-space structural rules (zero symbol-specific tuning).
  4. Objective 3-gate scientific anti-overfit verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from audit_and_fix_gold_bugs import load_deduplicated_gold_segments  # noqa: E402
from audit_multi_symbol_waves import (  # noqa: E402
    analyze_symbol_microstructure,
    discover_recorded_symbol_files,
    load_deduplicated_symbol_segments,
)

from trade_mcp.all_weather_paper import AllWeatherPaperSuite  # noqa: E402
from trade_mcp.multi_symbol_aw_v4 import (  # noqa: E402
    GOLD_REF_SL_RISK_USD,
    AlpariSymbolSpec,
    NormalizedAllWeatherV4,
    compute_risk_parity_lot_scale,
    load_alpari_specs,
)
from trade_mcp.s1_dual_radar import make_synthetic_dual_session  # noqa: E402

DEFAULT_SPECS_CACHE = ROOT / "reports" / "paper_live" / "alpari_wave_symbols_scan.json"


@dataclass(frozen=True)
class StructuralVariant:
    name: str
    desc: str
    fix_l1: bool = False
    vsnap_60m: float = 0.0
    vsnap_120m: float = 0.0
    macro_30m: float = 0.0
    macro_60m: float = 0.0
    macro_120m: float = 0.0


VARIANTS: tuple[StructuralVariant, ...] = (
    StructuralVariant(
        name="A0_PROD_V4",
        desc="Shipped COMBO_AW_V4_24H control (dynamic L1 up to 2.5x, VSNAP 15m/30m, MACRO no HTF)",
    ),
    StructuralVariant(
        name="B1_FIX_L1",
        desc="Fixed 1x Leg-1 only (VSNAP6 + MACRO_WAVE dyn_max_l1_lot=leg_lot; Leg 2+ adds kept)",
        fix_l1=True,
    ),
    StructuralVariant(
        name="B2_HTF60_ONLY",
        desc="60m HTF guard only (VSNAP 60m=10, MACRO 30m=8 & 60m=10; dynamic L1 unchanged)",
        vsnap_60m=10.0,
        macro_30m=8.0,
        macro_60m=10.0,
    ),
    StructuralVariant(
        name="B3_FIX_L1+HTF60",
        desc="Combined Fix-L1 + 60m=10 HTF guard (VSNAP 60m=10, MACRO 30m=8 & 60m=10)",
        fix_l1=True,
        vsnap_60m=10.0,
        macro_30m=8.0,
        macro_60m=10.0,
    ),
    StructuralVariant(
        name="B4_FIX_L1+HTF_TIGHT",
        desc="Sensitivity neighbor: Fix-L1 + tighter 60m=8 / 120m=12 HTF guard",
        fix_l1=True,
        vsnap_60m=8.0,
        vsnap_120m=12.0,
        macro_30m=6.0,
        macro_60m=8.0,
        macro_120m=12.0,
    ),
    StructuralVariant(
        name="B5_FIX_L1+HTF_WIDE",
        desc="Sensitivity neighbor: Fix-L1 + wider 60m=12 / 120m=16 HTF guard (matches ZSCORE 60m=12)",
        fix_l1=True,
        vsnap_60m=12.0,
        vsnap_120m=16.0,
        macro_30m=8.0,
        macro_60m=12.0,
        macro_120m=16.0,
    ),
)


def apply_structural_variant(suite: AllWeatherPaperSuite, variant: StructuralVariant) -> None:
    """Apply clean structural risk overrides to an AllWeatherPaperSuite instance (works for Gold & normalized symbols)."""
    if suite.fast_shock_engine is not None:
        fcfg = suite.fast_shock_engine.cfg
        if variant.fix_l1:
            fcfg.dyn_max_l1_lot = float(fcfg.leg_lot)
        fcfg.block_counter_60m = float(variant.vsnap_60m)
        fcfg.block_counter_120m = float(variant.vsnap_120m)
    mcfg = suite.macro_engine.cfg
    if variant.fix_l1:
        mcfg.dyn_max_l1_lot = float(mcfg.leg_lot)
    mcfg.block_counter_30m = float(variant.macro_30m)
    mcfg.block_counter_60m = float(variant.macro_60m)
    mcfg.block_counter_120m = float(variant.macro_120m)


def _fmt_usd(val: float, dec: int = 2) -> str:
    sign = "+" if val >= 0 else "-"
    return f"{sign}${abs(val):.{dec}f}"


def _fmt_dd(val: float) -> str:
    return f"-${abs(val):.2f}"


def _iso_hms(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M:%S")


def _parse_oos_ts(oos_str: str) -> float:
    dt = datetime.strptime(oos_str.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _gold_bucket(ts: float, *, oos_start_ts: float, is_mock: bool = False) -> str:
    if is_mock:
        return "MOCK_RUN"
    if ts >= oos_start_ts:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        if dt.strftime("%Y-%m-%d") == "2026-11-27":
            return "11-27_NY"
        return dt.strftime("%Y-%m-%d")
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if dt >= datetime(2026, 11, 26, 19, 35, 0, tzinfo=timezone.utc):
        return "11-27_AM"
    day_s = dt.strftime("%Y-%m-%d")
    if day_s == "2026-11-26":
        return "11-26_DAY"
    if day_s == "2026-11-25":
        return "11-25"
    return day_s


def _stats(waves: list[dict[str, Any]]) -> dict[str, Any]:
    if not waves:
        return {"n": 0, "w": 0, "l": 0, "net": 0.0, "dd": 0.0, "rdd": 0.0, "pf": 0.0, "wr": 0.0, "avg_l": 0.0}
    ordered = sorted(waves, key=lambda w: float(w.get("closed_at", 0.0)))
    eq = 0.0
    pk = 0.0
    dd = 0.0
    gw = 0.0
    gl = 0.0
    wins = 0
    for w in ordered:
        p = float(w.get("net_pnl_usd", 0.0))
        eq += p
        if eq > pk:
            pk = eq
        if (pk - eq) > dd:
            dd = pk - eq
        if p > 0:
            wins += 1
            gw += p
        else:
            gl += abs(p)
    n = len(ordered)
    losses = n - wins
    pf = (gw / gl) if gl > 1e-9 else (99.0 if gw > 0 else 0.0)
    rdd = (eq / dd) if dd > 1e-9 else (99.0 if eq > 0 else 0.0)
    return {
        "n": n,
        "w": wins,
        "l": losses,
        "net": round(eq, 2),
        "dd": round(dd, 2),
        "rdd": round(min(rdd, 99.0), 2),
        "pf": round(min(pf, 99.0), 2),
        "wr": round(100.0 * wins / n, 1),
        "avg_l": round(-gl / losses, 2) if losses else 0.0,
    }


def run_gold_variant(
    segments: list[list[tuple[float, float, float, dict[str, Any]]]],
    variant: StructuralVariant,
    *,
    risk_multiplier: float = 2.0,
) -> dict[str, Any]:
    """Run AllWeatherPaperSuite(v4) + structural variant over deduplicated Gold segments."""
    suite = AllWeatherPaperSuite(
        initial_capital_usd=1000.0,
        engine_version="v4",
        risk_multiplier=float(risk_multiplier),
    )
    apply_structural_variant(suite, variant)
    htf_blocked = 0
    for idx, seg in enumerate(segments):
        if idx > 0:
            if suite.fast_shock_engine is not None:
                htf_blocked += int(suite.fast_shock_engine.signals_blocked_htf)
            htf_blocked += int(suite.macro_engine.signals_blocked_htf)
            suite.reset_file_state()
            apply_structural_variant(suite, variant)
        if not seg:
            continue
        for ts, bid, ask, m1_ctx in seg:
            suite.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
        ts_e, b_e, a_e, _ = seg[-1]
        suite.close_all_open(ts=ts_e, bid=b_e, ask=a_e, reason="eof_segment")
    if suite.fast_shock_engine is not None:
        htf_blocked += int(suite.fast_shock_engine.signals_blocked_htf)
    htf_blocked += int(suite.macro_engine.signals_blocked_htf)
    waves: list[dict[str, Any]] = []
    for w in suite.trades_closed:
        rec = dict(w)
        lbl = str(rec.get("engine_label", rec.get("engine", "OTHER")))
        rec["_engine"] = {"FAST_SHOCK6": "VSNAP6", "NEWS_SHOCK": "SHOCK10"}.get(lbl, lbl)
        waves.append(rec)
    return {
        "variant": variant.name,
        "waves": waves,
        "htf_blocked": htf_blocked,
        "stats": _stats(waves),
    }


def run_symbol_variant(
    symbol: str,
    spec: AlpariSymbolSpec,
    micro: dict[str, Any],
    segments: list[list[tuple[float, float, float, dict[str, Any]]]],
    variant: StructuralVariant,
    *,
    price_scale: float,
) -> dict[str, Any]:
    """Run NormalizedAllWeatherV4 + exact same Gold-space structural variant on a non-Gold symbol."""
    lot_scale, real_lot, real_sl_usd, _ = compute_risk_parity_lot_scale(
        price_scale=price_scale,
        usd_per_price_unit=micro["usd_per_unit"],
        target_vsnap_sl_usd=GOLD_REF_SL_RISK_USD,
        volume_min=spec.volume_min,
        volume_step=spec.volume_step,
    )
    eng = NormalizedAllWeatherV4(
        symbol=symbol,
        price_scale=price_scale,
        usd_per_price_unit=micro["usd_per_unit"],
        lot_scale=lot_scale,
        digits=spec.digits,
        active_hours_srv=None,
        allow_partial_split=True,
        stale_freeze_sec=300.0,
        max_single_tick_jump_norm=4.50,
        teleport_cooldown_sec=90.0,
        suite_mutator=lambda s: apply_structural_variant(s, variant),
    )
    htf_blocked = 0
    for idx, seg in enumerate(segments):
        if idx > 0:
            if eng.suite.fast_shock_engine is not None:
                htf_blocked += int(eng.suite.fast_shock_engine.signals_blocked_htf)
            htf_blocked += int(eng.suite.macro_engine.signals_blocked_htf)
            eng.reset_segment()
        if not seg:
            continue
        for ts, bid, ask, m1_ctx in seg:
            eng.on_tick(ts, bid, ask, m1_ctx)
        ts_e, b_e, a_e, _ = seg[-1]
        eng.close_open(ts_e, b_e, a_e, reason="eof_segment")
    if eng.suite.fast_shock_engine is not None:
        htf_blocked += int(eng.suite.fast_shock_engine.signals_blocked_htf)
    htf_blocked += int(eng.suite.macro_engine.signals_blocked_htf)
    return {
        "variant": variant.name,
        "price_scale": round(price_scale, 4),
        "real_lot": real_lot,
        "real_sl_usd": real_sl_usd,
        "waves": list(eng.waves_closed),
        "htf_blocked": htf_blocked,
        "stats": _stats(eng.waves_closed),
    }


def _make_mock_gold_segments() -> list[list[tuple[float, float, float, dict[str, Any]]]]:
    return [make_synthetic_dual_session()]


def _print_trade_list(title: str, waves: list[dict[str, Any]]) -> None:
    print(f"\n  [{title}] ({len(waves)} trades)")
    if not waves:
        print("    (0 trades)")
        return
    cum = 0.0
    for i, w in enumerate(sorted(waves, key=lambda x: float(x.get("opened_at", 0.0))), 1):
        p = float(w.get("net_pnl_usd", 0.0))
        cum += p
        eng = str(w.get("_engine", w.get("engine_label", "OTHER")))
        side = str(w.get("side", "")).upper()
        vol = float(w.get("total_volume", 0.0))
        ent = float(w.get("avg_entry", 0.0))
        ex = float(w.get("exit_price", 0.0))
        rsn = str(w.get("exit_reason", ""))[:22]
        t0 = _iso_hms(float(w.get("opened_at", 0.0)))
        t1 = _iso_hms(float(w.get("closed_at", 0.0))).split()[-1]
        print(
            f"    {i:>2} {t0}->{t1} {eng:<10} {side:<4} {vol:>5.2f}L "
            f"{ent:>8.2f}->{ex:<8.2f} {rsn:<22} {_fmt_usd(p):>9} (cum {_fmt_usd(cum):>9})"
        )


def main(argv: list[str] | None = None) -> int:
    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    ap = argparse.ArgumentParser(description="Causal Structural-Risk A/B Audit Lab (ORDERS=0)")
    ap.add_argument("--logs-dir", type=Path, default=ROOT / "logs")
    ap.add_argument("--risk-mult", type=float, default=2.0, help="Gold risk multiplier (default 2.0 to match live account)")
    ap.add_argument("--oos-start", default="2026-11-27 14:24", help="Out-of-sample NY session start (SRV YYYY-MM-DD HH:MM)")
    ap.add_argument("--extra-symbols", nargs="*", default=["NAS100_i"], help="Cross-market validation symbols")
    ap.add_argument("--specs-cache", type=Path, default=DEFAULT_SPECS_CACHE)
    ap.add_argument("--mock", action="store_true", help="Run fast synthetic self-check")
    ap.add_argument("--save", action="store_true", help="Save JSON report to reports/paper_live/ab_structural_risk.json")
    args = ap.parse_args(argv)

    oos_start_ts = _parse_oos_ts(args.oos_start)
    if args.mock:
        gold_segments = _make_mock_gold_segments()
        gold_total_ticks = sum(len(s) for s in gold_segments)
    else:
        gold_segments, gold_total_ticks = load_deduplicated_gold_segments(args.logs_dir)
        if not gold_segments:
            print(f"ERROR: No XAUUSD tick files found in {args.logs_dir}")
            return 1

    gold_results: dict[str, dict[str, Any]] = {}
    all_buckets_set: set[str] = set()
    for v in VARIANTS:
        res = run_gold_variant(gold_segments, v, risk_multiplier=args.risk_mult)
        by_b: dict[str, list[dict[str, Any]]] = {}
        for w in res["waves"]:
            b = _gold_bucket(float(w.get("opened_at", 0.0)), oos_start_ts=oos_start_ts, is_mock=args.mock)
            by_b.setdefault(b, []).append(w)
            all_buckets_set.add(b)
        res["by_bucket"] = {b: _stats(ws) for b, ws in by_b.items()}
        res["waves_by_bucket"] = by_b
        is_waves = [w for b, ws in by_b.items() if b != "11-27_NY" for w in ws]
        oos_waves = list(by_b.get("11-27_NY", []))
        res["is_stats"] = _stats(is_waves)
        res["oos_stats"] = _stats(oos_waves)
        gold_results[v.name] = res

    preferred_order = ["11-25", "11-26_DAY", "11-27_AM", "11-27_NY", "MOCK_RUN"]
    buckets = [b for b in preferred_order if b in all_buckets_set] + sorted(all_buckets_set - set(preferred_order))

    print("=" * 150)
    print(
        f"=== STRUCTURAL RISK A/B AUDIT — GOLD ({gold_total_ticks:,} TICKS, {len(gold_segments)} SEGS, "
        f"RISK={args.risk_mult:.1f}x, OOS >= {args.oos_start} SRV) | ORDERS=0 ==="
    )
    print("=" * 150)

    print("SECTION 1 — GOLD (XAUUSD_i) DAY-BY-DAY A/B MATRIX (IN-SAMPLE vs OUT-OF-SAMPLE TODAY NY):")
    hdr = f"{'VARIANT':<21} {'BLK':>4} | {'ALL_NET':>9} {'ALL_DD':>8} {'R/DD':>5} {'W/L':>7} {'PF':>5} {'AvgL':>7} |"
    for b in buckets:
        tag = f"{b}*" if b == "11-27_NY" else b
        hdr += f" {tag:>15}"
    hdr += f" | {'IS_NET(R/DD)':>15} {'OOS_NY(W/L)':>15}"
    print(hdr)
    print("-" * len(hdr))
    for v in VARIANTS:
        r = gold_results[v.name]
        st = r["stats"]
        line = (
            f"{v.name:<21} {r['htf_blocked']:>4} | "
            f"{_fmt_usd(st['net']):>9} {_fmt_dd(st['dd']):>8} {st['rdd']:>5.2f} "
            f"{st['w']:>2}W/{st['l']:<2}L {st['pf']:>5.2f} {_fmt_usd(st['avg_l']):>7} |"
        )
        for b in buckets:
            bs = r["by_bucket"].get(b, {"net": 0.0, "w": 0, "l": 0})
            cell = f"{_fmt_usd(bs['net'], 1)}({bs['w']}/{bs['l']})"
            line += f" {cell:>15}"
        ist = r["is_stats"]
        ost = r["oos_stats"]
        is_cell = f"{_fmt_usd(ist['net'], 1)}({ist['rdd']:.1f})"
        oos_cell = f"{_fmt_usd(ost['net'], 1)}({ost['w']}/{ost['l']})"
        line += f" | {is_cell:>15} {oos_cell:>15}"
        print(line)
    print("  (* = 11-27_NY is TODAY'S live out-of-sample NY session; BLK = causal HTF counter-trend blocks)")

    # Side-by-side BUY vs SELL attribution on TODAY NY (11-27_NY)
    if not args.mock and "11-27_NY" in all_buckets_set:
        print("\nSECTION 2 — TODAY'S LIVE NY SESSION (11-27_NY >= " + args.oos_start + " SRV) FORENSIC SPLIT:")
        print(f"  {'VARIANT':<21} | {'TOTAL_NY':>10} {'DD_NY':>8} {'W/L':>7} | {'SELLS (with-trend)':>22} | {'BUYS (counter-trend)':>22}")
        print("  " + "-" * 102)
        for v in VARIANTS:
            ws = gold_results[v.name]["waves_by_bucket"].get("11-27_NY", [])
            st = _stats(ws)
            sells = _stats([w for w in ws if w.get("side") == "sell"])
            buys = _stats([w for w in ws if w.get("side") == "buy"])
            print(
                f"  {v.name:<21} | {_fmt_usd(st['net']):>10} {_fmt_dd(st['dd']):>8} {st['w']:>2}W/{st['l']:<2}L | "
                f"{_fmt_usd(sells['net']):>9} ({sells['w']}W/{sells['l']}L) | "
                f"{_fmt_usd(buys['net']):>9} ({buys['w']}W/{buys['l']}L)"
            )
        _print_trade_list(
            f"A0_PROD_V4 on 11-27_NY ({args.risk_mult:.1f}x)",
            gold_results["A0_PROD_V4"]["waves_by_bucket"].get("11-27_NY", []),
        )
        _print_trade_list(
            f"B3_FIX_L1+HTF60 on 11-27_NY ({args.risk_mult:.1f}x)",
            gold_results["B3_FIX_L1+HTF60"]["waves_by_bucket"].get("11-27_NY", []),
        )

    # SECTION 3: Cross-market validation on NAS100_i (and any extra requested symbols)
    cross_results: dict[str, dict[str, Any]] = {}
    if not args.mock and args.extra_symbols:
        specs = load_alpari_specs(args.specs_cache)
        recorded_by_sym = discover_recorded_symbol_files(args.logs_dir)
        print("\n" + "=" * 150)
        print("SECTION 3 — CROSS-MARKET OUT-OF-SAMPLE CHECK (EXACT SAME GOLD-SPACE RULES, ZERO SYMBOL TUNING):")
        print("=" * 150)
        for sym in args.extra_symbols:
            spec = specs.get(sym)
            if spec is None:
                print(f"  [{sym}] skipped — spec missing in {args.specs_cache}")
                continue
            sym_files = recorded_by_sym.get(sym, [])
            if not sym_files:
                print(f"  [{sym}] skipped — no tick files found in {args.logs_dir}")
                continue
            segs, fmeta = load_deduplicated_symbol_segments(sym_files, freeze_split_sec=300.0)
            if not segs:
                print(f"  [{sym}] skipped — no active segments in {args.logs_dir}")
                continue
            micro = analyze_symbol_microstructure(sym, spec, segs)
            scale_modes = [
                ("V4_GUARD_24H", micro["s_spread"]),
                ("V4_GUARD_VOL", micro["s_vol"]),
            ]
            cross_results[sym] = {}
            print(
                f"\n  [{sym}] live_ticks={fmeta['active_ticks']:,} ({fmeta['live_hrs']:.1f}h active / "
                f"{fmeta['frozen_hrs']:.1f}h frozen stripped) | MED_SP={micro['med_sp']:.2f} | S_SP={micro['s_spread']:.2f} | S_VOL={micro['s_vol']:.2f}"
            )
            print(f"  {'SCALE_MODE':<13} {'VARIANT':<21} {'BLK':>4} | {'NET':>9} {'DD':>8} {'R/DD':>5} {'W/L':>7} {'PF':>5} {'AvgL':>7}")
            print("  " + "-" * 94)
            for mode_name, s_val in scale_modes:
                cross_results[sym][mode_name] = {}
                for v in VARIANTS:
                    sr = run_symbol_variant(sym, spec, micro, segs, v, price_scale=s_val)
                    st = sr["stats"]
                    cross_results[sym][mode_name][v.name] = {
                        "htf_blocked": sr["htf_blocked"],
                        "stats": st,
                    }
                    print(
                        f"  {mode_name:<13} {v.name:<21} {sr['htf_blocked']:>4} | "
                        f"{_fmt_usd(st['net']):>9} {_fmt_dd(st['dd']):>8} {st['rdd']:>5.2f} "
                        f"{st['w']:>2}W/{st['l']:<2}L {st['pf']:>5.2f} {_fmt_usd(st['avg_l']):>7}"
                    )

    # SECTION 4: Objective 3-gate Anti-Overfit Verdict
    a0 = gold_results["A0_PROD_V4"]
    b3 = gold_results["B3_FIX_L1+HTF60"]
    b4 = gold_results["B4_FIX_L1+HTF_TIGHT"]
    b5 = gold_results["B5_FIX_L1+HTF_WIDE"]

    gate1_oos = b3["oos_stats"]["net"] > a0["oos_stats"]["net"]
    gate2_is = (b3["is_stats"]["rdd"] >= a0["is_stats"]["rdd"] * 0.90) and (b3["stats"]["rdd"] >= a0["stats"]["rdd"])
    gate3_neighbors = (
        b4["oos_stats"]["net"] > a0["oos_stats"]["net"]
        and b5["oos_stats"]["net"] > a0["oos_stats"]["net"]
        and b4["stats"]["net"] > 0
        and b5["stats"]["net"] > 0
    )
    nas_ok = True
    nas_note = "N/A"
    if "NAS100_i" in cross_results and "V4_GUARD_24H" in cross_results["NAS100_i"]:
        n_a0 = cross_results["NAS100_i"]["V4_GUARD_24H"]["A0_PROD_V4"]["stats"]
        n_b3 = cross_results["NAS100_i"]["V4_GUARD_24H"]["B3_FIX_L1+HTF60"]["stats"]
        nas_ok = (n_b3["net"] >= n_a0["net"] - 2.0) and (n_b3["dd"] <= n_a0["dd"] + 2.0)
        nas_note = f"A0 {_fmt_usd(n_a0['net'])} (DD -${n_a0['dd']:.2f}) -> B3 {_fmt_usd(n_b3['net'])} (DD -${n_b3['dd']:.2f})"

    print("\n" + "=" * 150)
    print("SECTION 4 — SCIENTIFIC ANTI-OVERFIT VERDICT (A0_PROD_V4 vs B3_FIX_L1+HTF60):")
    print(
        f"  GATE 1 (OOS 11-27_NY improvement):     {'PASS' if gate1_oos else 'FAIL'}  "
        f"(A0 {_fmt_usd(a0['oos_stats']['net'])} -> B3 {_fmt_usd(b3['oos_stats']['net'])})"
    )
    print(
        f"  GATE 2 (In-Sample & Total R/DD intact): {'PASS' if gate2_is else 'FAIL'}  "
        f"(IS R/DD {a0['is_stats']['rdd']:.2f} -> {b3['is_stats']['rdd']:.2f} | "
        f"ALL R/DD {a0['stats']['rdd']:.2f} -> {b3['stats']['rdd']:.2f})"
    )
    print(
        f"  GATE 3 (Neighborhood stability B4/B5):  {'PASS' if gate3_neighbors else 'FAIL'}  "
        f"(OOS B4={_fmt_usd(b4['oos_stats']['net'])}, B3={_fmt_usd(b3['oos_stats']['net'])}, B5={_fmt_usd(b5['oos_stats']['net'])})"
    )
    print(f"  CROSS-MARKET CHECK (NAS100_i 24H):      {'PASS' if nas_ok else 'CAUTION'}  ({nas_note})")
    print("=" * 150)

    if args.save:
        out_path = ROOT / "reports" / "paper_live" / "ab_structural_risk.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "paper_only": True,
            "orders_submitted": 0,
            "risk_mult": args.risk_mult,
            "oos_start": args.oos_start,
            "gold_ticks": gold_total_ticks,
            "gold_segments": len(gold_segments),
            "gold": {
                k: {
                    "htf_blocked": v["htf_blocked"],
                    "stats": v["stats"],
                    "is_stats": v["is_stats"],
                    "oos_stats": v["oos_stats"],
                    "by_bucket": v["by_bucket"],
                }
                for k, v in gold_results.items()
            },
            "cross_market": cross_results,
            "gates": {
                "gate1_oos": gate1_oos,
                "gate2_is": gate2_is,
                "gate3_neighbors": gate3_neighbors,
                "nas_ok": nas_ok,
            },
        }
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"SAVED_AB_REPORT={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
