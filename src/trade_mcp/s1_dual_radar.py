"""Live Shock Radar + Side-by-Side Dual Paper Suite (v2.5 Shock + Pullback-Dip V2, ORDERS=0).

Combines both validated high-selectivity systems on a single 0.30s Bid/Ask tick stream:
1. ShockRadar: 24/7 sleep-mode tape monitor that wakes ONLY on genuine high-efficiency
   accelerations (PRE_SHOCK >= $6, SHOCK_8 >= $8 & eff>=0.45, SHOCK_10 >= $10 & eff>=0.50),
   with rollover/reconnect gap immunity and per-direction alert cooldown + escalation.
2. Side-by-side strictly virtual Paper engines (independent $1,000 accounts, ORDERS=0):
   - v2.5_SHOCK_10         (exact validated 100% WR / $0 DD shock gate)
   - v2.5_SHOCK_10_RUNNER  (same $10/60s gate with shock-scaled trail breathing room)
   - v2.5_SHOCK_8          (wider $8/60s, eff>=0.45 shock sample collector)
   - PB_DIP_V2_SESS        (Pullback-Dip V2 with pivot SL, profit-lock, trend eff, London/NY hours 09-19 server)
   - PB_DIP_V2_24H         (Pullback-Dip V2 24h shadow control to verify session filter OOS)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from trade_mcp.s1_impulse_audit import (
    ALPARI_LONDON_NY_HOURS,
    PullbackDipEngine,
    build_pullback_dip_profile_config,
    combine_portfolio_waves,
)
from trade_mcp.s1_velocity_surfer import (
    S1VelocitySurferEngine,
    build_s1_profile_config,
)
from trade_mcp.s10_hybrid_engines import (
    S10HybridEngine,
    build_s10_hybrid_profile_config,
)


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ShockRadarConfig:
    """Strictly causal 3-tier shock radar thresholds with disconnect/rollover gap protection."""

    pre_shock_60s: float = 6.0
    pre_shock_eff: float = 0.40
    shock8_60s: float = 8.0
    shock8_eff: float = 0.45
    shock10_60s: float = 10.0
    shock10_eff: float = 0.50
    min_s1_bars_60s: int = 10  # blocks fake shocks across reconnect/rollover gaps
    min_updates_10s: int = 6  # requires live quote surge, not 1 stale jump tick
    max_gap_sec: float = 15.0  # resets rolling 60s tape buffer cleanly after disconnects
    max_spread_price: float = 1.20  # ignores wide rollover spread spikes
    alert_cooldown_sec: float = 60.0  # anti-spam cooldown per same tier & direction
    active_server_hours: tuple[int, ...] = ALPARI_LONDON_NY_HOURS


@dataclass
class ShockRadar:
    """24/7 causal shock detector that sleeps through noise and fires crisp alerts on true acceleration."""

    cfg: ShockRadarConfig = field(default_factory=ShockRadarConfig)

    _s1_bars: deque[dict[str, float]] = field(default_factory=deque)
    _cur_s1: dict[str, float] | None = None
    _ticks_3s: deque[tuple[float, float]] = field(default_factory=deque)
    _price_change_ts: deque[float] = field(default_factory=deque)
    _prev_quote: tuple[float, float] | None = None
    _last_ts: float = 0.0

    _last_alert_ts: float = -1e18
    _last_alert_level: int = 0
    _last_alert_dir: int = 0
    _next_alert_id: int = 1

    state: str = "SLEEP"
    alerts_history: list[dict[str, Any]] = field(default_factory=list)
    alerts_by_tier: dict[str, int] = field(
        default_factory=lambda: {"PRE_SHOCK": 0, "SHOCK_8": 0, "SHOCK_10": 0}
    )
    last_metrics: dict[str, Any] = field(default_factory=dict)

    def _reset_tape(self) -> None:
        self._s1_bars.clear()
        self._cur_s1 = None
        self._ticks_3s.clear()
        self._price_change_ts.clear()
        self._prev_quote = None
        self._last_alert_level = 0
        self.state = "SLEEP"

    def _tape_60s(self, ts: float, mid: float) -> tuple[float, float, int, bool]:
        if not self._s1_bars:
            return 0.0, 0.0, 0, False
        cutoff = int(ts) - 60
        ref_bar: dict[str, float] | None = None
        path = 0.0
        last_c = mid
        bars_n = 0
        for bar in self._s1_bars:
            if int(bar["sec"]) >= cutoff:
                if ref_bar is None:
                    ref_bar = bar
                path += abs(float(bar["body"]))
                last_c = float(bar["c"])
                bars_n += 1
        if ref_bar is None:
            return 0.0, 0.0, 0, False
        span = int(ts) - int(ref_bar["sec"])
        if span < 20 or bars_n < self.cfg.min_s1_bars_60s:
            return 0.0, 0.0, bars_n, False
        path += abs(mid - last_c)
        move = mid - float(ref_bar["o"])
        eff = (abs(move) / path) if path > 1e-9 else 0.0
        return round(move, 3), round(min(1.0, eff), 3), bars_n, True

    def _move_3s(self, ts: float, mid: float) -> float:
        if len(self._ticks_3s) < 2:
            return 0.0
        cut = ts - 3.0
        ref = self._ticks_3s[0][1]
        for t_ts, t_mid in self._ticks_3s:
            if t_ts >= cut:
                ref = t_mid
                break
        return round(mid - ref, 3)

    def on_tick(
        self,
        *,
        ts: float,
        bid: float,
        ask: float,
        m1_context: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Process one live Bid/Ask tick; return a structured alert dict ONLY when a shock triggers."""
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return None

        if self._last_ts > 0.0 and (ts - self._last_ts) > self.cfg.max_gap_sec:
            self._reset_tape()
        self._last_ts = ts

        mid = 0.5 * (bid + ask)
        spread = round(ask - bid, 5)
        sec = int(ts)
        srv_hour = (sec // 3600) % 24
        in_london_ny = srv_hour in self.cfg.active_server_hours

        # Track live quote updates over 10s
        if self._prev_quote is None or bid != self._prev_quote[0] or ask != self._prev_quote[1]:
            self._price_change_ts.append(ts)
            self._prev_quote = (bid, ask)
        while self._price_change_ts and (ts - self._price_change_ts[0]) > 10.0:
            self._price_change_ts.popleft()
        updates_10s = len(self._price_change_ts)

        # Track 3s tick buffer
        self._ticks_3s.append((ts, mid))
        while self._ticks_3s and (ts - self._ticks_3s[0][0]) > 4.0:
            self._ticks_3s.popleft()

        # Update 1-second candle tape
        if self._cur_s1 is None:
            self._cur_s1 = {"sec": float(sec), "o": mid, "h": mid, "l": mid, "c": mid}
        elif sec > int(self._cur_s1["sec"]):
            prev = self._cur_s1
            prev["body"] = round(prev["c"] - prev["o"], 5)
            self._s1_bars.append(prev)
            while len(self._s1_bars) > 75:
                self._s1_bars.popleft()
            self._cur_s1 = {
                "sec": float(sec),
                "o": prev["c"],
                "h": max(prev["c"], mid),
                "l": min(prev["c"], mid),
                "c": mid,
            }
        else:
            self._cur_s1["h"] = max(self._cur_s1["h"], mid)
            self._cur_s1["l"] = min(self._cur_s1["l"], mid)
            self._cur_s1["c"] = mid

        move_60s, eff_60s, bars_60s, has_60s = self._tape_60s(ts, mid)
        move_3s = self._move_3s(ts, mid)
        abs_m60 = abs(move_60s)
        direction = 1 if move_60s > 0 else (-1 if move_60s < 0 else 0)

        tier = "SLEEP"
        level = 0
        if (
            has_60s
            and direction != 0
            and spread <= self.cfg.max_spread_price
            and updates_10s >= self.cfg.min_updates_10s
        ):
            if abs_m60 >= self.cfg.shock10_60s and eff_60s >= self.cfg.shock10_eff:
                tier = "SHOCK_10"
                level = 3
            elif abs_m60 >= self.cfg.shock8_60s and eff_60s >= self.cfg.shock8_eff:
                tier = "SHOCK_8"
                level = 2
            elif abs_m60 >= self.cfg.pre_shock_60s and eff_60s >= self.cfg.pre_shock_eff:
                tier = "PRE_SHOCK"
                level = 1

        self.state = tier
        self.last_metrics = {
            "ts": ts,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "move_60s": round(move_60s, 2),
            "eff_60s": round(eff_60s, 2),
            "move_3s": round(move_3s, 2),
            "bars_60s": bars_60s,
            "updates_10s": updates_10s,
            "server_hour": srv_hour,
            "in_london_ny": in_london_ny,
            "state": tier,
            "m1_label": str(m1_context.get("label", "UNKNOWN")),
            "m1_bias": int(m1_context.get("bias", 0)),
        }

        # Reset escalation memory once tape cools back down
        if level == 0:
            if abs_m60 < (self.cfg.pre_shock_60s * 0.60) and (ts - self._last_alert_ts) >= 15.0:
                self._last_alert_level = 0
            return None

        cooldown_done = (ts - self._last_alert_ts) >= self.cfg.alert_cooldown_sec
        escalated = level > self._last_alert_level and direction == self._last_alert_dir
        reversed_dir = (self._last_alert_dir != 0) and (direction != self._last_alert_dir)

        if not (cooldown_done or escalated or reversed_dir):
            return None

        alert = {
            "alert_id": self._next_alert_id,
            "tier": tier,
            "tier_level": level,
            "side": "BUY" if direction == 1 else "SELL",
            "ts": round(ts, 3),
            "server_time": _iso_utc(ts),
            "server_hour": srv_hour,
            "in_london_ny": in_london_ny,
            "bid": round(bid, 4),
            "ask": round(ask, 4),
            "spread": spread,
            "move_60s": round(move_60s, 2),
            "eff_60s": round(eff_60s, 2),
            "move_3s": round(move_3s, 2),
            "updates_10s": updates_10s,
            "m1_label": str(m1_context.get("label", "UNKNOWN")),
            "m1_bias": int(m1_context.get("bias", 0)),
            "support": m1_context.get("support"),
            "resistance": m1_context.get("resistance"),
            "escalated": bool(escalated and not cooldown_done),
        }
        self._next_alert_id += 1
        self._last_alert_ts = ts
        self._last_alert_level = level
        self._last_alert_dir = direction
        self.alerts_by_tier[tier] = self.alerts_by_tier.get(tier, 0) + 1
        self.alerts_history.append(alert)
        if len(self.alerts_history) > 200:
            self.alerts_history = self.alerts_history[-200:]
        return alert


class DualRadarPaperSuite:
    """Runs ShockRadar + 5 core (or 9 with S10 VSA/SR/EMA hybrids) Paper engines side-by-side (ORDERS=0)."""

    ENGINE_ORDER: tuple[str, ...] = (
        "v2.5_SHOCK_10",
        "v2.5_SHOCK_10_RUNNER",
        "v2.5_SHOCK_8",
        "PB_DIP_V2_SESS",
        "PB_DIP_V2_24H",
    )
    HYBRID_ENGINE_ORDER: tuple[str, ...] = (
        "SR_SWEEP_VSA_24H",
        "SR_SWEEP_VSA_SESS",
        "SR_BREAK_VSA_S10",
        "EMA_RECLAIM_S10",
    )

    def __init__(
        self,
        *,
        initial_capital_usd: float = 1000.0,
        leg_lot: float = 0.01,
        max_legs: int = 6,
        radar_cfg: ShockRadarConfig | None = None,
        include_hybrids: bool = False,
    ) -> None:
        self.initial_capital_usd = float(initial_capital_usd)
        self.leg_lot = float(leg_lot)
        self.max_legs = int(max_legs)
        self.include_hybrids = bool(include_hybrids)
        self.radar = ShockRadar(cfg=radar_cfg or ShockRadarConfig())
        dip_max_legs = min(4, self.max_legs)
        self.engine_order: tuple[str, ...] = (
            self.ENGINE_ORDER + self.HYBRID_ENGINE_ORDER if self.include_hybrids else self.ENGINE_ORDER
        )
        self.engines: dict[str, S1VelocitySurferEngine | PullbackDipEngine | S10HybridEngine] = {
            "v2.5_SHOCK_10": S1VelocitySurferEngine(
                cfg=build_s1_profile_config("v2.5_SHOCK_10", leg_lot=self.leg_lot, max_legs=self.max_legs),
                initial_capital_usd=self.initial_capital_usd,
            ),
            "v2.5_SHOCK_10_RUNNER": S1VelocitySurferEngine(
                cfg=build_s1_profile_config("v2.5_SHOCK_10_RUNNER", leg_lot=self.leg_lot, max_legs=self.max_legs),
                initial_capital_usd=self.initial_capital_usd,
            ),
            "v2.5_SHOCK_8": S1VelocitySurferEngine(
                cfg=build_s1_profile_config("v2.5_SHOCK_8", leg_lot=self.leg_lot, max_legs=self.max_legs),
                initial_capital_usd=self.initial_capital_usd,
            ),
            "PB_DIP_V2_SESS": PullbackDipEngine(
                cfg=build_pullback_dip_profile_config("PB_DIP_V2_SESS", leg_lot=self.leg_lot, max_legs=dip_max_legs),
                initial_capital_usd=self.initial_capital_usd,
            ),
            "PB_DIP_V2_24H": PullbackDipEngine(
                cfg=build_pullback_dip_profile_config("PB_DIP_V2_24H", leg_lot=self.leg_lot, max_legs=dip_max_legs),
                initial_capital_usd=self.initial_capital_usd,
            ),
        }
        if self.include_hybrids:
            hyb_max_legs = min(3, self.max_legs)
            for hname in self.HYBRID_ENGINE_ORDER:
                self.engines[hname] = S10HybridEngine(
                    cfg=build_s10_hybrid_profile_config(hname, leg_lot=self.leg_lot, max_legs=hyb_max_legs),
                    initial_capital_usd=self.initial_capital_usd,
                )

    def set_multiplier(self, multiplier: float) -> None:
        for eng in self.engines.values():
            eng.cfg.multiplier = float(multiplier)

    def on_tick(
        self,
        *,
        ts: float,
        bid: float,
        ask: float,
        m1_context: dict[str, Any],
    ) -> dict[str, Any]:
        alert = self.radar.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_context)
        engine_events: list[tuple[str, dict[str, Any]]] = []
        for name in self.engine_order:
            eng = self.engines[name]
            for ev in eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_context):
                engine_events.append((name, ev))
        return {"radar_alert": alert, "engine_events": engine_events}

    def close_all_open(
        self,
        *,
        ts: float,
        bid: float,
        ask: float,
        reason: str = "session_stop",
    ) -> list[tuple[str, dict[str, Any]]]:
        closed_events: list[tuple[str, dict[str, Any]]] = []
        if bid <= 0.0 or ask <= 0.0:
            return closed_events
        for name in self.engine_order:
            eng = self.engines[name]
            if eng.basket is None:
                continue
            ex = bid if eng.basket["side"] == "buy" else ask
            if isinstance(eng, S1VelocitySurferEngine):
                w = eng._close_basket(ex, ts, reason)
            else:
                w = eng._close(ex, ts, reason)
            closed_events.append((name, {"type": "CLOSE_ALL", "wave": w}))
        return closed_events

    def snapshot(
        self,
        *,
        symbol: str,
        status: str,
        started_utc: str,
        elapsed_minutes: float,
        m1_ctx: dict[str, Any],
        last_bid: float,
        last_ask: float,
        ticks_processed: int,
        ticks_recorded: int,
        reconnect_count: int,
        consecutive_errors: int,
    ) -> dict[str, Any]:
        eng_payload: dict[str, Any] = {}
        for name in self.engine_order:
            eng = self.engines[name]
            s = eng.summary(last_bid, last_ask)
            wins = [w["net_pnl_usd"] for w in eng.waves_closed if w["net_pnl_usd"] > 0]
            losses = [w["net_pnl_usd"] for w in eng.waves_closed if w["net_pnl_usd"] <= 0]
            s["avg_win_usd"] = round(sum(wins) / len(wins), 2) if wins else 0.0
            s["avg_loss_usd"] = round(sum(losses) / len(losses), 2) if losses else 0.0
            open_b: dict[str, Any] | None = None
            if eng.basket is not None:
                b = eng.basket
                open_b = {
                    "wave_id": b["wave_id"],
                    "side": b["side"],
                    "regime": b.get("regime", ""),
                    "legs_count": len(b["legs"]),
                    "total_volume": b["total_volume"],
                    "avg_entry": b["avg_entry"],
                    "stop": b["stop"],
                    "stop_locked": b["stop_locked"],
                    "m1_label": b["m1_label"],
                }
            eng_payload[name] = {
                "summary": {k: v for k, v in s.items() if k not in ("open_basket", "last_metrics")},
                "open_basket": open_b,
                "waves": eng.waves_closed,
            }

        combo = combine_portfolio_waves(
            "COMBO_SHOCK10_RUNNER_PLUS_PB_DIP_V2_SESS",
            [
                self.engines["v2.5_SHOCK_10_RUNNER"].waves_closed,
                self.engines["PB_DIP_V2_SESS"].waves_closed,
            ],
        )
        combo_compact = {k: v for k, v in combo.items() if k != "waves"}

        return {
            "schema": "s1_dual_radar_live_v1",
            "orders_submitted": 0,
            "paper_only": True,
            "status": status,
            "symbol": symbol,
            "started_utc": started_utc,
            "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "elapsed_minutes": round(elapsed_minutes, 2),
            "radar": {
                "state": self.radar.state,
                "alerts_by_tier": dict(self.radar.alerts_by_tier),
                "total_alerts": sum(self.radar.alerts_by_tier.values()),
                "last_metrics": dict(self.radar.last_metrics),
                "recent_alerts": self.radar.alerts_history[-50:],
            },
            "live_state": {
                "ticks_processed": ticks_processed,
                "ticks_recorded": ticks_recorded,
                "last_bid": round(last_bid, 4),
                "last_ask": round(last_ask, 4),
                "last_spread": round(max(0.0, last_ask - last_bid), 4),
                "last_m1_label": m1_ctx.get("label", ""),
                "last_m1_bias": m1_ctx.get("bias", 0),
                "reconnect_count": reconnect_count,
                "consecutive_errors": consecutive_errors,
            },
            "combined_primary": combo_compact,
            "engines": eng_payload,
        }


def make_synthetic_dual_session() -> list[tuple[float, float, float, dict[str, Any]]]:
    """Deterministic offline session exercising ShockRadar + v2.5_SHOCK_* + PB_DIP_V2_SESS + off-session block.

    Timeline (broker server clock):
      - Phase 1 (14:00 server = Active London/NY, 0..420s):
          Smooth +$12 bull trend over 300s -> -$3.00 pullback dip -> +$1.25 S1 bounce -> +$10 rally -> -$3.20 exit.
          Triggers PB_DIP_V2_SESS & PB_DIP_V2_24H in profit.
      - Phase 2 (14:07 server = Active London/NY, 420..560s):
          40s calm baseline -> clean +$13.50/48s straight-line shock acceleration + C3 burst -> +$6 continuation -> fade.
          Triggers ShockRadar (PRE_SHOCK -> SHOCK_8 -> SHOCK_10) AND v2.5_SHOCK_8 / v2.5_SHOCK_10 / v2.5_SHOCK_10_RUNNER.
      - Phase 3 (03:00 server = Overnight Asia off-session, +13h jump -> clean gap reset):
          Same smooth +$12 bull trend + -$3.00 dip + +$1.25 bounce -> verifies PB_DIP_V2_SESS stays asleep
          while PB_DIP_V2_24H shadows it!
    """
    bull_ctx = {"bias": 1, "label": "BULL_TREND", "support": 3450.0, "resistance": 3620.0, "atr_m1": 2.4}
    spread = 0.36
    ticks: list[tuple[float, float, float, dict[str, Any]]] = []

    # Anchor exactly at 2026-11-26T14:00:00Z (server hour 14 = inside ALPARI_LONDON_NY_HOURS 09..19)
    t_active = float(datetime(2026, 11, 26, 14, 0, 0, tzinfo=timezone.utc).timestamp())
    bid = 3480.00
    for step in range(4 * 420):
        sec = step * 0.25
        if sec < 300.0:
            bid = round(3480.00 + (12.00 * sec / 300.0), 2)
        elif sec < 320.0:
            bid = round(3492.00 - (3.00 * (sec - 300.0) / 20.0), 2)
        elif sec < 324.0:
            bid = round(3489.00 + (1.30 * (sec - 320.0) / 4.0), 2)
        elif sec < 375.0:
            bid = round(3490.30 + (10.50 * (sec - 324.0) / 51.0), 2)
        elif sec < 395.0:
            bid = round(3500.80 - (3.60 * (sec - 375.0) / 20.0), 2)
        else:
            bid = round(3497.20 + (0.04 if (step % 4 < 2) else -0.04), 2)
        ticks.append((round(t_active + sec, 3), bid, round(bid + spread, 2), bull_ctx))

    # Phase 2: 40s calm baseline, then clean straight-line +$14/45s shock with strong C3 burst
    t_shock = t_active + 420.0
    base_bid = 3497.20
    bid = base_bid
    for step in range(4 * 140):
        sec = step * 0.25
        if sec < 40.0:
            bid = round(base_bid + (0.04 if (step % 4 < 2) else -0.04), 2)
        elif sec < 78.0:
            # Smooth straight-line acceleration +$11.40 over 38s (+$0.30/s) -> crosses $6, $8, $10 with eff ~ 1.0!
            bid = round(bid + 0.075, 2)
        elif sec < 85.0:
            # Strong 3s C3 burst (+$0.64/s) -> triggers v2.5_SHOCK_8 / v2.5_SHOCK_10 / v2.5_SHOCK_10_RUNNER!
            bid = round(bid + 0.16, 2)
        elif sec < 110.0:
            # Clean runner continuation (+$8.00) -> pyramids and locks profit
            bid = round(bid + 0.08, 2)
        else:
            # Sharp -$5.50 reversal -> clean trail exit
            bid = round(bid - 0.12, 2)
        ticks.append((round(t_shock + sec, 3), bid, round(bid + spread, 2), bull_ctx))

    # Phase 3: Off-session 03:00 server hour (next day) — same dip pattern blocked by PB_DIP_V2_SESS!
    t_off = float(datetime(2026, 11, 27, 3, 0, 0, tzinfo=timezone.utc).timestamp())
    bid = 3510.00
    for step in range(4 * 390):
        sec = step * 0.25
        if sec < 300.0:
            bid = round(3510.00 + (12.00 * sec / 300.0), 2)
        elif sec < 320.0:
            bid = round(3522.00 - (3.00 * (sec - 300.0) / 20.0), 2)
        elif sec < 324.0:
            bid = round(3519.00 + (1.30 * (sec - 320.0) / 4.0), 2)
        elif sec < 365.0:
            bid = round(3520.30 + (8.00 * (sec - 324.0) / 41.0), 2)
        else:
            bid = round(3528.30 - (4.00 * (sec - 365.0) / 25.0), 2)
        ticks.append((round(t_off + sec, 3), bid, round(bid + spread, 2), bull_ctx))

    return ticks
