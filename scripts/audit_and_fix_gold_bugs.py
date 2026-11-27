#!/usr/bin/env python3
"""Forensic Bug-Fix & Full-Timeline Gold Audit Lab (OLD CHOP DAYS vs LAST NIGHT +$108 RALLY, ORDERS=0).

Diagnoses and causally tests concrete fixes for the exact 5 bugs exposed by live overnight paper trading:
  BUG 1: VSNAP6 counter-trend entries (shorting -$6 dips during a +$70 bull rally -> 4 straight SLs)
  BUG 2: VSNAP6 premature profit_floor_hit choke-out (be_arm=$1.50 / ratchet=$2.50 -> AvgWin $1.15 vs AvgLoss -$2.89)
  BUG 3: SHOCK10 buying exhausted vertical peaks + duplicating an already-open VSNAP6 spike trade
  BUG 4: ZSCORE_MR shorting flags inside a +$70 breakout (no 60m macro trend guard + oversized SL)
  BUG 5: Missing 24h trend-rider engine (MACRO_WAVE slept at night; CALM_M1/M3/M5 trend riders made +$44..+$169!)

CRITICAL DATA HYGIENE:
  Deduplicates overlapping XAUUSD_i tick recorder files into ONE clean, non-overlapping chronological
  timeline (split into continuous segments at >120s gaps) so NO night or day is ever double-counted!

Strictly read-only / paper-only (ORDERS=0).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trade_mcp.all_weather_paper import AllWeatherPaperSuite  # noqa: E402
from trade_mcp.calm_regime_engines import (  # noqa: E402
    CalmRegimeEngine,
    build_calm_regime_profile_config,
)
from trade_mcp.fast_snap_engines import FastSnapConfig, FastSnapEngine, fast_snap_profiles  # noqa: E402
from trade_mcp.quant_github_accel_lab import (  # noqa: E402
    TickRobustZScoreConfig,
    TickRobustZScoreEngine,
)
from trade_mcp.s1_dual_radar import make_synthetic_dual_session  # noqa: E402
from trade_mcp.s1_impulse_audit import PullbackDipEngine, load_tick_stream, pullback_dip_profiles  # noqa: E402
from trade_mcp.s1_velocity_surfer import (  # noqa: E402
    S1VelocitySurferEngine,
    _mini_m1_context,
    analyze_m1_sr_context,
    build_s1_profile_config,
)


def _iso_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def load_deduplicated_gold_segments(
    logs_dir: Path,
    *,
    gap_split_sec: float = 120.0,
    dedupe_sec: float = 0.22,
) -> tuple[list[list[tuple[float, float, float, dict[str, Any]]]], int]:
    """Merge ALL s1_ticks_XAUUSD*.jsonl files into ONE deduplicated chronological timeline split at >120s gaps."""
    files = sorted(logs_dir.glob("s1_ticks_XAUUSD*.jsonl"))
    raw_ticks: list[tuple[float, float, float]] = []
    for p in files:
        for ts, bid, ask, _ in load_tick_stream(p, rebuild_m1=False):
            raw_ticks.append((ts, bid, ask))
    if not raw_ticks:
        return [], 0

    raw_ticks.sort(key=lambda r: r[0])

    # Deduplicate overlapping recorders (~0.30s cadence -> keep 1 tick per >= dedupe_sec)
    deduped: list[tuple[float, float, float]] = []
    last_ts = -1e18
    for ts, bid, ask in raw_ticks:
        if (ts - last_ts) >= dedupe_sec:
            deduped.append((ts, bid, ask))
            last_ts = ts

    # Split into continuous segments at disconnects / overnight rollover gaps > gap_split_sec
    segments_raw: list[list[tuple[float, float, float]]] = []
    cur: list[tuple[float, float, float]] = []
    for row in deduped:
        if cur and (row[0] - cur[-1][0]) > gap_split_sec:
            segments_raw.append(cur)
            cur = [row]
        else:
            cur.append(row)
    if cur:
        segments_raw.append(cur)

    # Rebuild strictly causal M1 context per continuous segment
    segments: list[list[tuple[float, float, float, dict[str, Any]]]] = []
    for seg in segments_raw:
        synth_bars: list[list[float]] = []
        cur_min: dict[str, Any] | None = None
        last_min = -1
        cached_m1: dict[str, Any] = {"bias": 0, "label": "WARMING_UP", "support": None, "resistance": None}
        out_seg: list[tuple[float, float, float, dict[str, Any]]] = []
        for ts, bid, ask in seg:
            mid = 0.5 * (bid + ask)
            min_ep = int(ts // 60) * 60
            if cur_min is None:
                cur_min = {"ts": float(min_ep), "o": mid, "h": mid, "l": mid, "c": mid}
            elif min_ep > int(cur_min["ts"]):
                synth_bars.append([cur_min["ts"], cur_min["o"], cur_min["h"], cur_min["l"], cur_min["c"], 1.0])
                if len(synth_bars) > 200:
                    synth_bars = synth_bars[-200:]
                cur_min = {"ts": float(min_ep), "o": mid, "h": mid, "l": mid, "c": mid}
            else:
                cur_min["h"] = max(cur_min["h"], mid)
                cur_min["l"] = min(cur_min["l"], mid)
                cur_min["c"] = mid
            if min_ep != last_min:
                last_min = min_ep
                cached_m1 = analyze_m1_sr_context(synth_bars, server_now_ts=ts) if len(synth_bars) >= 25 else _mini_m1_context(synth_bars)
            out_seg.append((ts, bid, ask, cached_m1))
        segments.append(out_seg)

    return segments, len(files)


# ---------------------------------------------------------------------------
# Causal Higher-Timeframe (15m / 30m / 60m) Trend Context Tracker
# ---------------------------------------------------------------------------
@dataclass
class CausalMacroTrendTracker:
    """Tracks strictly causal 15m, 30m, and 60m directional moves from 1s downsampled mids."""

    _sec_hist: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=4000))
    _cur_sec: tuple[int, float] | None = None

    def update(self, ts: float, mid: float) -> tuple[float, float, float]:
        sec = int(ts)
        if self._cur_sec is None:
            self._cur_sec = (sec, mid)
        elif sec > self._cur_sec[0]:
            self._sec_hist.append(self._cur_sec)
            while self._sec_hist and (sec - self._sec_hist[0][0]) > 3700:
                self._sec_hist.popleft()
            self._cur_sec = (sec, mid)
        else:
            self._cur_sec = (sec, mid)

        def _mv(win: int) -> float:
            cut = sec - win
            for s, m in self._sec_hist:
                if s >= cut:
                    return mid - m
            return 0.0

        return _mv(900), _mv(1800), _mv(3600)


# ---------------------------------------------------------------------------
# Wrapper applying Bug Fixes #1/#2/#3/#4 causally WITHOUT mutating production yet
# ---------------------------------------------------------------------------
class GuardedFastSnap:
    """FastSnapEngine with causal HTF counter-trend block (Bug #1) & optional wider winner breathing (Bug #2)."""

    def __init__(
        self,
        cfg: FastSnapConfig,
        *,
        block_counter_15m: float = 0.0,
        block_counter_30m: float = 0.0,
        require_with_trend_15m: float = 0.0,
    ) -> None:
        self.eng = FastSnapEngine(cfg=cfg)
        self.block_counter_15m = block_counter_15m
        self.block_counter_30m = block_counter_30m
        self.require_with_trend_15m = require_with_trend_15m
        self.macro = CausalMacroTrendTracker()
        self.waves_closed: list[dict[str, Any]] = []

    def reset_segment(self) -> None:
        self.eng = FastSnapEngine(cfg=self.eng.cfg)
        self.macro = CausalMacroTrendTracker()

    def on_tick(self, ts: float, bid: float, ask: float, _m1: dict[str, Any]) -> None:
        m15, m30, _ = self.macro.update(ts, 0.5 * (bid + ask))
        had_pending = self.eng.pending is not None
        evs = self.eng.on_tick(ts=ts, bid=bid, ask=ask)
        # If a brand-new entry was just queued, apply strict causal HTF guard before fill!
        if (not had_pending) and self.eng.pending is not None and self.eng.pending.get("kind") == "entry":
            side = self.eng.pending["side"]
            blocked = (
                (
                    self.block_counter_15m > 0.0
                    and (
                        (side == "sell" and m15 >= self.block_counter_15m)
                        or (side == "buy" and m15 <= -self.block_counter_15m)
                    )
                )
                or (
                    self.block_counter_30m > 0.0
                    and (
                        (side == "sell" and m30 >= self.block_counter_30m)
                        or (side == "buy" and m30 <= -self.block_counter_30m)
                    )
                )
                or (
                    self.require_with_trend_15m > 0.0
                    and (
                        (side == "buy" and m15 < self.require_with_trend_15m)
                        or (side == "sell" and m15 > -self.require_with_trend_15m)
                    )
                )
            )
            if blocked:
                self.eng.pending = None
        for ev in evs:
            if ev.get("type") == "CLOSE_ALL":
                self.waves_closed.append(ev["wave"])

    def close_open(self, ts: float, bid: float, ask: float) -> None:
        w = self.eng.force_close_open(ts=ts, bid=bid, ask=ask, reason="eof_seg")
        if w is not None:
            self.waves_closed.append(w)


class GuardedZScoreMR:
    """TickRobustZScoreEngine with causal 30m/60m macro breakout guard (Bug #4)."""

    def __init__(
        self,
        cfg: TickRobustZScoreConfig,
        *,
        block_counter_30m: float = 10.0,
        block_counter_60m: float = 14.0,
    ) -> None:
        self.eng = TickRobustZScoreEngine(cfg=cfg)
        self.block_counter_30m = block_counter_30m
        self.block_counter_60m = block_counter_60m
        self.macro = CausalMacroTrendTracker()
        self.waves_closed: list[dict[str, Any]] = []

    def reset_segment(self) -> None:
        self.eng = TickRobustZScoreEngine(cfg=self.eng.cfg)
        self.macro = CausalMacroTrendTracker()

    def on_tick(self, ts: float, bid: float, ask: float, _m1: dict[str, Any]) -> None:
        _, m30, m60 = self.macro.update(ts, 0.5 * (bid + ask))
        was_flat = self.eng.basket is None
        evs = self.eng.on_tick(ts=ts, bid=bid, ask=ask)
        # If ZScore just opened on bar close against a huge 30m/60m breakout, cancel entry causally!
        if was_flat and self.eng.basket is not None:
            side = self.eng.basket["side"]
            if (
                (side == "sell" and (m30 >= self.block_counter_30m or m60 >= self.block_counter_60m))
                or (side == "buy" and (m30 <= -self.block_counter_30m or m60 <= -self.block_counter_60m))
            ):
                self.eng.basket = None
                return
        for ev in evs:
            if ev.get("type") == "CLOSE_ALL":
                self.waves_closed.append(ev["wave"])

    def close_open(self, ts: float, bid: float, ask: float) -> None:
        w = self.eng.force_close_open(ts=ts, bid=bid, ask=ask, reason="eof_seg")
        if w is not None:
            self.waves_closed.append(w)


class GuardedCalmTrend:
    """CalmRegimeEngine with optional causal 30m macro trend alignment filter (blocks chop fakeouts!)."""

    def __init__(self, cfg: Any, *, min_30m_align: float = 0.0) -> None:
        self.eng = CalmRegimeEngine(cfg=cfg)
        self.min_30m_align = float(min_30m_align)
        self.macro = CausalMacroTrendTracker()
        self.waves_closed: list[dict[str, Any]] = []

    def reset_segment(self) -> None:
        self.eng = CalmRegimeEngine(cfg=self.eng.cfg)
        self.macro = CausalMacroTrendTracker()

    def on_tick(self, ts: float, bid: float, ask: float, m1: dict[str, Any]) -> None:
        _, m30, _ = self.macro.update(ts, 0.5 * (bid + ask))
        had_pending = self.eng.pending is not None
        evs = self.eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1)
        if (
            self.min_30m_align > 0.0
            and (not had_pending)
            and self.eng.pending is not None
            and self.eng.pending.get("kind") == "open"
        ):
            side = self.eng.pending["side"]
            if (side == "buy" and m30 < self.min_30m_align) or (side == "sell" and m30 > -self.min_30m_align):
                self.eng.pending = None
        for ev in evs:
            if ev.get("type") == "CLOSE_ALL":
                self.waves_closed.append(ev["wave"])

    def close_open(self, ts: float, bid: float, ask: float) -> None:
        if self.eng.basket is not None:
            ex_px = bid if self.eng.basket["side"] == "buy" else ask
            w = self.eng._close(ex_px, ts, "eof_seg")
            if w is not None:
                self.waves_closed.append(w)


class CoordinatedAWV4:
    """AW_V4 portfolio with Bug #3 Fix: blocks SHOCK10 duplicate entry when VSNAP6 is already open."""

    def __init__(
        self,
        *,
        shock_factory: Any,
        vsnap_factory: Any,
        macro_factory: Any,
        zscore_factory: Any,
        trend_factory: Any | None = None,
    ) -> None:
        self._factories = (shock_factory, vsnap_factory, macro_factory, zscore_factory, trend_factory)
        self.waves_closed: list[dict[str, Any]] = []
        self._init_subs()

    def _init_subs(self) -> None:
        sf, vf, mf, zf, tf = self._factories
        self.shock = sf()
        self.vsnap = vf()
        self.macro = mf()
        self.zscore = zf()
        self.trend = tf() if tf is not None else None

    def reset_segment(self) -> None:
        self._init_subs()

    def on_tick(self, ts: float, bid: float, ask: float, m1: dict[str, Any]) -> None:
        # 1) Run VSNAP6 first so coordination lock sees live VSNAP basket state
        for sub in (self.vsnap, self.macro, self.zscore, *([self.trend] if self.trend is not None else [])):
            prev = len(sub.waves_closed)
            sub.on_tick(ts, bid, ask, m1)
            if len(sub.waves_closed) > prev:
                self.waves_closed.extend(sub.waves_closed[prev:])

        # 2) Run SHOCK10 with Bug #3 coordination lock (cancel fresh pending open if VSNAP6 is already in a spike!)
        had_pend = self.shock.eng.pending_order is not None
        prev_s = len(self.shock.waves_closed)
        self.shock.on_tick(ts, bid, ask, m1)
        if (
            (not had_pend)
            and self.shock.eng.pending_order is not None
            and self.shock.eng.pending_order.get("kind") == "open"
            and self.vsnap.eng.basket is not None
        ):
            self.shock.eng.pending_order = None
        if len(self.shock.waves_closed) > prev_s:
            self.waves_closed.extend(self.shock.waves_closed[prev_s:])

    def close_open(self, ts: float, bid: float, ask: float) -> None:
        subs = [self.shock, self.vsnap, self.macro, self.zscore]
        if self.trend is not None:
            subs.append(self.trend)
        for sub in subs:
            prev = len(sub.waves_closed)
            sub.close_open(ts, bid, ask)
            if len(sub.waves_closed) > prev:
                self.waves_closed.extend(sub.waves_closed[prev:])


class PortfolioWrapper:
    """Combines multiple causal sub-engines cleanly into one account."""

    def __init__(self, builders: list[Any]) -> None:
        self._builders = builders
        self._subs: list[Any] = [b() for b in builders]
        self.waves_closed: list[dict[str, Any]] = []

    def reset_segment(self) -> None:
        self._subs = [b() for b in self._builders]

    def on_tick(self, ts: float, bid: float, ask: float, m1: dict[str, Any]) -> None:
        for s in self._subs:
            prev = len(s.waves_closed)
            s.on_tick(ts, bid, ask, m1)
            if len(s.waves_closed) > prev:
                self.waves_closed.extend(s.waves_closed[prev:])

    def close_open(self, ts: float, bid: float, ask: float) -> None:
        for s in self._subs:
            prev = len(s.waves_closed)
            s.close_open(ts, bid, ask)
            if len(s.waves_closed) > prev:
                self.waves_closed.extend(s.waves_closed[prev:])


class SimpleAdapter:
    """Uniform adapter for CalmRegimeEngine, PullbackDipEngine, S1VelocitySurferEngine, AllWeatherPaperSuite."""

    def __init__(self, factory: Any, kind: str) -> None:
        self._factory = factory
        self._kind = kind
        self.eng = factory()
        self.waves_closed: list[dict[str, Any]] = []

    def reset_segment(self) -> None:
        if self._kind == "suite":
            self.eng.reset_file_state()
        else:
            self.eng = self._factory()

    def on_tick(self, ts: float, bid: float, ask: float, m1: dict[str, Any]) -> None:
        if self._kind == "suite":
            prev = len(self.eng.trades_closed)
            self.eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1)
            if len(self.eng.trades_closed) > prev:
                self.waves_closed.extend(self.eng.trades_closed[prev:])
        elif self._kind == "m1":
            evs = self.eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1)
            for ev in evs:
                if ev.get("type") == "CLOSE_ALL":
                    self.waves_closed.append(ev["wave"])
        else:
            evs = self.eng.on_tick(ts=ts, bid=bid, ask=ask)
            for ev in evs:
                if ev.get("type") == "CLOSE_ALL":
                    self.waves_closed.append(ev["wave"])

    def close_open(self, ts: float, bid: float, ask: float) -> None:
        if self._kind == "suite":
            prev = len(self.eng.trades_closed)
            self.eng.close_all_open(ts=ts, bid=bid, ask=ask, reason="eof_seg")
            if len(self.eng.trades_closed) > prev:
                self.waves_closed.extend(self.eng.trades_closed[prev:])
        elif hasattr(self.eng, "close_open_wave"):
            w = self.eng.close_open_wave(ts=ts, bid=bid, ask=ask, reason="eof_seg")
            if w is not None:
                self.waves_closed.append(w)
        elif hasattr(self.eng, "force_close_open"):
            w = self.eng.force_close_open(ts=ts, bid=bid, ask=ask, reason="eof_seg")
            if w is not None:
                self.waves_closed.append(w)
        elif getattr(self.eng, "basket", None) is not None and hasattr(self.eng, "_close"):
            ex_px = bid if self.eng.basket["side"] == "buy" else ask
            w = self.eng._close(ex_px, ts, "eof_seg")
            if w is not None:
                self.waves_closed.append(w)


def _stats(waves: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [float(w.get("net_pnl_usd", 0.0)) for w in waves]
    net = round(sum(pnls), 2)
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    gw = sum(wins)
    gl = abs(sum(losses))
    pf = round(gw / gl, 2) if gl > 0 else (99.0 if gw > 0 else 0.0)
    bal = 0.0
    peak = 0.0
    dd = 0.0
    for x in pnls:
        bal += x
        if bal > peak:
            peak = bal
        dd = max(dd, peak - bal)
    rdd = round(net / dd, 2) if dd > 0 else (99.0 if net > 0 else 0.0)
    avg_w = round(gw / len(wins), 2) if wins else 0.0
    avg_l = round(-gl / len(losses), 2) if losses else 0.0
    return {
        "n": len(pnls),
        "w": len(wins),
        "l": len(losses),
        "net": net,
        "dd": round(dd, 2),
        "rdd": rdd,
        "pf": pf,
        "avg_w": avg_w,
        "avg_l": avg_l,
    }


def build_candidates() -> list[tuple[str, Any]]:
    vsnap_base = replace(dict(fast_snap_profiles(base_lot=0.02))["VSNAP_SHOCK_6"], name="VSNAP6_24H", active_server_hours=None)
    # Bug #2 Fix: Wider winner breathing room so waves run +$5..+$25 instead of choking at +$1.15!
    vsnap_breathe = replace(
        vsnap_base,
        name="VSNAP6_BREATHE",
        be_arm_price=2.40,
        be_lock_profit=0.25,
        ratchet_arm_price=4.00,
        ratchet_frac=0.52,
        ratchet_breathing_room=2.10,
        trail_arm_price=5.20,
        trail_pullback_price=2.80,
        max_hold_sec=480.0,
    )
    vsnap_breathe_wide = replace(
        vsnap_base,
        name="VSNAP6_WIDE",
        be_arm_price=2.80,
        be_lock_profit=0.30,
        ratchet_arm_price=4.80,
        ratchet_frac=0.50,
        ratchet_breathing_room=2.50,
        trail_arm_price=6.50,
        trail_pullback_price=3.20,
        max_hold_sec=600.0,
    )

    z_base = TickRobustZScoreConfig(
        name="ZSCORE_MR_24H",
        bar_sec=60,
        entry_z=2.2,
        adx_max=22.0,
        min_sd_cost_mult=2.5,
        min_tp_cost_mult=3.5,
        min_sl_price=4.50,
        max_sl_price=7.50,
        leg_lot=0.03,
        variable_lot_by_z=True,
        max_lot=0.06,
        night_adaptive=True,
        night_entry_z=2.15,
        night_adx_max=24.0,
        night_sd_cost_mult=2.2,
        night_tp_cost_mult=3.2,
        active_server_hours=None,
    )
    z_tight_sl = replace(z_base, name="ZSCORE_MR_SAFE", min_sl_price=3.20, max_sl_price=4.80)

    macro_sess_cfg = dict(pullback_dip_profiles(leg_lot=0.01, max_legs=4))["MACRO_WAVE_DYN"]
    shock_cfg = build_s1_profile_config("v2.6_SHOCK10_DYN", leg_lot=0.01, max_legs=6)

    def _aw_v3() -> Any:
        return SimpleAdapter(lambda: AllWeatherPaperSuite(), "suite")

    def _shock10() -> Any:
        return SimpleAdapter(lambda: S1VelocitySurferEngine(cfg=shock_cfg), "m1")

    def _macro_sess() -> Any:
        return SimpleAdapter(lambda: PullbackDipEngine(cfg=macro_sess_cfg), "m1")

    def _z_guard() -> Any:
        return GuardedZScoreMR(z_base, block_counter_30m=9.0, block_counter_60m=12.0)

    def _z_guard_tight() -> Any:
        return GuardedZScoreMR(z_tight_sl, block_counter_30m=9.0, block_counter_60m=12.0)

    def _vsnap_htf() -> Any:
        return GuardedFastSnap(vsnap_base, block_counter_15m=5.0, block_counter_30m=8.0)

    def _vsnap_htf_breathe() -> Any:
        return GuardedFastSnap(vsnap_breathe, block_counter_15m=5.0, block_counter_30m=8.0)

    def _vsnap_htf_wide() -> Any:
        return GuardedFastSnap(vsnap_breathe_wide, block_counter_15m=5.0, block_counter_30m=8.0)

    def _calm(name: str) -> Any:
        return SimpleAdapter(
            lambda: CalmRegimeEngine(cfg=build_calm_regime_profile_config(name, leg_lot=0.02, max_legs=8)),
            "m1",
        )

    return [
        # Baseline shipped AW_V3_24H + all 4 solo components
        ("AW_V3_SHIPPED_BASE", _aw_v3()),
        ("SHOCK10_ORIG", _shock10()),
        ("MACRO_SESS_ORIG", _macro_sess()),
        ("VSNAP6_ORIG", GuardedFastSnap(vsnap_base)),
        ("VSNAP6_FIX1_HTF", _vsnap_htf()),
        ("VSNAP6_FIX12_BREATHE", _vsnap_htf_breathe()),
        ("VSNAP6_FIX12_WIDE", _vsnap_htf_wide()),
        ("ZSCORE_ORIG", GuardedZScoreMR(z_base, block_counter_30m=999.0, block_counter_60m=999.0)),
        ("ZSCORE_FIX4_GUARD", _z_guard()),
        ("ZSCORE_FIX4_TIGHTSL", _z_guard_tight()),
        # 24H Multi-Minute Trend-Riders (M1 / M3 / M5) across ALL days (Chop + $108 Rally!)
        ("CALM_M1_EMA_24H", _calm("CALM_M1_EMA_24H")),
        ("CALM_M3_SWING_24H", _calm("CALM_M3_SWING_24H")),
        ("CALM_M5_TREND_24H", _calm("CALM_M5_TREND_24H")),
        ("CALM_M5_RR_24H", _calm("CALM_M5_RR_24H")),
        ("CALM_M1_PRO_SESS", _calm("CALM_M1_PRO_SESS")),
        # Combined AW_V4 candidates!
        ("AW_V4_FIX124_ONLY", PortfolioWrapper([_shock10, _vsnap_htf_breathe, _macro_sess, _z_guard])),
        ("AW_V4_FIX124_WIDE", PortfolioWrapper([_shock10, _vsnap_htf_wide, _macro_sess, _z_guard_tight])),
        ("AW_V4_PLUS_M5_TREND", PortfolioWrapper([_shock10, _vsnap_htf_breathe, _macro_sess, _z_guard, lambda: _calm("CALM_M5_TREND_24H")])),
        ("AW_V4_PLUS_M5_RR", PortfolioWrapper([_shock10, _vsnap_htf_breathe, _macro_sess, _z_guard, lambda: _calm("CALM_M5_RR_24H")])),
        ("AW_V4_PLUS_M1_EMA", PortfolioWrapper([_shock10, _vsnap_htf_breathe, _macro_sess, _z_guard, lambda: _calm("CALM_M1_EMA_24H")])),
        ("AW_V4_TREND_ONLY_M5", PortfolioWrapper([_shock10, _macro_sess, _z_guard, lambda: _calm("CALM_M5_TREND_24H")])),
        ("AW_V4_TREND_ONLY_M5RR", PortfolioWrapper([_shock10, _macro_sess, _z_guard, lambda: _calm("CALM_M5_RR_24H")])),
    ]


def build_phase2_candidates() -> list[tuple[str, Any]]:
    vsnap_base = replace(dict(fast_snap_profiles(base_lot=0.02))["VSNAP_SHOCK_6"], name="VSNAP6_24H", active_server_hours=None)
    vsnap_wide = replace(
        vsnap_base,
        name="VSNAP6_WIDE",
        be_arm_price=2.80,
        be_lock_profit=0.30,
        ratchet_arm_price=4.80,
        ratchet_frac=0.50,
        ratchet_breathing_room=2.50,
        trail_arm_price=6.50,
        trail_pullback_price=3.20,
        max_hold_sec=600.0,
    )
    # Parameter-stability neighbors around VSNAP6_WIDE (confirm smooth plateau, NOT an isolated lucky point!)
    vsnap_wide_nb1 = replace(
        vsnap_wide,
        name="VSNAP6_WIDE_NB1",
        be_arm_price=2.60,
        ratchet_arm_price=4.50,
        ratchet_breathing_room=2.30,
        trail_arm_price=6.00,
        trail_pullback_price=3.00,
    )
    vsnap_wide_nb2 = replace(
        vsnap_wide,
        name="VSNAP6_WIDE_NB2",
        be_arm_price=3.00,
        ratchet_arm_price=5.20,
        ratchet_breathing_room=2.70,
        trail_arm_price=7.00,
        trail_pullback_price=3.40,
    )

    z_tight = TickRobustZScoreConfig(
        name="ZSCORE_MR_SAFE",
        bar_sec=60,
        entry_z=2.2,
        adx_max=22.0,
        min_sd_cost_mult=2.5,
        min_tp_cost_mult=3.5,
        min_sl_price=3.20,
        max_sl_price=4.80,
        leg_lot=0.03,
        variable_lot_by_z=True,
        max_lot=0.06,
        night_adaptive=True,
        night_entry_z=2.15,
        night_adx_max=24.0,
        night_sd_cost_mult=2.2,
        night_tp_cost_mult=3.2,
        active_server_hours=None,
    )
    macro_cfg = dict(pullback_dip_profiles(leg_lot=0.01, max_legs=4))["MACRO_WAVE_DYN"]
    shock_cfg = build_s1_profile_config("v2.6_SHOCK10_DYN", leg_lot=0.01, max_legs=6)
    m5_rr_base = build_calm_regime_profile_config("CALM_M5_RR_24H", leg_lot=0.02, max_legs=8)

    m5_e35_cfg = replace(
        m5_rr_base,
        leg_lot=0.01,
        dyn_max_leg_lot=0.06,
        dyn_max_total_lot=0.25,
        min_atr_price=2.20,
        min_trend_sep_atr=0.35,
        min_trend_eff=0.35,
    )
    m5_e42_cfg = replace(
        m5_rr_base,
        leg_lot=0.01,
        dyn_max_leg_lot=0.06,
        dyn_max_total_lot=0.25,
        min_atr_price=2.50,
        min_trend_sep_atr=0.42,
        min_trend_eff=0.42,
    )

    def _aw_v3() -> Any:
        return SimpleAdapter(lambda: AllWeatherPaperSuite(), "suite")

    def _shock10() -> Any:
        return SimpleAdapter(lambda: S1VelocitySurferEngine(cfg=shock_cfg), "m1")

    def _macro_sess() -> Any:
        return SimpleAdapter(lambda: PullbackDipEngine(cfg=macro_cfg), "m1")

    def _z_safe() -> Any:
        return GuardedZScoreMR(z_tight, block_counter_30m=9.0, block_counter_60m=12.0)

    def _v_wide() -> Any:
        return GuardedFastSnap(vsnap_wide, block_counter_15m=5.0, block_counter_30m=8.0)

    def _v_nb1() -> Any:
        return GuardedFastSnap(vsnap_wide_nb1, block_counter_15m=5.0, block_counter_30m=8.0)

    def _v_nb2() -> Any:
        return GuardedFastSnap(vsnap_wide_nb2, block_counter_15m=5.0, block_counter_30m=8.0)

    return [
        ("AW_V3_SHIPPED_BASE", _aw_v3()),
        ("VSNAP6_WIDE_BASE", _v_wide()),
        ("VSNAP6_WIDE_NB1", _v_nb1()),
        ("VSNAP6_WIDE_NB2", _v_nb2()),
        ("M5_RR_GATED_E35", GuardedCalmTrend(m5_e35_cfg, min_30m_align=0.0)),
        ("M5_RR_GATED_E42", GuardedCalmTrend(m5_e42_cfg, min_30m_align=0.0)),
        ("M5_RR_GATED_M30_8", GuardedCalmTrend(m5_e35_cfg, min_30m_align=8.0)),
        ("M5_RR_GATED_M30_12", GuardedCalmTrend(m5_e35_cfg, min_30m_align=12.0)),
        ("AW_V4_FIX124_WIDE", PortfolioWrapper([_shock10, _v_wide, _macro_sess, _z_safe])),
        ("AW_V4_FIX1234_COORD", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_wide, macro_factory=_macro_sess, zscore_factory=_z_safe)),
        ("AW_V4_COORD_M5_E35", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_wide, macro_factory=_macro_sess, zscore_factory=_z_safe, trend_factory=lambda: GuardedCalmTrend(m5_e35_cfg, min_30m_align=0.0))),
        ("AW_V4_COORD_M5_E42", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_wide, macro_factory=_macro_sess, zscore_factory=_z_safe, trend_factory=lambda: GuardedCalmTrend(m5_e42_cfg, min_30m_align=0.0))),
        ("AW_V4_COORD_M5_M30_8", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_wide, macro_factory=_macro_sess, zscore_factory=_z_safe, trend_factory=lambda: GuardedCalmTrend(m5_e35_cfg, min_30m_align=8.0))),
        ("AW_V4_COORD_M5_M30_12", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_wide, macro_factory=_macro_sess, zscore_factory=_z_safe, trend_factory=lambda: GuardedCalmTrend(m5_e35_cfg, min_30m_align=12.0))),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deduplicated Full-History Gold Bug-Fix Audit (ORDERS=0)")
    parser.add_argument("--logs-dir", default="logs")
    parser.add_argument("--phase2", action="store_true", help="Run Phase-2 stability, Bug #3 coordination lock & gated M5 trend rider")
    parser.add_argument("--mock", action="store_true", help="Run fast synthetic smoke test")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    if args.mock:
        segs = [make_synthetic_dual_session()]
        n_files = 1
        overnight_cut = segs[0][len(segs[0]) // 2][0]
    else:
        logs_dir = Path(args.logs_dir) if Path(args.logs_dir).is_absolute() else (ROOT / args.logs_dir)
        segs, n_files = load_deduplicated_gold_segments(logs_dir)
        if not segs:
            print("No s1_ticks_XAUUSD*.jsonl files found.", file=sys.stderr)
            return 2
        # Split strictly at 2026-11-26 19:35 SRV (start of last night's live session & +$108 rally)
        overnight_cut = datetime(2026, 11, 26, 19, 35, 0, tzinfo=timezone.utc).timestamp()

    total_ticks = sum(len(s) for s in segs)
    old_ticks = sum(sum(1 for r in s if r[0] < overnight_cut) for s in segs)
    new_ticks = total_ticks - old_ticks
    print(
        f"=== GOLD FULL DEDUPLICATED BUG-FIX AUDIT ({n_files} FILES -> {len(segs)} CLEAN SEGMENTS, "
        f"{total_ticks} UNIQUE TICKS | OLD_CHOP={old_ticks}t | LAST_NIGHT_RALLY={new_ticks}t | ORDERS=0) ==="
    )
    for idx, s in enumerate(segs, 1):
        mids = [0.5 * (b + a) for _, b, a, _ in s]
        print(f"  Seg#{idx:02d}: {_iso_dt(s[0][0])} -> {_iso_dt(s[-1][0])} | {len(s):6d}t | {min(mids):.2f}..{max(mids):.2f} (rng=${max(mids) - min(mids):.2f})")

    cands = build_phase2_candidates() if args.phase2 else build_candidates()
    print("-" * 124)
    print(
        f"{'MODEL':22s} | {'ALL_NET':>8s} {'ALL_DD':>7s} {'R/DD':>5s} {'W/L':>7s} {'PF':>5s} {'AvgW/AvgL':>12s} "
        f"| {'OLD_CHOP':>8s} {'OLD_DD':>7s} | {'LAST_NGT':>8s} {'NGT_DD':>7s} {'NGT_WL':>6s}"
    )
    print("-" * 124)

    for name, model in cands:
        for seg_idx, seg in enumerate(segs):
            if seg_idx > 0:
                model.reset_segment()
            for ts, bid, ask, m1_ctx in seg:
                model.on_tick(ts, bid, ask, m1_ctx)
            if seg:
                model.close_open(seg[-1][0], seg[-1][1], seg[-1][2])

        all_w = model.waves_closed
        old_w = [w for w in all_w if float(w.get("opened_at", w.get("closed_at", 0.0))) < overnight_cut]
        new_w = [w for w in all_w if float(w.get("opened_at", w.get("closed_at", 0.0))) >= overnight_cut]
        sa = _stats(all_w)
        so = _stats(old_w)
        sn = _stats(new_w)
        print(
            f"{name:22s} | ${sa['net']:+7.2f} -${sa['dd']:5.2f} {sa['rdd']:5.2f} {sa['w']:2d}W/{sa['l']:<2d}L {sa['pf']:5.2f} "
            f"{sa['avg_w']:+5.2f}/{sa['avg_l']:+5.2f} "
            f"| ${so['net']:+7.2f} -${so['dd']:5.2f} | ${sn['net']:+7.2f} -${sn['dd']:5.2f} {sn['w']:2d}W/{sn['l']:<2d}L"
        )
    print("=" * 124)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
