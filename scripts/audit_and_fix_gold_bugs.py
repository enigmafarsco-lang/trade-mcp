#!/usr/bin/env python3
"""Forensic Bug-Fix & Day-by-Day Full-Timeline Gold Audit Lab (ORDERS=0).

Diagnoses and causally tests concrete fixes for all 5 bugs exposed by live overnight paper trading:
  BUG 1: VSNAP6 counter-trend entries (shorting -$6 dips during a +$70 bull rally -> 4 straight SLs)
  BUG 2: VSNAP6 premature profit_floor_hit choke-out (be_arm=$1.50 / ratchet=$2.50 / add breathing=$1.15!)
  BUG 3: SHOCK10 buying exhausted vertical peaks + duplicating an already-open VSNAP6 spike trade
  BUG 4: ZSCORE_MR shorting flags inside a +$70 breakout (no 60m macro trend guard + oversized SL)
  BUG 5: Missing 24h trend-rider / conditional overnight MACRO_WAVE during smooth +$108 overnight rallies

CRITICAL DATA HYGIENE & TRANSPARENCY:
  1. Deduplicates overlapping XAUUSD_i tick recorder files into ONE clean chronological timeline.
  2. Reports EXACT day-by-day & session-by-session PnL (e.g., 2026-11-24, 2026-11-25, 2026-11-26_DAY,
     LAST_NIGHT >= 2026-11-26 19:35 SRV) — NEVER opaque lumped numbers!
  3. Prints per-engine x per-day reconciliation matrices AND a complete trade-by-trade LAST_NIGHT log.

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
from trade_mcp.s1_impulse_audit import (  # noqa: E402
    ALPARI_LONDON_NY_HOURS,
    PullbackDipConfig,
    PullbackDipEngine,
    load_tick_stream,
    pullback_dip_profiles,
)
from trade_mcp.s1_velocity_surfer import (  # noqa: E402
    S1VelocitySurferEngine,
    _mini_m1_context,
    analyze_m1_sr_context,
    build_s1_profile_config,
)


def _iso_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _iso_hms(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M:%S")


def _fmt_usd(val: float, dec: int = 2) -> str:
    sign = "+" if val >= 0 else "-"
    return f"{sign}${abs(val):.{dec}f}"


def _norm_engine_label(raw: str) -> str:
    up = str(raw or "").upper()
    if "SHOCK10" in up or "NEWS_SHOCK" in up:
        return "SHOCK10"
    if "VSNAP" in up or "FAST_SHOCK" in up:
        return "VSNAP6"
    if "MACRO" in up or "PULLBACK" in up:
        return "MACRO_WAVE"
    if "ZSCORE" in up:
        return "ZSCORE_MR"
    if "DONCHIAN" in up or "DC_TREND" in up:
        return "DC_TREND"
    if "CALM" in up or "M5" in up or "M1_" in up or "M3_" in up or "TREND" in up:
        return "M5_TREND"
    return up or "OTHER"


def _tag_wave(w: dict[str, Any], engine_label: str) -> dict[str, Any]:
    out = dict(w)
    out["_engine"] = _norm_engine_label(engine_label)
    return out


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
                cached_m1 = (
                    analyze_m1_sr_context(synth_bars, server_now_ts=ts)
                    if len(synth_bars) >= 25
                    else _mini_m1_context(synth_bars)
                )
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
# Causal Bug-Fix Wrappers (non-invasive until verified on full history)
# ---------------------------------------------------------------------------
class GuardedFastSnap:
    """FastSnapEngine with causal HTF counter-trend block (Bug #1) & wider winner/add breathing (Bug #2)."""

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
                self.waves_closed.append(_tag_wave(ev["wave"], "VSNAP6"))

    def close_open(self, ts: float, bid: float, ask: float) -> None:
        w = self.eng.force_close_open(ts=ts, bid=bid, ask=ask, reason="eof_seg")
        if w is not None:
            self.waves_closed.append(_tag_wave(w, "VSNAP6"))


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
                self.waves_closed.append(_tag_wave(ev["wave"], "ZSCORE_MR"))

    def close_open(self, ts: float, bid: float, ask: float) -> None:
        w = self.eng.force_close_open(ts=ts, bid=bid, ask=ask, reason="eof_seg")
        if w is not None:
            self.waves_closed.append(_tag_wave(w, "ZSCORE_MR"))


class GuardedMacroWave24H:
    """MACRO_WAVE_DYN active in London/NY, and conditionally wakes up off-hours ONLY on real 30m macro trends (Bug #5A)."""

    def __init__(
        self,
        cfg: PullbackDipConfig,
        *,
        primary_hours: tuple[int, ...] = ALPARI_LONDON_NY_HOURS,
        night_min_30m_move: float = 10.0,
    ) -> None:
        self.cfg_24h = replace(cfg, active_server_hours=None)
        self.primary_hours = set(primary_hours)
        self.night_min_30m_move = float(night_min_30m_move)
        self.eng = PullbackDipEngine(cfg=self.cfg_24h)
        self.macro = CausalMacroTrendTracker()
        self.waves_closed: list[dict[str, Any]] = []

    def reset_segment(self) -> None:
        self.eng = PullbackDipEngine(cfg=self.cfg_24h)
        self.macro = CausalMacroTrendTracker()

    def on_tick(self, ts: float, bid: float, ask: float, m1: dict[str, Any]) -> None:
        _, m30, _ = self.macro.update(ts, 0.5 * (bid + ask))
        had_pending = self.eng.pending is not None
        evs = self.eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1)
        if (not had_pending) and self.eng.pending is not None and self.eng.pending.get("kind") == "open":
            srv_hour = (int(ts) // 3600) % 24
            if srv_hour not in self.primary_hours:
                side = self.eng.pending["side"]
                aligned = (side == "buy" and m30 >= self.night_min_30m_move) or (
                    side == "sell" and m30 <= -self.night_min_30m_move
                )
                if not aligned:
                    self.eng.pending = None
        for ev in evs:
            if ev.get("type") == "CLOSE_ALL":
                self.waves_closed.append(_tag_wave(ev["wave"], "MACRO_WAVE"))

    def close_open(self, ts: float, bid: float, ask: float) -> None:
        if self.eng.basket is not None:
            ex_px = bid if self.eng.basket["side"] == "buy" else ask
            w = self.eng._close(ex_px, ts, "eof_seg")
            if w is not None:
                self.waves_closed.append(_tag_wave(w, "MACRO_WAVE"))


class GuardedCalmTrend:
    """CalmRegimeEngine with optional causal 30m/60m macro trend alignment filter (Bug #5B: sleeps in chop, rides rallies!)."""

    def __init__(
        self,
        cfg: Any,
        *,
        min_30m_align: float = 0.0,
        min_60m_align: float = 0.0,
    ) -> None:
        self.eng = CalmRegimeEngine(cfg=cfg)
        self.min_30m_align = float(min_30m_align)
        self.min_60m_align = float(min_60m_align)
        self.macro = CausalMacroTrendTracker()
        self.waves_closed: list[dict[str, Any]] = []

    def reset_segment(self) -> None:
        self.eng = CalmRegimeEngine(cfg=self.eng.cfg)
        self.macro = CausalMacroTrendTracker()

    def on_tick(self, ts: float, bid: float, ask: float, m1: dict[str, Any]) -> None:
        _, m30, m60 = self.macro.update(ts, 0.5 * (bid + ask))
        had_pending = self.eng.pending is not None
        evs = self.eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1)
        if (
            (self.min_30m_align > 0.0 or self.min_60m_align > 0.0)
            and (not had_pending)
            and self.eng.pending is not None
            and self.eng.pending.get("kind") == "open"
        ):
            side = self.eng.pending["side"]
            bad_30 = self.min_30m_align > 0.0 and (
                (side == "buy" and m30 < self.min_30m_align) or (side == "sell" and m30 > -self.min_30m_align)
            )
            bad_60 = self.min_60m_align > 0.0 and (
                (side == "buy" and m60 < self.min_60m_align) or (side == "sell" and m60 > -self.min_60m_align)
            )
            if bad_30 or bad_60:
                self.eng.pending = None
        for ev in evs:
            if ev.get("type") == "CLOSE_ALL":
                self.waves_closed.append(_tag_wave(ev["wave"], "M5_TREND"))

    def close_open(self, ts: float, bid: float, ask: float) -> None:
        if self.eng.basket is not None:
            ex_px = bid if self.eng.basket["side"] == "buy" else ask
            w = self.eng._close(ex_px, ts, "eof_seg")
            if w is not None:
                self.waves_closed.append(_tag_wave(w, "M5_TREND"))


@dataclass
class DonchianTrendConfig:
    """Classic institutional Donchian box-breakout + Chandelier ATR trailing trend-rider (strictly causal, ORDERS=0)."""

    bar_sec: int = 300  # M5 bars
    donchian_bars: int = 12  # 12 x M5 = 60-minute structural High/Low breakout
    fast_ema: int = 8
    slow_ema: int = 21
    atr_period: int = 12
    leg_lot: float = 0.02
    max_legs: int = 3
    multiplier: float = 100.0
    latency_sec: float = 0.25
    max_spread_price: float = 0.85
    min_atr_price: float = 2.00
    max_atr_price: float = 12.00
    min_trend_eff: float = 0.35
    min_body_frac: float = 0.35
    sl_atr_mult: float = 2.20
    min_stop_price: float = 5.00
    max_stop_price: float = 11.00
    be_arm_min_price: float = 6.00
    be_arm_atr_mult: float = 2.00
    trail_arm_min_price: float = 8.00
    trail_arm_atr_mult: float = 2.50
    trail_atr_mult: float = 2.40
    trail_min_dist: float = 6.00
    trail_max_dist: float = 13.00
    enable_adds: bool = True
    add_step_min_price: float = 8.00
    add_step_atr_mult: float = 2.50
    add_interval_sec: float = 300.0
    add_breathing_room: float = 4.50
    max_hold_sec: float = 14400.0
    win_cooldown_sec: float = 300.0
    loss_cooldown_sec: float = 900.0
    consec_loss_cooldown_sec: float = 1800.0


class DonchianTrendRider:
    """Strictly causal tick-fed M5 Donchian breakout + Chandelier ATR trend-rider (zero lookahead)."""

    def __init__(self, cfg: DonchianTrendConfig | None = None) -> None:
        self.cfg = cfg or DonchianTrendConfig()
        self.waves_closed: list[dict[str, Any]] = []
        self._init_state()

    def _init_state(self) -> None:
        self._bars: deque[dict[str, float]] = deque(maxlen=120)
        self._cur_bar: dict[str, float] | None = None
        self._prev_ts: float | None = None
        self._ema_fast: float | None = None
        self._ema_slow: float | None = None
        self._atr: float | None = None
        self.pending: dict[str, Any] | None = None
        self.basket: dict[str, Any] | None = None
        self.cooldown_until: float = 0.0
        self.consecutive_losses: int = 0
        self.next_wave_id: int = 1

    def reset_segment(self) -> None:
        self._init_state()

    def _push_bar(self, bar: dict[str, float]) -> None:
        prev_c = self._bars[-1]["c"] if self._bars else bar["o"]
        self._bars.append(bar)
        c = bar["c"]
        tr = max(bar["h"] - bar["l"], abs(bar["h"] - prev_c), abs(bar["l"] - prev_c))
        kf = 2.0 / (self.cfg.fast_ema + 1.0)
        ks = 2.0 / (self.cfg.slow_ema + 1.0)
        if self._ema_fast is None:
            self._ema_fast = c
            self._ema_slow = c
            self._atr = max(tr, 0.5)
        else:
            assert self._ema_slow is not None and self._atr is not None
            self._ema_fast = kf * c + (1.0 - kf) * self._ema_fast
            self._ema_slow = ks * c + (1.0 - ks) * self._ema_slow
            a = 1.0 / max(1, self.cfg.atr_period)
            self._atr = (1.0 - a) * self._atr + a * tr

    def _trend_eff(self, n_bars: int = 8) -> float:
        if len(self._bars) < n_bars + 1:
            return 0.0
        rec = list(self._bars)[-(n_bars + 1) :]
        net = abs(rec[-1]["c"] - rec[0]["c"])
        path = sum(abs(rec[i]["c"] - rec[i - 1]["c"]) for i in range(1, len(rec)))
        return (net / path) if path > 1e-9 else 0.0

    def _close(self, ex_px: float, ts: float, reason: str) -> None:
        assert self.basket is not None
        b = self.basket
        self.basket = None
        self.pending = None
        diff = (ex_px - b["avg_entry"]) if b["side"] == "buy" else (b["avg_entry"] - ex_px)
        pnl = round(diff * b["total_volume"] * self.cfg.multiplier, 4)
        w = {
            "wave_id": b["wave_id"],
            "side": b["side"],
            "legs_count": len(b["legs"]),
            "total_volume": b["total_volume"],
            "avg_entry": b["avg_entry"],
            "exit_price": round(ex_px, 4),
            "stop_final": b["stop"],
            "opened_at": b["opened_at"],
            "closed_at": float(ts),
            "hold_seconds": round(max(0.0, ts - b["opened_at"]), 2),
            "net_pnl_usd": pnl,
            "exit_reason": reason,
        }
        self.waves_closed.append(_tag_wave(w, "DC_TREND"))
        if pnl > 0:
            self.consecutive_losses = 0
            self.cooldown_until = ts + self.cfg.win_cooldown_sec
        else:
            self.consecutive_losses += 1
            cd = self.cfg.consec_loss_cooldown_sec if self.consecutive_losses >= 2 else self.cfg.loss_cooldown_sec
            self.cooldown_until = ts + cd

    def on_tick(self, ts: float, bid: float, ask: float, _m1: dict[str, Any]) -> None:
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return
        mid = 0.5 * (bid + ask)
        spread = ask - bid

        max_gap = max(180.0, float(self.cfg.bar_sec) * 3.0)
        if self._prev_ts is not None and (ts - self._prev_ts) > max_gap:
            self._bars.clear()
            self._cur_bar = None
            self._ema_fast = None
            self._ema_slow = None
            self._atr = None
            self.pending = None
        self._prev_ts = ts

        bar_id = int(ts // self.cfg.bar_sec)
        just_closed: dict[str, float] | None = None
        if self._cur_bar is None:
            self._cur_bar = {"bar_id": float(bar_id), "o": mid, "h": mid, "l": mid, "c": mid}
        elif bar_id > int(self._cur_bar["bar_id"]):
            just_closed = dict(self._cur_bar)
            self._push_bar(just_closed)
            self._cur_bar = {"bar_id": float(bar_id), "o": mid, "h": mid, "l": mid, "c": mid}
        else:
            self._cur_bar["h"] = max(self._cur_bar["h"], mid)
            self._cur_bar["l"] = min(self._cur_bar["l"], mid)
            self._cur_bar["c"] = mid

        # 1) Execute causal pending fill
        if self.pending is not None and ts >= self.pending["fill_ts"]:
            po = self.pending
            self.pending = None
            if spread <= self.cfg.max_spread_price:
                side = po["side"]
                fill_px = ask if side == "buy" else bid
                if po["kind"] == "open" and self.basket is None:
                    sl_dist = float(po["sl_dist"])
                    stop = round((fill_px - sl_dist) if side == "buy" else (fill_px + sl_dist), 4)
                    lot = round(self.cfg.leg_lot, 2)
                    self.basket = {
                        "wave_id": self.next_wave_id,
                        "side": side,
                        "legs": [{"entry": round(fill_px, 4), "vol": lot, "ts": ts}],
                        "total_volume": lot,
                        "avg_entry": round(fill_px, 4),
                        "stop": stop,
                        "stop_locked": False,
                        "opened_at": ts,
                        "last_add_ts": ts,
                        "best_px": bid if side == "buy" else ask,
                    }
                    self.next_wave_id += 1
                elif po["kind"] == "add" and self.basket is not None and len(self.basket["legs"]) < self.cfg.max_legs:
                    b = self.basket
                    old_v = float(b["total_volume"])
                    add_v = round(self.cfg.leg_lot, 2)
                    new_v = round(old_v + add_v, 2)
                    new_avg = round((b["avg_entry"] * old_v + fill_px * add_v) / new_v, 4)
                    # Verify risk-free lock with wide breathing room before committing add
                    if (side == "buy" and (bid - self.cfg.add_breathing_room) >= (new_avg + 0.60)) or (
                        side == "sell" and (ask + self.cfg.add_breathing_room) <= (new_avg - 0.60)
                    ):
                        b["legs"].append({"entry": round(fill_px, 4), "vol": add_v, "ts": ts})
                        b["total_volume"] = new_v
                        b["avg_entry"] = new_avg
                        b["last_add_ts"] = ts
                        if side == "buy":
                            lock_sl = round(min(new_avg + 0.60, bid - self.cfg.add_breathing_room), 4)
                            if lock_sl > b["stop"]:
                                b["stop"] = lock_sl
                                b["stop_locked"] = True
                        else:
                            lock_sl = round(max(new_avg - 0.60, ask + self.cfg.add_breathing_room), 4)
                            if lock_sl < b["stop"]:
                                b["stop"] = lock_sl
                                b["stop_locked"] = True

        # 2) Manage open basket
        if self.basket is not None:
            b = self.basket
            side = b["side"]
            ex_px = bid if side == "buy" else ask
            cur_atr = float(self._atr) if self._atr is not None else 3.0
            if side == "buy":
                b["best_px"] = max(b["best_px"], bid)
                avg_gain = bid - b["avg_entry"]
                last_net = bid - b["legs"][-1]["entry"]
            else:
                b["best_px"] = min(b["best_px"], ask)
                avg_gain = b["avg_entry"] - ask
                last_net = b["legs"][-1]["entry"] - ask

            # Hard/locked SL first (gap-aware)
            if (side == "buy" and bid <= b["stop"]) or (side == "sell" and ask >= b["stop"]):
                rsn = ("chandelier_trail" if avg_gain >= 0 else "locked_sl_slip") if b["stop_locked"] else "hard_stop_loss"
                self._close(ex_px, ts, rsn)
                return

            # Wide Break-Even arm
            be_arm = max(self.cfg.be_arm_min_price, self.cfg.be_arm_atr_mult * cur_atr)
            if (not b["stop_locked"]) and avg_gain >= be_arm:
                if side == "buy":
                    be_sl = round(min(b["avg_entry"] + 0.50, bid - 2.50), 4)
                    if be_sl > b["stop"]:
                        b["stop"] = be_sl
                        b["stop_locked"] = True
                else:
                    be_sl = round(max(b["avg_entry"] - 0.50, ask + 2.50), 4)
                    if be_sl < b["stop"]:
                        b["stop"] = be_sl
                        b["stop_locked"] = True

            # Chandelier ATR trailing stop
            tr_arm = max(self.cfg.trail_arm_min_price, self.cfg.trail_arm_atr_mult * cur_atr)
            if avg_gain >= tr_arm:
                tr_dist = min(max(self.cfg.trail_atr_mult * cur_atr, self.cfg.trail_min_dist), self.cfg.trail_max_dist)
                if side == "buy":
                    c_sl = round(b["best_px"] - tr_dist, 4)
                    if c_sl > b["stop"]:
                        b["stop"] = c_sl
                        b["stop_locked"] = True
                else:
                    c_sl = round(b["best_px"] + tr_dist, 4)
                    if c_sl < b["stop"]:
                        b["stop"] = c_sl
                        b["stop_locked"] = True

            # Max hold
            if (ts - b["opened_at"]) >= self.cfg.max_hold_sec:
                self._close(ex_px, ts, "max_hold_exit")
                return

            # Risk-free pyramid add on strong continuation
            add_step = max(self.cfg.add_step_min_price, self.cfg.add_step_atr_mult * cur_atr)
            if (
                self.cfg.enable_adds
                and self.pending is None
                and len(b["legs"]) < self.cfg.max_legs
                and (ts - b["last_add_ts"]) >= self.cfg.add_interval_sec
                and spread <= self.cfg.max_spread_price
                and last_net >= add_step
            ):
                est_fill = ask if side == "buy" else bid
                old_v = float(b["total_volume"])
                new_v = round(old_v + self.cfg.leg_lot, 2)
                est_avg = (b["avg_entry"] * old_v + est_fill * self.cfg.leg_lot) / new_v
                rf_ok = (side == "buy" and (bid - self.cfg.add_breathing_room) >= (est_avg + 0.60)) or (
                    side == "sell" and (ask + self.cfg.add_breathing_room) <= (est_avg - 0.60)
                )
                if rf_ok:
                    self.pending = {"kind": "add", "side": side, "fill_ts": ts + self.cfg.latency_sec}
            return

        # 3) Flat: evaluate strictly causal Donchian breakout on closed M5 bar
        if just_closed is None or self.pending is not None or ts < self.cooldown_until:
            return
        n_dc = self.cfg.donchian_bars
        if len(self._bars) < max(n_dc + 2, 14) or self._ema_fast is None or self._ema_slow is None or self._atr is None:
            return
        if spread > self.cfg.max_spread_price:
            return
        atr = float(self._atr)
        if atr < self.cfg.min_atr_price or atr > self.cfg.max_atr_price:
            return
        if self._trend_eff(8) < self.cfg.min_trend_eff:
            return

        prior_bars = list(self._bars)[-(n_dc + 1) : -1]
        dc_hi = max(b["h"] for b in prior_bars)
        dc_lo = min(b["l"] for b in prior_bars)
        o, h, lo, c = just_closed["o"], just_closed["h"], just_closed["l"], just_closed["c"]
        rng = max(h - lo, 1e-9)
        if (abs(c - o) / rng) < self.cfg.min_body_frac:
            return

        sl_dist = min(max(self.cfg.sl_atr_mult * atr, self.cfg.min_stop_price), self.cfg.max_stop_price)
        if c > dc_hi and c > o and self._ema_fast > self._ema_slow:
            self.pending = {"kind": "open", "side": "buy", "fill_ts": ts + self.cfg.latency_sec, "sl_dist": sl_dist}
        elif c < dc_lo and c < o and self._ema_fast < self._ema_slow:
            self.pending = {"kind": "open", "side": "sell", "fill_ts": ts + self.cfg.latency_sec, "sl_dist": sl_dist}

    def close_open(self, ts: float, bid: float, ask: float) -> None:
        if self.basket is not None:
            ex_px = bid if self.basket["side"] == "buy" else ask
            self._close(ex_px, ts, "eof_seg")


class CoordinatedAWV4:
    """AW_V4 portfolio with Bug #3 Fix: cancels fresh SHOCK10 open when VSNAP6 is already open on the spike."""

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
        for sub in (self.vsnap, self.macro, self.zscore, *([self.trend] if self.trend is not None else [])):
            prev = len(sub.waves_closed)
            sub.on_tick(ts, bid, ask, m1)
            if len(sub.waves_closed) > prev:
                self.waves_closed.extend(sub.waves_closed[prev:])

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

    def __init__(self, factory: Any, kind: str, engine_label: str = "OTHER") -> None:
        self._factory = factory
        self._kind = kind
        self._engine_label = engine_label
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
                for t in self.eng.trades_closed[prev:]:
                    self.waves_closed.append(_tag_wave(t, str(t.get("engine_label", t.get("engine", "OTHER")))))
        elif self._kind == "m1":
            evs = self.eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1)
            for ev in evs:
                if ev.get("type") == "CLOSE_ALL":
                    self.waves_closed.append(_tag_wave(ev["wave"], self._engine_label))
        else:
            evs = self.eng.on_tick(ts=ts, bid=bid, ask=ask)
            for ev in evs:
                if ev.get("type") == "CLOSE_ALL":
                    self.waves_closed.append(_tag_wave(ev["wave"], self._engine_label))

    def close_open(self, ts: float, bid: float, ask: float) -> None:
        if self._kind == "suite":
            prev = len(self.eng.trades_closed)
            self.eng.close_all_open(ts=ts, bid=bid, ask=ask, reason="eof_seg")
            if len(self.eng.trades_closed) > prev:
                for t in self.eng.trades_closed[prev:]:
                    self.waves_closed.append(_tag_wave(t, str(t.get("engine_label", t.get("engine", "OTHER")))))
        elif hasattr(self.eng, "close_open_wave"):
            w = self.eng.close_open_wave(ts=ts, bid=bid, ask=ask, reason="eof_seg")
            if w is not None:
                self.waves_closed.append(_tag_wave(w, self._engine_label))
        elif hasattr(self.eng, "force_close_open"):
            w = self.eng.force_close_open(ts=ts, bid=bid, ask=ask, reason="eof_seg")
            if w is not None:
                self.waves_closed.append(_tag_wave(w, self._engine_label))
        elif getattr(self.eng, "basket", None) is not None and hasattr(self.eng, "_close"):
            ex_px = bid if self.eng.basket["side"] == "buy" else ask
            w = self.eng._close(ex_px, ts, "eof_seg")
            if w is not None:
                self.waves_closed.append(_tag_wave(w, self._engine_label))


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


def _bucket_label(ts: float, overnight_cut: float, *, is_mock: bool) -> str:
    if is_mock:
        return "MOCK_D1" if ts < overnight_cut else "LAST_NIGHT"
    if ts >= overnight_cut:
        return "LAST_NIGHT"
    d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    cut_d = datetime.fromtimestamp(overnight_cut, tz=timezone.utc).strftime("%Y-%m-%d")
    return f"{d}_DAY" if d == cut_d else d


def _short_bucket_hdr(b: str) -> str:
    if b == "LAST_NIGHT":
        return "LAST_NIGHT"
    if b.startswith("2026-"):
        return b[5:]  # e.g. 11-24, 11-25, 11-26_DAY
    return b[:10]


def build_candidates() -> list[tuple[str, Any]]:
    vsnap_base = replace(dict(fast_snap_profiles(base_lot=0.02))["VSNAP_SHOCK_6"], name="VSNAP6_24H", active_server_hours=None)
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
        return SimpleAdapter(lambda: AllWeatherPaperSuite(), "suite", "SUITE")

    def _shock10() -> Any:
        return SimpleAdapter(lambda: S1VelocitySurferEngine(cfg=shock_cfg), "m1", "SHOCK10")

    def _macro_sess() -> Any:
        return SimpleAdapter(lambda: PullbackDipEngine(cfg=macro_sess_cfg), "m1", "MACRO_WAVE")

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
            "M5_TREND",
        )

    return [
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
        ("CALM_M1_EMA_24H", _calm("CALM_M1_EMA_24H")),
        ("CALM_M3_SWING_24H", _calm("CALM_M3_SWING_24H")),
        ("CALM_M5_TREND_24H", _calm("CALM_M5_TREND_24H")),
        ("CALM_M5_RR_24H", _calm("CALM_M5_RR_24H")),
        ("AW_V4_FIX124_WIDE", PortfolioWrapper([_shock10, _vsnap_htf_wide, _macro_sess, _z_guard_tight])),
        ("AW_V4_PLUS_M5_RR", PortfolioWrapper([_shock10, _vsnap_htf_wide, _macro_sess, _z_guard_tight, lambda: _calm("CALM_M5_RR_24H")])),
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
    # Parameter-stability neighbors around VSNAP6_WIDE
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
    # Bug #2B Fix: Also fix Leg-2 choke-out (sl_min_breathing_room was 1.15 -> killed 2-leg rally winners in 10s!)
    vsnap_wide_add22 = replace(
        vsnap_wide,
        name="VSNAP6_WIDE_ADD22",
        sl_min_breathing_room=2.20,
        add_step_price=3.00,
        runner_max_hold_sec=1200.0,
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
    m5_pro_base = build_calm_regime_profile_config("CALM_M5_PRO_24H", leg_lot=0.02, max_legs=8)

    # Gated M5 Trend-Riders (sleep in sideways chop, wake up & surf real multi-hour trends like +$108 rally!)
    m5_e35_l01 = replace(
        m5_rr_base,
        leg_lot=0.01,
        dyn_max_leg_lot=0.06,
        dyn_max_total_lot=0.25,
        min_atr_price=2.20,
        min_trend_sep_atr=0.35,
        min_trend_eff=0.35,
    )
    m5_m30_l02 = replace(
        m5_rr_base,
        leg_lot=0.02,
        dyn_max_leg_lot=0.08,
        dyn_max_total_lot=0.32,
        min_atr_price=2.00,
        min_trend_sep_atr=0.28,
        min_trend_eff=0.30,
    )
    m5_m60_l02 = replace(
        m5_rr_base,
        leg_lot=0.02,
        dyn_max_leg_lot=0.08,
        dyn_max_total_lot=0.32,
        min_atr_price=1.90,
        min_trend_sep_atr=0.24,
        min_trend_eff=0.26,
    )
    m5_pro_m60_l02 = replace(
        m5_pro_base,
        leg_lot=0.02,
        dyn_max_leg_lot=0.08,
        dyn_max_total_lot=0.32,
        min_atr_price=1.90,
        min_trend_sep_atr=0.24,
        min_trend_eff=0.26,
    )

    def _aw_v3() -> Any:
        return SimpleAdapter(lambda: AllWeatherPaperSuite(), "suite", "SUITE")

    def _shock10() -> Any:
        return SimpleAdapter(lambda: S1VelocitySurferEngine(cfg=shock_cfg), "m1", "SHOCK10")

    def _macro_sess() -> Any:
        return SimpleAdapter(lambda: PullbackDipEngine(cfg=macro_cfg), "m1", "MACRO_WAVE")

    def _macro_24h_10() -> Any:
        return GuardedMacroWave24H(macro_cfg, night_min_30m_move=10.0)

    def _macro_24h_12() -> Any:
        return GuardedMacroWave24H(macro_cfg, night_min_30m_move=12.0)

    def _z_safe() -> Any:
        return GuardedZScoreMR(z_tight, block_counter_30m=9.0, block_counter_60m=12.0)

    def _v_wide() -> Any:
        return GuardedFastSnap(vsnap_wide, block_counter_15m=5.0, block_counter_30m=8.0)

    def _v_nb1() -> Any:
        return GuardedFastSnap(vsnap_wide_nb1, block_counter_15m=5.0, block_counter_30m=8.0)

    def _v_nb2() -> Any:
        return GuardedFastSnap(vsnap_wide_nb2, block_counter_15m=5.0, block_counter_30m=8.0)

    def _v_add22() -> Any:
        return GuardedFastSnap(vsnap_wide_add22, block_counter_15m=5.0, block_counter_30m=8.0)

    def _m5_e35() -> Any:
        return GuardedCalmTrend(m5_e35_l01, min_30m_align=0.0, min_60m_align=0.0)

    def _m5_m30_8() -> Any:
        return GuardedCalmTrend(m5_m30_l02, min_30m_align=8.0, min_60m_align=0.0)

    def _m5_m60_14() -> Any:
        return GuardedCalmTrend(m5_m60_l02, min_30m_align=6.0, min_60m_align=14.0)

    def _m5_pro_m60_14() -> Any:
        return GuardedCalmTrend(m5_pro_m60_l02, min_30m_align=6.0, min_60m_align=14.0)

    return [
        # 1) Baseline shipped AW_V3
        ("AW_V3_SHIPPED_BASE", _aw_v3()),
        # 2) VSNAP6 stability plateau + Leg-2 breathing fix (Bug #1 + #2/#2B)
        ("VSNAP6_WIDE_BASE", _v_wide()),
        ("VSNAP6_WIDE_NB1", _v_nb1()),
        ("VSNAP6_WIDE_NB2", _v_nb2()),
        ("VSNAP6_WIDE_ADD22", _v_add22()),
        # 3) Conditional 24h MACRO_WAVE solo (Bug #5A)
        ("MACRO_24H_TREND10", _macro_24h_10()),
        ("MACRO_24H_TREND12", _macro_24h_12()),
        # 4) Gated M5 Trend-Riders solo (Bug #5B)
        ("M5_RR_E35_L01", _m5_e35()),
        ("M5_RR_M30_8_L02", _m5_m30_8()),
        ("M5_RR_M60_14_L02", _m5_m60_14()),
        ("M5_PRO_M60_14_L02", _m5_pro_m60_14()),
        # 5) Full Coordinated AW_V4 Portfolios (Bugs #1+#2+#3+#4+#5)
        ("AW_V4_COORD_BASE", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_wide, macro_factory=_macro_sess, zscore_factory=_z_safe)),
        ("AW_V4_ADD22_MAC10", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_add22, macro_factory=_macro_24h_10, zscore_factory=_z_safe)),
        ("AW_V4_WIDE_M5_M30", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_wide, macro_factory=_macro_24h_10, zscore_factory=_z_safe, trend_factory=_m5_m30_8)),
        ("AW_V4_WIDE_M5_M60", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_wide, macro_factory=_macro_24h_10, zscore_factory=_z_safe, trend_factory=_m5_m60_14)),
        ("AW_V4_ADD22_M5_E35", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_add22, macro_factory=_macro_24h_10, zscore_factory=_z_safe, trend_factory=_m5_e35)),
        ("AW_V4_ADD22_M5_M30", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_add22, macro_factory=_macro_24h_10, zscore_factory=_z_safe, trend_factory=_m5_m30_8)),
        ("AW_V4_ADD22_M5_M60", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_add22, macro_factory=_macro_24h_10, zscore_factory=_z_safe, trend_factory=_m5_m60_14)),
        ("AW_V4_ADD22_M5PRO", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_add22, macro_factory=_macro_24h_10, zscore_factory=_z_safe, trend_factory=_m5_pro_m60_14)),
    ]


def build_phase3_candidates() -> list[tuple[str, Any]]:
    """Clean Final Selection: Proven AW_V4 Core (Bugs #1-#4, MACRO_SESS intact) + 24h Trend-Rider Shootout."""
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
    vsnap_nb1 = replace(
        vsnap_wide,
        name="VSNAP6_WIDE_NB1",
        be_arm_price=2.60,
        ratchet_arm_price=4.50,
        ratchet_breathing_room=2.30,
        trail_arm_price=6.00,
        trail_pullback_price=3.00,
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
    m5_pro_base = build_calm_regime_profile_config("CALM_M5_PRO_24H", leg_lot=0.02, max_legs=8)

    # Stability plateau around M5_RR_E42 (which made +$43.60 LAST_NIGHT at 0.01 lot!)
    m5_e38_01 = replace(m5_rr_base, leg_lot=0.01, dyn_max_leg_lot=0.06, dyn_max_total_lot=0.25, min_atr_price=2.30, min_trend_sep_atr=0.38, min_trend_eff=0.38)
    m5_e42_01 = replace(m5_rr_base, leg_lot=0.01, dyn_max_leg_lot=0.06, dyn_max_total_lot=0.25, min_atr_price=2.50, min_trend_sep_atr=0.42, min_trend_eff=0.42)
    m5_e45_01 = replace(m5_rr_base, leg_lot=0.01, dyn_max_leg_lot=0.06, dyn_max_total_lot=0.25, min_atr_price=2.60, min_trend_sep_atr=0.45, min_trend_eff=0.45)
    m5_e42_02 = replace(m5_rr_base, leg_lot=0.02, dyn_max_leg_lot=0.08, dyn_max_total_lot=0.35, min_atr_price=2.50, min_trend_sep_atr=0.42, min_trend_eff=0.42)
    m5_e42_2h = replace(m5_e42_02, max_hold_sec=7200.0)
    m5_pro_e42 = replace(m5_pro_base, leg_lot=0.02, dyn_max_leg_lot=0.08, dyn_max_total_lot=0.35, min_atr_price=2.50, min_trend_sep_atr=0.42, min_trend_eff=0.42)

    # Institutional M5 Donchian Box-Breakout + Chandelier ATR Trailing Trend-Riders (zero curve-fitting!)
    dc12_cfg = DonchianTrendConfig(bar_sec=300, donchian_bars=12, leg_lot=0.02, max_legs=3, min_atr_price=2.00, min_trend_eff=0.35)
    dc16_cfg = DonchianTrendConfig(bar_sec=300, donchian_bars=16, leg_lot=0.02, max_legs=3, min_atr_price=2.00, min_trend_eff=0.35)
    dc12_e42_cfg = DonchianTrendConfig(bar_sec=300, donchian_bars=12, leg_lot=0.02, max_legs=3, min_atr_price=2.20, min_trend_eff=0.42)

    def _aw_v3() -> Any:
        return SimpleAdapter(lambda: AllWeatherPaperSuite(), "suite", "SUITE")

    def _shock10() -> Any:
        return SimpleAdapter(lambda: S1VelocitySurferEngine(cfg=shock_cfg), "m1", "SHOCK10")

    def _macro_sess() -> Any:
        return SimpleAdapter(lambda: PullbackDipEngine(cfg=macro_cfg), "m1", "MACRO_WAVE")

    def _z_safe() -> Any:
        return GuardedZScoreMR(z_tight, block_counter_30m=9.0, block_counter_60m=12.0)

    def _v_wide() -> Any:
        return GuardedFastSnap(vsnap_wide, block_counter_15m=5.0, block_counter_30m=8.0)

    def _v_nb1() -> Any:
        return GuardedFastSnap(vsnap_nb1, block_counter_15m=5.0, block_counter_30m=8.0)

    return [
        # 1) Baseline shipped AW_V3 vs Proven 4-Engine AW_V4 Core
        ("AW_V3_SHIPPED_BASE", _aw_v3()),
        ("AW_V4_COORD_BASE", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_wide, macro_factory=_macro_sess, zscore_factory=_z_safe)),
        ("AW_V4_COORD_NB1", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_nb1, macro_factory=_macro_sess, zscore_factory=_z_safe)),
        # 2) Solo 24h Trend-Riders (Day-by-Day transparency on 11-25, 11-26_DAY, LAST_NIGHT!)
        ("M5_RR_E38_L01", GuardedCalmTrend(m5_e38_01)),
        ("M5_RR_E42_L01", GuardedCalmTrend(m5_e42_01)),
        ("M5_RR_E45_L01", GuardedCalmTrend(m5_e45_01)),
        ("M5_RR_E42_L02", GuardedCalmTrend(m5_e42_02)),
        ("M5_RR_E42_2H_L02", GuardedCalmTrend(m5_e42_2h)),
        ("M5_PRO_E42_L02", GuardedCalmTrend(m5_pro_e42)),
        ("M5_DONCHIAN12_L02", DonchianTrendRider(dc12_cfg)),
        ("M5_DONCHIAN16_L02", DonchianTrendRider(dc16_cfg)),
        ("M5_DONCH12_E42_02", DonchianTrendRider(dc12_e42_cfg)),
        # 3) Full 5-Engine AW_V4 Portfolios (Proven 4-Engine Core + Clean 24h Trend-Rider!)
        ("AW_V4_BASE_E42_01", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_wide, macro_factory=_macro_sess, zscore_factory=_z_safe, trend_factory=lambda: GuardedCalmTrend(m5_e42_01))),
        ("AW_V4_BASE_E42_02", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_wide, macro_factory=_macro_sess, zscore_factory=_z_safe, trend_factory=lambda: GuardedCalmTrend(m5_e42_02))),
        ("AW_V4_BASE_E42_2H", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_wide, macro_factory=_macro_sess, zscore_factory=_z_safe, trend_factory=lambda: GuardedCalmTrend(m5_e42_2h))),
        ("AW_V4_BASE_M5PRO", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_wide, macro_factory=_macro_sess, zscore_factory=_z_safe, trend_factory=lambda: GuardedCalmTrend(m5_pro_e42))),
        ("AW_V4_BASE_DC12", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_wide, macro_factory=_macro_sess, zscore_factory=_z_safe, trend_factory=lambda: DonchianTrendRider(dc12_cfg))),
        ("AW_V4_BASE_DC12E42", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_wide, macro_factory=_macro_sess, zscore_factory=_z_safe, trend_factory=lambda: DonchianTrendRider(dc12_e42_cfg))),
        ("AW_V4_NB1_E42_02", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_nb1, macro_factory=_macro_sess, zscore_factory=_z_safe, trend_factory=lambda: GuardedCalmTrend(m5_e42_02))),
        ("AW_V4_NB1_DC12", CoordinatedAWV4(shock_factory=_shock10, vsnap_factory=_v_nb1, macro_factory=_macro_sess, zscore_factory=_z_safe, trend_factory=lambda: DonchianTrendRider(dc12_cfg))),
    ]


def _print_engine_day_matrix(
    model_name: str,
    waves: list[dict[str, Any]],
    buckets: list[str],
    overnight_cut: float,
    *,
    is_mock: bool,
) -> None:
    engines_order = ["VSNAP6", "SHOCK10", "MACRO_WAVE", "ZSCORE_MR", "M5_TREND", "DC_TREND"]
    present_engs = [e for e in engines_order if any(w.get("_engine") == e for w in waves)]
    extra = sorted({str(w.get("_engine", "OTHER")) for w in waves} - set(present_engs))
    present_engs.extend(extra)

    hdr_cols = " | ".join(f"{_short_bucket_hdr(b):>15s}" for b in buckets)
    print(f"\n  [{model_name}] PER-ENGINE x PER-DAY BREAKDOWN (exact reconciliation):")
    print(f"  {'ENGINE':12s} | {hdr_cols} | {'TOTAL':>17s}")
    print("  " + "-" * (35 + 18 * len(buckets)))
    for eng in present_engs:
        ew = [w for w in waves if w.get("_engine") == eng]
        cells: list[str] = []
        for b in buckets:
            bw = [
                w
                for w in ew
                if _bucket_label(float(w.get("opened_at", w.get("closed_at", 0.0))), overnight_cut, is_mock=is_mock) == b
            ]
            st = _stats(bw)
            cells.append(f"{_fmt_usd(st['net'])}({st['w']}/{st['l']})" if st["n"] else "$0.00(0/0)")
        st_all = _stats(ew)
        tot_cell = f"{_fmt_usd(st_all['net'])} ({st_all['w']}W/{st_all['l']}L)"
        print(f"  {eng:12s} | " + " | ".join(f"{c:>15s}" for c in cells) + f" | {tot_cell:>17s}")

    # TOTAL row
    tot_cells: list[str] = []
    for b in buckets:
        bw = [
            w
            for w in waves
            if _bucket_label(float(w.get("opened_at", w.get("closed_at", 0.0))), overnight_cut, is_mock=is_mock) == b
        ]
        st = _stats(bw)
        tot_cells.append(f"{_fmt_usd(st['net'])}({st['w']}/{st['l']})")
    sa = _stats(waves)
    print("  " + "-" * (35 + 18 * len(buckets)))
    print(
        f"  {'TOTAL':12s} | "
        + " | ".join(f"{c:>15s}" for c in tot_cells)
        + f" | {_fmt_usd(sa['net'])} ({sa['w']}W/{sa['l']}L)"
    )


def _print_last_night_trades(
    model_name: str,
    waves: list[dict[str, Any]],
    overnight_cut: float,
) -> None:
    ngt = [w for w in waves if float(w.get("opened_at", w.get("closed_at", 0.0))) >= overnight_cut]
    ngt.sort(key=lambda w: float(w.get("opened_at", w.get("closed_at", 0.0))))
    print(f"\n  [{model_name}] COMPLETE LAST-NIGHT TRADE-BY-TRADE AUDIT ({len(ngt)} trades, >= {_iso_dt(overnight_cut)} SRV):")
    if not ngt:
        print("    (no trades in LAST_NIGHT)")
        return
    print(
        f"  {'#':>2s} {'OPEN_SRV':14s} {'ENGINE':11s} {'SIDE':4s} {'LG':>2s} {'VOL':>4s} "
        f"{'ENTRY->EXIT':19s} {'HOLD':>5s} {'REASON':18s} {'NET_USD':>9s} {'CUM_USD':>9s}"
    )
    print("  " + "-" * 108)
    cum = 0.0
    for i, w in enumerate(ngt, 1):
        pnl = float(w.get("net_pnl_usd", 0.0))
        cum += pnl
        opened = float(w.get("opened_at", w.get("closed_at", 0.0)))
        eng = str(w.get("_engine", w.get("engine_label", "OTHER")))[:11]
        side = str(w.get("side", "?")).upper()[:4]
        legs = int(w.get("legs_count", 1))
        vol = float(w.get("total_volume", w.get("volume", 0.0)))
        ent = float(w.get("avg_entry", w.get("entry_price", 0.0)))
        ex = float(w.get("exit_price", 0.0))
        hold = float(w.get("hold_seconds", 0.0))
        rsn = str(w.get("exit_reason", ""))[:18]
        print(
            f"  {i:2d} {_iso_hms(opened):14s} {eng:11s} {side:4s} {legs:2d} {vol:4.2f} "
            f"{ent:8.2f}->{ex:<8.2f} {hold:4.0f}s {rsn:18s} {_fmt_usd(pnl):>9s} {_fmt_usd(cum):>9s}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Day-by-Day Deduplicated Gold Bug-Fix Audit (ORDERS=0)")
    parser.add_argument("--logs-dir", default="logs")
    parser.add_argument("--phase2", action="store_true", help="Run Phase-2 stability, Bug #2B/#3/#5 fixes & day-by-day report")
    parser.add_argument("--phase3", action="store_true", help="Run Phase-3 Clean Final Selection (Proven AW_V4 Core + 24h Trend-Rider Shootout)")
    parser.add_argument("--trades", default="", help="Optional model name to print full LAST_NIGHT trade log for")
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
        overnight_cut = datetime(2026, 11, 26, 19, 35, 0, tzinfo=timezone.utc).timestamp()

    total_ticks = sum(len(s) for s in segs)

    # Auto-discover chronological day/session buckets and exact market price stats per bucket
    bucket_order: list[str] = []
    bucket_meta: dict[str, dict[str, Any]] = {}
    for seg in segs:
        for ts, bid, ask, _ in seg:
            b = _bucket_label(ts, overnight_cut, is_mock=args.mock)
            mid = 0.5 * (bid + ask)
            if b not in bucket_meta:
                bucket_order.append(b)
                bucket_meta[b] = {
                    "start": ts,
                    "end": ts,
                    "ticks": 0,
                    "open": mid,
                    "close": mid,
                    "lo": mid,
                    "hi": mid,
                }
            m = bucket_meta[b]
            m["end"] = ts
            m["ticks"] += 1
            m["close"] = mid
            if mid < m["lo"]:
                m["lo"] = mid
            if mid > m["hi"]:
                m["hi"] = mid

    prior_buckets = [b for b in bucket_order if b != "LAST_NIGHT"]

    print(
        f"=== GOLD DAY-BY-DAY DEDUPLICATED BUG-FIX AUDIT ({n_files} FILES -> {len(segs)} SEGMENTS, "
        f"{total_ticks:,d} UNIQUE TICKS | ORDERS=0) ==="
    )
    print("SECTION 1 — MARKET TIMELINE BY DAY / SESSION (Server Clock):")
    for b in bucket_order:
        m = bucket_meta[b]
        chg = m["close"] - m["open"]
        rng = m["hi"] - m["lo"]
        print(
            f"  {b:14s}: {_iso_dt(m['start'])} -> {_iso_dt(m['end'])} | {m['ticks']:7,d}t | "
            f"{m['open']:.2f}->{m['close']:.2f} (net {chg:+7.2f}, lo..hi {m['lo']:.2f}..{m['hi']:.2f}, rng=${rng:.2f})"
        )

    if args.phase3:
        cands = build_phase3_candidates()
    elif args.phase2:
        cands = build_phase2_candidates()
    else:
        cands = build_candidates()

    day_hdr = " | ".join(f"{_short_bucket_hdr(b):>14s}" for b in prior_buckets)
    sep_len = 82 + 17 * len(prior_buckets) + 27
    print("\nSECTION 2 — MODEL COMPARISON (EXACT DAY-BY-DAY + LAST NIGHT RALLY, 1x LOT):")
    print("-" * sep_len)
    print(
        f"{'MODEL':21s} | {'ALL_NET':>9s} {'ALL_DD':>8s} {'R/DD':>5s} {'W/L':>7s} {'PF':>5s} "
        f"| {day_hdr} | {'LAST_NIGHT (DD, W/L)':>26s}"
    )
    print("-" * sep_len)

    results: dict[str, dict[str, Any]] = {}
    for name, model in cands:
        for seg_idx, seg in enumerate(segs):
            if seg_idx > 0:
                model.reset_segment()
            for ts, bid, ask, m1_ctx in seg:
                model.on_tick(ts, bid, ask, m1_ctx)
            if seg:
                model.close_open(seg[-1][0], seg[-1][1], seg[-1][2])

        all_w = model.waves_closed
        sa = _stats(all_w)
        by_b: dict[str, dict[str, Any]] = {}
        for b in bucket_order:
            bw = [
                w
                for w in all_w
                if _bucket_label(float(w.get("opened_at", w.get("closed_at", 0.0))), overnight_cut, is_mock=args.mock) == b
            ]
            by_b[b] = _stats(bw)
        results[name] = {"waves": all_w, "all": sa, "by_b": by_b}

        prior_cells = " | ".join(
            f"{_fmt_usd(by_b[b]['net'], 1)}({by_b[b]['w']}/{by_b[b]['l']})" if by_b[b]["n"] else "$0.0(0/0)"
            for b in prior_buckets
        )
        prior_fmt = " | ".join(f"{c:>14s}" for c in prior_cells.split(" | ")) if prior_buckets else ""
        sn = by_b.get("LAST_NIGHT", _stats([]))
        ngt_cell = f"{_fmt_usd(sn['net']):>9s} -${sn['dd']:5.1f} {sn['w']:2d}W/{sn['l']:<2d}L"
        print(
            f"{name:21s} | {_fmt_usd(sa['net']):>9s} -${sa['dd']:6.2f} {sa['rdd']:5.2f} {sa['w']:2d}W/{sa['l']:<2d}L {sa['pf']:5.2f} "
            f"| {prior_fmt} | {ngt_cell:>26s}"
        )
    print("=" * sep_len)

    # SECTION 3: Per-Engine x Per-Day Reconciliation Matrices for Baseline + Top Portfolio Winners
    port_names = [n for n in results if n.startswith("AW_V4")]
    # Rank AW_V4 portfolios prioritizing strong LAST_NIGHT profit AND strong overall R/DD + Net
    ranked_ports = sorted(
        port_names,
        key=lambda n: (
            1 if results[n]["by_b"].get("LAST_NIGHT", {"net": 0.0})["net"] > 15.0 else 0,
            results[n]["all"]["net"] + 1.5 * results[n]["by_b"].get("LAST_NIGHT", {"net": 0.0})["net"],
        ),
        reverse=True,
    )

    print("\nSECTION 3 — PER-ENGINE x PER-DAY RECONCILIATION (ZERO MYSTERY):")
    inspect_list = ["AW_V3_SHIPPED_BASE"]
    for pn in ranked_ports[:3]:
        if pn not in inspect_list:
            inspect_list.append(pn)
    for mname in inspect_list:
        if mname in results:
            _print_engine_day_matrix(mname, results[mname]["waves"], bucket_order, overnight_cut, is_mock=args.mock)

    # SECTION 4: Complete Trade-by-Trade LAST_NIGHT Audit for #1 Winner (or user-specified --trades model)
    print("\nSECTION 4 — LAST-NIGHT TRADE-BY-TRADE AUDIT:")
    trade_target = args.trades if (args.trades and args.trades in results) else (ranked_ports[0] if ranked_ports else "AW_V3_SHIPPED_BASE")
    if trade_target in results:
        _print_last_night_trades(trade_target, results[trade_target]["waves"], overnight_cut)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
