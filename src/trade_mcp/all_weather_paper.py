"""All-Weather Paper Trading Suite (COMBO_AW_V2, ORDERS=0).

Combines the 4 causally validated engines into ONE unified MetaTrader-style paper account:
  1. SHOCK10_DYN_RUN  (News / >=$10 per 60s shock dynamic runner)
  2. VSNAP_SHOCK_6    (Fast sharp $6-$10/60s burst & V-snap runner — 77.8% WR, PF 2.23 on 322k real ticks)
  3. MACRO_WAVE_DYN   (London/NY macro pullback-continuation dynamic pyramid runner)
  4. CALM_M1_PRO_SESS (London/NY calm-drift M1 EMA9/21 + HTF alignment + 1L 55% ratchet)

Strictly read-only / paper-only (`paper_only=True`, `orders_submitted=0`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from trade_mcp.calm_regime_engines import CalmRegimeEngine, build_calm_regime_profile_config
from trade_mcp.fast_snap_engines import FastSnapEngine, fast_snap_profiles
from trade_mcp.s1_impulse_audit import PullbackDipEngine, pullback_dip_profiles
from trade_mcp.s1_velocity_surfer import S1VelocitySurferEngine, build_s1_profile_config


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _build_macro_wave_cfg(*, leg_lot: float = 0.01, max_legs: int = 6) -> Any:
    by_name = dict(pullback_dip_profiles(leg_lot=leg_lot, max_legs=min(4, max_legs)))
    return by_name["MACRO_WAVE_DYN"]


def _build_fast_shock6_cfg(*, leg_lot: float = 0.02) -> Any:
    by_name = dict(fast_snap_profiles(base_lot=leg_lot))
    return by_name["VSNAP_SHOCK_6"]


ENGINE_LABELS: dict[str, str] = {
    "SHOCK10_DYN_RUN": "NEWS_SHOCK",
    "VSNAP_SHOCK_6": "FAST_SHOCK6",
    "MACRO_WAVE_DYN": "MACRO_WAVE",
    "CALM_M1_PRO_SESS": "CALM_M1_PRO",
    "CALM_M1_PRO_150": "CALM_M1_150",
}


@dataclass
class AllWeatherPaperSuite:
    """Unified MetaTrader-style paper account driving COMBO_AW_V2 (ORDERS=0)."""

    initial_capital_usd: float = 1000.0
    news_leg_lot: float = 0.01
    calm_leg_lot: float = 0.02
    max_legs: int = 6
    calm_profile: str = "CALM_M1_PRO_SESS"
    enable_fast_shock6: bool = True

    shock_engine: S1VelocitySurferEngine = field(init=False)
    fast_shock_engine: FastSnapEngine | None = field(init=False)
    macro_engine: PullbackDipEngine = field(init=False)
    calm_engine: CalmRegimeEngine = field(init=False)
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
        self.peak_balance_usd = float(self.initial_capital_usd)
        self.peak_equity_usd = float(self.initial_capital_usd)
        self.engine_order = ["SHOCK10_DYN_RUN"]
        if self.enable_fast_shock6:
            self.engine_order.append("VSNAP_SHOCK_6")
        self.engine_order.extend(["MACRO_WAVE_DYN", self.calm_profile])
        self._init_engines()

    def _init_engines(self) -> None:
        self.shock_engine = S1VelocitySurferEngine(
            cfg=build_s1_profile_config("v2.6_SHOCK10_DYN", leg_lot=self.news_leg_lot, max_legs=self.max_legs),
            initial_capital_usd=self.initial_capital_usd,
        )
        self.fast_shock_engine = (
            FastSnapEngine(
                cfg=_build_fast_shock6_cfg(leg_lot=self.calm_leg_lot),
                initial_capital_usd=self.initial_capital_usd,
            )
            if self.enable_fast_shock6
            else None
        )
        self.macro_engine = PullbackDipEngine(
            cfg=_build_macro_wave_cfg(leg_lot=self.news_leg_lot, max_legs=self.max_legs),
            initial_capital_usd=self.initial_capital_usd,
        )
        self.calm_engine = CalmRegimeEngine(
            cfg=build_calm_regime_profile_config(self.calm_profile, leg_lot=self.calm_leg_lot, max_legs=max(8, self.max_legs)),
            initial_capital_usd=self.initial_capital_usd,
        )
        self.engines = {"SHOCK10_DYN_RUN": self.shock_engine}
        if self.fast_shock_engine is not None:
            self.engines["VSNAP_SHOCK_6"] = self.fast_shock_engine
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
        """Warm up CALM_M1_PRO_SESS with strictly closed historical M1 bars at live startup."""
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
        for eng_name in self.engine_order:
            eng = self.engines[eng_name]
            if isinstance(eng, FastSnapEngine):
                evs = eng.on_tick(ts=ts, bid=bid, ask=ask)
            else:
                evs = eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_context)
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
            elif isinstance(eng, FastSnapEngine):
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
        return {
            "paper_only": True,
            "orders_submitted": 0,
            "portfolio": "COMBO_AW_V2" if self.enable_fast_shock6 else "COMBO_AW_PRO_M1",
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
