"""All-Weather Paper Trading Suite (COMBO_AW_V4_24H, ORDERS=0).

Combines the 4 causally validated & bug-fixed 24-hour engines into ONE unified MetaTrader-style paper account:
  1. SHOCK10_DYN_RUN  (24H News / >=$10 per 60s shock dynamic runner; coordinated with VSNAP6 to prevent duplicate peak entries)
  2. VSNAP6_24H       (24H Fast $6+/60s burst runner with 15m/30m HTF counter-trend guard + 50% partial profit bank @ +$2.60 & wide free runner)
  3. MACRO_WAVE_DYN   (London/NY macro pullback-continuation dynamic pyramid)
  4. ZSCORE_MR_24H    (24H TickRobust Z-Score MR with tight $3.20..$4.80 SL + 30m/60m macro breakout guard)

Legacy COMBO_AW_V3 (`engine_version="v3"`) and COMBO_AW_V2 (`calm_profile="CALM_M1_PRO_SESS"`, `fast_shock_24h=False`)
remain supported for apples-to-apples historical A/B benchmarks.

Strictly read-only / paper-only (`paper_only=True`, `orders_submitted=0`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from trade_mcp.calm_regime_engines import CalmRegimeEngine, build_calm_regime_profile_config
from trade_mcp.fast_snap_engines import FastSnapEngine, fast_snap_profiles
from trade_mcp.quant_github_accel_lab import TickRobustZScoreConfig, TickRobustZScoreEngine
from trade_mcp.s1_impulse_audit import PullbackDipEngine, pullback_dip_profiles
from trade_mcp.s1_velocity_surfer import S1VelocitySurferEngine, build_s1_profile_config


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _build_macro_wave_cfg(*, leg_lot: float = 0.01, max_legs: int = 6) -> Any:
    by_name = dict(pullback_dip_profiles(leg_lot=leg_lot, max_legs=min(4, max_legs)))
    return by_name["MACRO_WAVE_DYN"]


def _build_fast_shock6_cfg(
    *,
    leg_lot: float = 0.02,
    all_24h: bool = True,
    varlot: bool = False,
    engine_version: str = "v4",
) -> Any:
    by_name = dict(fast_snap_profiles(base_lot=leg_lot))
    cfg = by_name["VSNAP_SHOCK_6"]
    if all_24h and varlot:
        cfg = replace(
            cfg,
            name="VSNAP6_24H_VARLOT",
            active_server_hours=None,
            leg_lot=max(0.03, leg_lot),
            max_legs=5,
            add_step_price=1.60,
            add_interval_sec=4.0,
            dyn_max_l1_lot=0.06,
            dyn_max_leg_lot=0.12,
            dyn_max_total_lot=0.45,
        )
    elif all_24h:
        cfg = replace(cfg, name="VSNAP6_24H", active_server_hours=None)
    if engine_version == "v4":
        # Bug #1 (causal 15m/30m HTF counter-trend guard) + Bug #2 (50% partial profit bank @ +$2.60 + wide free runner)
        cfg = replace(
            cfg,
            block_counter_15m=5.0,
            block_counter_30m=8.0,
            partial_arm_price=2.60,
            partial_take_frac=0.50,
            partial_runner_sl_offset=0.60,
            be_arm_price=4.50,
            be_lock_profit=0.30,
            ratchet_arm_price=6.00,
            ratchet_frac=0.50,
            ratchet_breathing_room=3.00,
            trail_arm_price=8.00,
            trail_pullback_price=4.20,
            max_hold_sec=1800.0,
            runner_max_hold_sec=2400.0,
            max_single_tick_jump_price=4.50,
            teleport_cooldown_sec=90.0,
        )
    return cfg


def _build_zscore_mr_24h_cfg(*, leg_lot: float = 0.01, engine_version: str = "v4") -> TickRobustZScoreConfig:
    if engine_version == "v4":
        # Bug #4 + Risk-Parity Sizing: tight SL ($3.20..$4.80), causal 30m/60m breakout guard,
        # and ~$4.80/trade SL risk parity with SHOCK10 ($4.50), MACRO_WAVE ($4.50), and VSNAP6 ($5.60).
        return TickRobustZScoreConfig(
            name="ZSCORE_MR_24H",
            bar_sec=60,
            entry_z=2.2,
            adx_max=22.0,
            min_sd_cost_mult=2.5,
            min_tp_cost_mult=3.5,
            min_sl_price=3.20,
            max_sl_price=4.80,
            leg_lot=leg_lot,
            variable_lot_by_z=True,
            max_lot=round(max(0.02, leg_lot * 2.0), 2),
            night_adaptive=True,
            night_entry_z=2.15,
            night_adx_max=24.0,
            night_sd_cost_mult=2.2,
            night_tp_cost_mult=3.2,
            block_counter_30m=9.0,
            block_counter_60m=12.0,
            active_server_hours=None,
        )
    return TickRobustZScoreConfig(
        name="ZSCORE_MR_24H",
        bar_sec=60,
        entry_z=2.2,
        adx_max=22.0,
        min_sd_cost_mult=2.5,
        min_tp_cost_mult=3.5,
        min_sl_price=4.50,
        max_sl_price=7.50,
        leg_lot=leg_lot,
        variable_lot_by_z=True,
        max_lot=0.06,
        night_adaptive=True,
        night_entry_z=2.15,
        night_adx_max=24.0,
        night_sd_cost_mult=2.2,
        night_tp_cost_mult=3.2,
        active_server_hours=None,
    )


def _scale_cfg_lots(cfg: Any, risk_multiplier: float) -> Any:
    """Scale all lot-sizing fields linearly by `risk_multiplier` without altering entry/exit price geometry."""
    rm = float(risk_multiplier)
    if abs(rm - 1.0) < 1e-9:
        return cfg
    updates: dict[str, float] = {}
    for attr in ("leg_lot", "dyn_max_l1_lot", "dyn_max_leg_lot", "dyn_max_total_lot", "max_lot"):
        if hasattr(cfg, attr):
            updates[attr] = max(0.01, round(float(getattr(cfg, attr)) * rm, 2))
    min_add = max(0.01, round(0.01 * rm, 2))
    step = min_add if (rm > 1.0 and abs(rm - round(rm)) < 1e-6) else 0.01
    if hasattr(cfg, "dyn_min_add_lot"):
        updates["dyn_min_add_lot"] = min_add
    if hasattr(cfg, "dyn_lot_step"):
        updates["dyn_lot_step"] = step
    return replace(cfg, **updates) if updates else cfg


ENGINE_LABELS: dict[str, str] = {
    "SHOCK10_DYN_RUN": "NEWS_SHOCK",
    "VSNAP_SHOCK_6": "FAST_SHOCK6",
    "VSNAP6_24H": "FAST_SHOCK6",
    "VSNAP6_24H_VARLOT": "FAST_SHOCK6",
    "MACRO_WAVE_DYN": "MACRO_WAVE",
    "ZSCORE_MR_24H": "ZSCORE_MR",
    "CALM_M1_PRO_SESS": "CALM_M1_PRO",
    "CALM_M1_PRO_150": "CALM_M1_150",
}


@dataclass
class AllWeatherPaperSuite:
    """Unified MetaTrader-style paper account driving COMBO_AW_V4_24H (ORDERS=0)."""

    initial_capital_usd: float = 1000.0
    news_leg_lot: float = 0.01
    calm_leg_lot: float = 0.02
    max_legs: int = 6
    calm_profile: str = "ZSCORE_MR_24H"
    enable_fast_shock6: bool = True
    fast_shock_24h: bool = True
    fast_shock_varlot: bool = False
    risk_multiplier: float = 1.0
    engine_version: str = "v4"  # "v4" (bug-fixed 10x R/DD default) | "v3" (legacy baseline)

    shock_engine: S1VelocitySurferEngine = field(init=False)
    fast_shock_engine: FastSnapEngine | None = field(init=False)
    macro_engine: PullbackDipEngine = field(init=False)
    calm_engine: Any = field(init=False)
    engines: dict[str, Any] = field(init=False)
    engine_order: list[str] = field(init=False)

    realized_pnl_usd: float = 0.0
    peak_balance_usd: float = 1000.0
    closed_max_dd_usd: float = 0.0
    peak_equity_usd: float = 1000.0
    max_dd_usd: float = 0.0
    trades_closed: list[dict[str, Any]] = field(default_factory=list)
    seeded_m1_bars: int = 0
    _multiplier: float = 100.0

    def __post_init__(self) -> None:
        rm = float(self.risk_multiplier)
        if not math.isfinite(rm) or rm < 0.25 or rm > 5.0:
            raise ValueError(f"risk_multiplier must be in [0.25, 5.0], got {self.risk_multiplier!r}")
        if self.engine_version not in ("v4", "v3"):
            raise ValueError(f"engine_version must be 'v4' or 'v3', got {self.engine_version!r}")
        self.risk_multiplier = rm
        self.peak_balance_usd = float(self.initial_capital_usd)
        self.peak_equity_usd = float(self.initial_capital_usd)
        self._fast_name = (
            "VSNAP6_24H_VARLOT"
            if (self.fast_shock_24h and self.fast_shock_varlot)
            else ("VSNAP6_24H" if self.fast_shock_24h else "VSNAP_SHOCK_6")
        )
        self.engine_order = ["SHOCK10_DYN_RUN"]
        if self.enable_fast_shock6:
            self.engine_order.append(self._fast_name)
        self.engine_order.extend(["MACRO_WAVE_DYN", self.calm_profile])
        self._init_engines()

    def _init_engines(self) -> None:
        rm = self.risk_multiplier
        self.shock_engine = S1VelocitySurferEngine(
            cfg=_scale_cfg_lots(
                build_s1_profile_config("v2.6_SHOCK10_DYN", leg_lot=self.news_leg_lot, max_legs=self.max_legs),
                rm,
            ),
            initial_capital_usd=self.initial_capital_usd,
        )
        self.fast_shock_engine = (
            FastSnapEngine(
                cfg=_scale_cfg_lots(
                    _build_fast_shock6_cfg(
                        leg_lot=self.calm_leg_lot,
                        all_24h=self.fast_shock_24h,
                        varlot=self.fast_shock_varlot,
                        engine_version=self.engine_version,
                    ),
                    rm,
                ),
                initial_capital_usd=self.initial_capital_usd,
            )
            if self.enable_fast_shock6
            else None
        )
        self.macro_engine = PullbackDipEngine(
            cfg=_scale_cfg_lots(_build_macro_wave_cfg(leg_lot=self.news_leg_lot, max_legs=self.max_legs), rm),
            initial_capital_usd=self.initial_capital_usd,
        )
        if self.calm_profile == "ZSCORE_MR_24H":
            z_base_lot = self.news_leg_lot if self.engine_version == "v4" else max(0.03, self.calm_leg_lot)
            self.calm_engine = TickRobustZScoreEngine(
                cfg=_scale_cfg_lots(
                    _build_zscore_mr_24h_cfg(
                        leg_lot=z_base_lot,
                        engine_version=self.engine_version,
                    ),
                    rm,
                ),
                initial_capital_usd=self.initial_capital_usd,
            )
        else:
            self.calm_engine = CalmRegimeEngine(
                cfg=_scale_cfg_lots(
                    build_calm_regime_profile_config(
                        self.calm_profile,
                        leg_lot=self.calm_leg_lot,
                        max_legs=max(8, self.max_legs),
                    ),
                    rm,
                ),
                initial_capital_usd=self.initial_capital_usd,
            )
        self.engines = {"SHOCK10_DYN_RUN": self.shock_engine}
        if self.fast_shock_engine is not None:
            self.engines[self._fast_name] = self.fast_shock_engine
        self.engines["MACRO_WAVE_DYN"] = self.macro_engine
        self.engines[self.calm_profile] = self.calm_engine
        self.set_multiplier(self._multiplier)

    def reset_file_state(self) -> None:
        """Reset per-engine tick buffers between separate offline replay files while keeping account history."""
        self._init_engines()

    def set_multiplier(self, multiplier: float) -> None:
        self._multiplier = float(multiplier)
        for eng in self.engines.values():
            eng.cfg.multiplier = self._multiplier

    def seed_m1_bars(
        self,
        m1_bars: list[list[float]],
        *,
        server_now_ts: float,
        spread: float = 0.36,
    ) -> int:
        """Warm up ZSCORE_MR / CALM and VSNAP6 HTF macro context with strictly closed MT5 M1 bars."""
        cur_bar_id = int(server_now_ts // 60)
        half_sp = max(0.0, float(spread)) * 0.5
        closed: list[dict[str, float]] = []
        for row in m1_bars:
            if len(row) < 5:
                continue
            b_id = int(float(row[0]) // 60)
            if b_id >= cur_bar_id:
                continue
            closed.append({
                "bar_id": float(b_id),
                "o": round(float(row[1]) + half_sp, 4),
                "h": round(float(row[2]) + half_sp, 4),
                "l": round(float(row[3]) + half_sp, 4),
                "c": round(float(row[4]) + half_sp, 4),
            })
        if closed:
            self.calm_engine.seed_closed_bars(closed[-120:])
            if self.fast_shock_engine is not None:
                self.fast_shock_engine.seed_htf_bars(closed[-60:])
            self.seeded_m1_bars = len(closed[-120:])
        return self.seeded_m1_bars

    def floating_pnl_usd(self, bid: float, ask: float) -> float:
        total = 0.0
        for eng in self.engines.values():
            if eng.basket is not None:
                total += float(eng.unrealized_pnl_usd(bid, ask))
        return round(total, 2)

    def _update_combined_dd(self, bid: float, ask: float) -> None:
        eq = self.initial_capital_usd + self.realized_pnl_usd + self.floating_pnl_usd(bid, ask)
        if eq > self.peak_equity_usd:
            self.peak_equity_usd = eq
        self.max_dd_usd = round(max(self.max_dd_usd, self.peak_equity_usd - eq), 2)

    def _record_closed_wave(self, eng_name: str, wave: dict[str, Any]) -> dict[str, Any]:
        pnl = round(float(wave.get("net_pnl_usd", 0.0)), 2)
        self.realized_pnl_usd = round(self.realized_pnl_usd + pnl, 2)
        bal_after = round(self.initial_capital_usd + self.realized_pnl_usd, 2)
        if bal_after > self.peak_balance_usd:
            self.peak_balance_usd = bal_after
        self.closed_max_dd_usd = round(max(self.closed_max_dd_usd, self.peak_balance_usd - bal_after), 2)
        rec = dict(wave)
        rec["engine"] = eng_name
        rec["engine_label"] = ENGINE_LABELS.get(eng_name, eng_name)
        rec["balance_after_usd"] = bal_after
        rec["total_profit_after_usd"] = self.realized_pnl_usd
        self.trades_closed.append(rec)
        return rec

    def on_tick(
        self,
        *,
        ts: float,
        bid: float,
        ask: float,
        m1_context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        out_events: list[dict[str, Any]] = []
        coord_v4 = self.engine_version == "v4" and self.fast_shock_engine is not None
        tick_order = (
            [n for n in self.engine_order if n != "SHOCK10_DYN_RUN"] + ["SHOCK10_DYN_RUN"]
            if coord_v4
            else self.engine_order
        )
        for eng_name in tick_order:
            eng = self.engines[eng_name]
            had_shock_pend = (
                coord_v4 and eng_name == "SHOCK10_DYN_RUN" and self.shock_engine.pending_order is not None
            )
            if isinstance(eng, (FastSnapEngine, TickRobustZScoreEngine)):
                evs = eng.on_tick(ts=ts, bid=bid, ask=ask)
            else:
                evs = eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_context)
            # Bug #3 Fix: prevent SHOCK10 from piling into the peak of a spike VSNAP6 is already riding
            if (
                coord_v4
                and eng_name == "SHOCK10_DYN_RUN"
                and (not had_shock_pend)
                and self.shock_engine.pending_order is not None
                and self.shock_engine.pending_order.get("kind") == "open"
                and self.fast_shock_engine is not None
                and self.fast_shock_engine.basket is not None
            ):
                self.shock_engine.pending_order = None
            for ev in evs:
                ev_copy = dict(ev)
                ev_copy["engine"] = eng_name
                ev_copy["engine_label"] = ENGINE_LABELS.get(eng_name, eng_name)
                if ev_copy.get("type") == "CLOSE_ALL" and isinstance(ev_copy.get("wave"), dict):
                    rec = self._record_closed_wave(eng_name, ev_copy["wave"])
                    ev_copy["wave"] = rec
                out_events.append(ev_copy)
        self._update_combined_dd(bid, ask)
        return out_events

    def close_all_open(self, *, ts: float, bid: float, ask: float, reason: str = "eof_replay") -> list[dict[str, Any]]:
        closed_events: list[dict[str, Any]] = []
        for eng_name in self.engine_order:
            eng = self.engines[eng_name]
            if eng.basket is None:
                continue
            ex_px = bid if eng.basket["side"] == "buy" else ask
            if isinstance(eng, S1VelocitySurferEngine):
                wave = eng._close_basket(ex_px, ts, reason)
            elif isinstance(eng, (FastSnapEngine, TickRobustZScoreEngine)):
                wave = eng.force_close_open(ts=ts, bid=bid, ask=ask, reason=reason)
                if wave is None:
                    continue
            else:
                wave = eng._close(ex_px, ts, reason)
            rec = self._record_closed_wave(eng_name, wave)
            closed_events.append({
                "type": "CLOSE_ALL",
                "engine": eng_name,
                "engine_label": ENGINE_LABELS.get(eng_name, eng_name),
                "wave": rec,
            })
        self._update_combined_dd(bid, ask)
        return closed_events

    def open_positions(self, bid: float, ask: float) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for eng_name in self.engine_order:
            eng = self.engines[eng_name]
            b = eng.basket
            if b is None:
                continue
            rows.append({
                "engine": eng_name,
                "engine_label": ENGINE_LABELS.get(eng_name, eng_name),
                "side": str(b["side"]).upper(),
                "legs": len(b["legs"]),
                "volume": round(float(b["total_volume"]), 2),
                "avg_entry": round(float(b["avg_entry"]), 2),
                "stop": round(float(b["stop"]), 2),
                "stop_locked": bool(b.get("stop_locked", False)),
                "partial_taken": bool(b.get("partial_taken", False)),
                "banked_pnl_usd": round(float(b.get("banked_pnl_usd", 0.0)), 2),
                "pnl_usd": round(float(eng.unrealized_pnl_usd(bid, ask)), 2),
                "opened_utc": _iso_utc(float(b["opened_at"])),
            })
        return rows

    def account_summary(self, bid: float = 0.0, ask: float = 0.0) -> dict[str, Any]:
        flt = self.floating_pnl_usd(bid, ask) if bid > 0.0 and ask > 0.0 else 0.0
        bal = round(self.initial_capital_usd + self.realized_pnl_usd, 2)
        eq = round(bal + flt, 2)
        net_profit = round(eq - self.initial_capital_usd, 2)
        wins = [t for t in self.trades_closed if float(t["net_pnl_usd"]) > 0]
        losses = [t for t in self.trades_closed if float(t["net_pnl_usd"]) <= 0]
        gw = sum(float(t["net_pnl_usd"]) for t in wins)
        gl = abs(sum(float(t["net_pnl_usd"]) for t in losses))
        pf = round(gw / gl, 2) if gl > 0 else (99.0 if gw > 0 else None)
        if self.calm_profile == "ZSCORE_MR_24H" and self.enable_fast_shock6 and self.fast_shock_24h:
            ver_tag = "V4" if self.engine_version == "v4" else "V3"
            port_name = f"COMBO_AW_{ver_tag}_24H_VARLOT" if self.fast_shock_varlot else f"COMBO_AW_{ver_tag}_24H"
        elif self.enable_fast_shock6:
            port_name = "COMBO_AW_V2"
        else:
            port_name = "COMBO_AW_PRO_M1"
        return {
            "paper_only": True,
            "orders_submitted": 0,
            "portfolio": port_name,
            "risk_multiplier": round(float(self.risk_multiplier), 2),
            "initial_capital_usd": round(self.initial_capital_usd, 2),
            "balance_usd": bal,
            "equity_usd": eq,
            "floating_pnl_usd": flt,
            "realized_pnl_usd": round(self.realized_pnl_usd, 2),
            "net_profit_usd": net_profit,
            "max_dd_usd": round(self.closed_max_dd_usd, 2),
            "equity_max_dd_usd": round(self.max_dd_usd, 2),
            "trades_count": len(self.trades_closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(100.0 * len(wins) / len(self.trades_closed), 1) if self.trades_closed else 0.0,
            "pf": pf,
            "avg_win_usd": round(gw / len(wins), 2) if wins else 0.0,
            "avg_loss_usd": round(-gl / len(losses), 2) if losses else 0.0,
            "open_positions_count": sum(1 for e in self.engines.values() if e.basket is not None),
            "seeded_m1_bars": self.seeded_m1_bars,
        }

    def live_status(self, ts: float | None = None) -> dict[str, Any]:
        """Human-readable market radar status across all 3 engines."""
        cs = self.calm_engine.live_status(ts)
        m60 = float(cs.get("move_60s", 0.0))
        open_labels = [
            ENGINE_LABELS.get(name, name)
            for name in self.engine_order
            if self.engines[name].basket is not None
        ]
        if open_labels:
            headline = f"ACTIVE TRADE ({', '.join(open_labels)})"
        else:
            headline = str(cs["reason"])
        return {
            "headline": headline,
            "calm_profile": self.calm_profile,
            "in_session": bool(cs["in_session"]),
            "bars_ready": int(cs["bars_ready"]),
            "atr_m1": float(cs["atr"]),
            "move_60s": round(m60, 2),
            "move_15m": float(cs["htf_move"]),
            "eff_8m": float(cs["eff"]),
            "ema_dir": str(cs["ema_dir"]),
        }

    def snapshot(
        self,
        *,
        symbol: str,
        status: str,
        started_utc: str,
        elapsed_minutes: float,
        last_bid: float,
        last_ask: float,
        server_time_str: str,
        ticks_processed: int,
        ticks_recorded: int,
        reconnect_count: int,
        consecutive_errors: int,
    ) -> dict[str, Any]:
        acct = self.account_summary(last_bid, last_ask)
        by_engine: dict[str, dict[str, Any]] = {}
        for eng_name in self.engine_order:
            e_trades = [t for t in self.trades_closed if t["engine"] == eng_name]
            e_wins = [t for t in e_trades if float(t["net_pnl_usd"]) > 0]
            e_losses = [t for t in e_trades if float(t["net_pnl_usd"]) <= 0]
            eng = self.engines[eng_name]
            e_flt = round(float(eng.unrealized_pnl_usd(last_bid, last_ask)), 2) if eng.basket is not None else 0.0
            e_real = round(sum(float(t["net_pnl_usd"]) for t in e_trades), 2)
            by_engine[ENGINE_LABELS.get(eng_name, eng_name)] = {
                "strategy": eng_name,
                "trades": len(e_trades),
                "wins": len(e_wins),
                "losses": len(e_losses),
                "realized_pnl_usd": e_real,
                "floating_pnl_usd": e_flt,
                "net_profit_usd": round(e_real + e_flt, 2),
            }
        return {
            "schema": "all_weather_pro_m1_paper_v1",
            "paper_only": True,
            "orders_submitted": 0,
            "symbol": symbol,
            "status": status,
            "started_utc": started_utc,
            "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "server_time": server_time_str,
            "elapsed_minutes": round(elapsed_minutes, 2),
            "account": acct,
            "market_status": self.live_status(),
            "open_positions": self.open_positions(last_bid, last_ask),
            "by_engine": by_engine,
            "recent_trades": self.trades_closed[-50:],
            "live_state": {
                "bid": round(last_bid, 4),
                "ask": round(last_ask, 4),
                "spread": round(last_ask - last_bid, 4),
                "ticks_processed": ticks_processed,
                "ticks_recorded": ticks_recorded,
                "reconnect_count": reconnect_count,
                "consecutive_errors": consecutive_errors,
            },
        }
