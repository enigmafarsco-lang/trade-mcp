from trade_mcp.config import Config
from trade_mcp.risk import (
    cfd_contract_size_mismatch,
    has_blockers,
    loss_per_lot,
    lots_for_risk,
    min_stop_distance,
    normalise_volume,
    reward_risk,
    round_to_step,
    validate_order,
)

GOLD = {
    "digits": 2,
    "point": 0.01,
    "trade_tick_size": 0.01,
    "trade_tick_value": 1.0,
    "trade_contract_size": 100.0,
    "volume_min": 0.01,
    "volume_max": 100.0,
    "volume_step": 0.01,
    "spread_points": 25,
    "trade_stops_level": 0,
}

ACCOUNT = {"balance": 10_000.0, "equity": 10_000.0, "margin_free": 9_000.0}


def test_loss_per_lot_gold():
    # 1 lot of gold = 100 oz -> a $1 move is $100
    assert loss_per_lot(GOLD, 1.0) == 100.0
    assert loss_per_lot(GOLD, 10.0) == 1000.0


def test_lot_sizing_matches_risk_budget():
    # $10,000 account, 1% = $100 risk, stop $5 away -> 0.20 lots (capped to max 0.1)
    sizing = lots_for_risk(10_000, 1.0, 2650.0, 2645.0, GOLD, max_lot=None)
    assert sizing["ok"] is True
    assert abs(sizing["raw_volume"] - 0.2) < 1e-6
    assert abs(sizing["actual_risk"] - 100.0) < 0.5

    capped = lots_for_risk(10_000, 1.0, 2650.0, 2645.0, GOLD, max_lot=0.1)
    assert capped["volume"] == 0.1
    assert any("MAX_LOT" in w for w in capped["warnings"])


def test_lot_sizing_rejects_flat_stop():
    assert lots_for_risk(10_000, 1.0, 2650.0, 2650.0, GOLD)["ok"] is False


def test_volume_grid_and_minimum():
    assert round_to_step(0.0199, 0.01) == 0.01
    vol, warnings = normalise_volume(0.0005, GOLD)
    assert vol == 0.01 and warnings


def test_reward_risk():
    assert reward_risk(100, 95, 110) == 2.0
    assert reward_risk(100, 100, 110) is None


def test_min_stop_distance_and_validation_blocks_bad_orders():
    cfg = Config(allow_trading=True, training_budget=10000, max_lot=0.1, max_risk_percent=1.0)
    assert min_stop_distance(GOLD) == (25 + 2) * 0.01

    issues = validate_order(
        cfg=cfg,
        info=GOLD,
        symbol="XAUUSD",
        side="buy",
        volume=0.05,
        entry=2650.0,
        stop=2649.9,  # ~10 points, inside the spread
        take_profit=2650.2,
        account=ACCOUNT,
    )
    codes = {i["code"] for i in issues}
    assert "stop_too_close" in codes
    assert "tp_too_close" in codes
    assert has_blockers(issues)


def test_validation_blocks_oversized_risk_and_lot():
    cfg = Config(allow_trading=True, training_budget=10000, max_lot=0.1, max_risk_percent=1.0)
    issues = validate_order(
        cfg=cfg,
        info=GOLD,
        symbol="XAUUSD",
        side="buy",
        volume=0.5,
        entry=2650.0,
        stop=2640.0,  # $10 stop * 0.5 lot * $100 = $500 = 5% of equity
        take_profit=2670.0,
        account=ACCOUNT,
    )
    codes = {i["code"] for i in issues}
    assert "max_lot" in codes
    assert "risk_exceeded" in codes


def test_validation_passes_a_sane_order():
    cfg = Config(allow_trading=True, training_budget=10000, max_lot=0.1, max_risk_percent=1.0)
    issues = validate_order(
        cfg=cfg,
        info=GOLD,
        symbol="XAUUSD",
        side="buy",
        volume=0.02,
        entry=2650.0,
        stop=2645.0,
        take_profit=2660.0,
        account=ACCOUNT,
        open_positions=[],
        margin_required=500.0,
    )
    assert not has_blockers(issues), issues


def test_symbol_allowlist_matches_broker_suffixes():
    cfg = Config(allowed_symbols=("XAUUSD", "GOLD"))
    assert cfg.is_symbol_allowed("XAUUSD")
    assert cfg.is_symbol_allowed("XAUUSD.a")
    assert cfg.is_symbol_allowed("xauusdm")
    assert not cfg.is_symbol_allowed("EURUSD")
    assert not cfg.is_symbol_allowed("")


def test_blocked_sizing_explains_minimum_lot_cost():
    info = {"volume_min": .01, "volume_step": .01, "volume_max": 100,
            "trade_tick_size": .01, "trade_tick_value": 1}
    result = lots_for_risk(100, 1, 3000, 2980, info)
    assert result['ok'] is False
    assert result['volume'] == 0
    assert result['risk_amount'] == 1
    assert result['minimum_volume'] == .01
    assert result['minimum_volume_loss'] == 20
    assert result['stop_distance'] == 20
    assert result['raw_volume'] == .0005


def test_cfd_leverage_contract_size_guard_prevents_10x_oversizing():
    # Alpari MT5 XAUUSD_i exact Specification: contract_size=100, profit_currency=USD,
    # calc_mode="CFD Leverage", tick_size=0.01, tick_value=0.1 (10x understated).
    alpari_gold = {
        **GOLD,
        "trade_tick_size": 0.01,
        "trade_tick_value": 0.1,
        "trade_contract_size": 100.0,
        "currency_profit": "USD",
        "trade_calc_mode": "CFD Leverage",
    }
    mismatch = cfd_contract_size_mismatch(alpari_gold)
    assert mismatch is not None and mismatch["ratio"] == 10.0 and mismatch["contract_size"] == 100.0
    assert loss_per_lot(alpari_gold, 1.0) == 100.0
    assert loss_per_lot(alpari_gold, 10.0) == 1000.0

    # Under $10 demo cash policy, 0.10 lot with $10 stop is $100 real loss -> MUST be blocked!
    cfg = Config(allow_trading=True, demo_cash_policy=True, training_budget=1000, max_lot=0.1, max_risk_percent=1.0)
    demo_account = {"balance": 1000.0, "equity": 1000.0, "margin_free": 900.0, "trade_mode": "demo", "currency": "USD"}
    issues = validate_order(
        cfg=cfg, info=alpari_gold, symbol="XAUUSD_i", side="buy",
        volume=0.10, entry=3621.92, stop=3611.92, take_profit=3623.92, account=demo_account,
    )
    codes = {i["code"] for i in issues}
    assert "cash_loss_limit" in codes
    assert "cfd_contract_size_override" in codes

    # Non-USD profit currency (USDJPY) or mild FX conversion (<1.5x) must NOT be overridden
    usdjpy = {
        "point": 0.001, "trade_tick_size": 0.001, "trade_tick_value": 0.65,
        "trade_contract_size": 100000.0, "currency_profit": "JPY", "trade_calc_mode": "Forex",
    }
    assert cfd_contract_size_mismatch(usdjpy) is None
    assert loss_per_lot(usdjpy, 1.0) == 650.0

    eur_acct_gold = {
        **GOLD, "trade_tick_value": 0.91, "trade_contract_size": 100.0,
        "currency_profit": "USD", "trade_calc_mode": "CFD Leverage",
    }
    assert cfd_contract_size_mismatch(eur_acct_gold) is None
    assert loss_per_lot(eur_acct_gold, 1.0) == 91.0

