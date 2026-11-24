"""Hermetic offline tests for multi-symbol cross-asset generalization & portfolio lab."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from trade_mcp.multi_symbol_lab import (
    estimate_margin_per_lot_usd,
    select_candidate_symbols,
)


def test_select_candidate_symbols_prefers_alpari_suffix_and_diversity() -> None:
    pool = [
        "EURUSD", "EURUSD_i", "GBPUSD_i", "XAUUSD", "XAUUSD_i",
        "XAGUSD_i", "USDJPY_i", "GBPJPY_i", "USTEC", "US30", "USOIL", "BTCUSD",
    ]
    picked = select_candidate_symbols(pool, max_symbols=10)
    assert "XAUUSD_i" in picked and "XAUUSD" not in picked
    assert "EURUSD_i" in picked and "EURUSD" not in picked
    assert "USDJPY_i" in picked
    assert "USTEC" in picked
    assert len(picked) == 10


def test_estimate_margin_per_lot_across_asset_classes() -> None:
    gold = {"point": 0.01, "currency_base": "XAU", "currency_profit": "USD", "trade_contract_size": 100.0, "trade_tick_size": 0.01, "trade_tick_value": 0.10}
    eur = {"point": 0.00001, "currency_base": "EUR", "currency_profit": "USD", "trade_contract_size": 100000.0, "trade_tick_size": 0.00001, "trade_tick_value": 1.0}
    jpy = {"point": 0.001, "currency_base": "USD", "currency_profit": "JPY", "trade_contract_size": 100000.0, "trade_tick_size": 0.001, "trade_tick_value": 0.65}

    # 1 lot at 1:500 leverage:
    # Gold @ $3600 => $360k / 500 = $720 ($7.20 per 0.01 lot)
    assert 700.0 <= estimate_margin_per_lot_usd(gold, 3600.0, 500.0) <= 740.0
    # EURUSD @ 1.08 => $108k / 500 = $216 ($2.16 per 0.01 lot)
    assert 200.0 <= estimate_margin_per_lot_usd(eur, 1.08, 500.0) <= 230.0
    # USDJPY @ 154 => $100k / 500 = $200 ($2.00 per 0.01 lot)
    assert 190.0 <= estimate_margin_per_lot_usd(jpy, 154.0, 500.0) <= 210.0


def test_multisymbol_cli_mock_smoke() -> None:
    out_file = Path("logs/_pytest_multisymbol_smoke.json")
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/daytrade_multi_symbol_lab.py",
                "--mock",
                "--mc-trials",
                "50",
                "--summary",
                "--out",
                str(out_file),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "ORDERS=0" in proc.stdout
        assert "XAUUSD_i" in proc.stdout
        assert "EURUSD_i" in proc.stdout
        assert "USDJPY_i" in proc.stdout
        assert out_file.exists() and (out_file.stat().st_mode & 0o777) == 0o600
    finally:
        if out_file.exists():
            out_file.unlink()


def test_inspect_symbol_live_subscribes_hidden_symbol(monkeypatch) -> None:
    scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import daytrade_multi_symbol_lab as cli  # noqa: E402

    state = {"subscribed": False, "calls": []}

    class FakeClient:
        def call_tool(self, name, args):
            state["calls"].append((name, args))
            if name == "add_marketwatch_symbol":
                state["subscribed"] = True
            return {"ok": True}

    class FakeFeed:
        client = FakeClient()

        def _call(self, name, args):
            assert name == "get_marketwatch_symbols"
            if not state["subscribed"]:
                return {"symbols": [{"name": "EURUSD_i", "bid": 0.0, "ask": 0.0, "point": 0.00001, "trade_tick_value": 0.0}]}
            return {
                "symbols": [{
                    "name": "EURUSD_i",
                    "bid": 1.0810,
                    "ask": 1.08112,
                    "point": 0.00001,
                    "trade_tick_size": 0.00001,
                    "trade_tick_value": 1.0,
                    "trade_contract_size": 100000.0,
                    "currency_base": "EUR",
                    "currency_profit": "USD",
                    "volume_min": 0.01,
                    "volume_max": 50.0,
                    "volume_step": 0.01,
                    "trade_stops_level": 0.0,
                }]
            }

    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    spec, q = cli._inspect_symbol_live(FakeFeed(), "EURUSD_i")  # type: ignore[arg-type]
    assert state["subscribed"] is True
    assert ("add_marketwatch_symbol", {"symbol": "EURUSD_i"}) in state["calls"]
    assert q["bid"] == 1.0810
    assert spec["currency_base"] == "EUR"

