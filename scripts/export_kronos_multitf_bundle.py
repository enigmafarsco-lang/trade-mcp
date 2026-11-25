#!/usr/bin/env python3
"""Strictly read-only multi-timeframe (M1..H4) redacted OHLCV exporter for Kronos Colab.

Connects read-only to local MT5 MCP (ORDERS=0), exports compact redacted OHLCV bars for
M1, M5, M15, H1, and H4 (resampled from H1) with live spread & multiplier ONLY (zero
account/login/balance/token fields) into an owner-only (0600) JSON file inside logs/.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from daytrade_2yr_oos_and_github_lab import _paginate_period_retry  # noqa: E402
from daytrade_m1_paper_window import (  # noqa: E402
    ReadOnlyNativeFeed,
    _save_private_json,
    enrich_spec,
)
from daytrade_mt5_readonly_check import (  # noqa: E402
    Blocked,
    exact_symbol,
    loopback_native,
    reported_spec,
    timestamp,
)
from daytrade_mt5_readonly_check import quote as parse_quote  # noqa: E402

from trade_mcp.bridge.native import McpError, find  # noqa: E402
from trade_mcp.candlestick_lab import (  # noqa: E402
    make_synthetic_bars_with_embedded_edge,
    resample_ohlcv,
)
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.daytrade_paper import resolve_pricing_basis  # noqa: E402
from trade_mcp.kronos_multitf_eval import FORBIDDEN_REDACTION_KEYS  # noqa: E402
from trade_mcp.m1_momentum import clean_ohlcv_bars  # noqa: E402


def _compact_rows(bars: list[list[float]], max_bars: int) -> list[list[float]]:
    clean = clean_ohlcv_bars(bars)
    if max_bars > 0 and len(clean) > max_bars:
        clean = clean[-max_bars:]
    return [[int(b[0]), round(float(b[1]), 5), round(float(b[2]), 5), round(float(b[3]), 5), round(float(b[4]), 5), round(float(b[5]), 2)] for b in clean]


def assert_redacted_bundle(bundle: dict[str, Any]) -> None:
    blob = json.dumps(bundle).lower()
    found = [k for k in FORBIDDEN_REDACTION_KEYS if f'"{k}"' in blob]
    if found:
        raise ValueError(f"Bundle contains forbidden sensitive keys: {found}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only redacted M1..H4 OHLCV exporter for Kronos Colab.")
    parser.add_argument("--symbol", default="XAUUSD_i")
    parser.add_argument("--m1-days", type=int, default=14, help="M1 history days (default 14 -> ~6000 bars)")
    parser.add_argument("--m5-days", type=int, default=45, help="M5 history days (default 45 -> ~6000 bars)")
    parser.add_argument("--m15-days", type=int, default=150, help="M15 history days (default 150 -> ~6000 bars)")
    parser.add_argument("--h1-days", type=int, default=1095, help="H1 history days (default 1095=3yr -> H1+H4)")
    parser.add_argument("--max-bars-per-tf", type=int, default=6000, help="Keep up to N most-recent bars per TF")
    parser.add_argument("--mock", action="store_true", help="Offline deterministic mock export")
    parser.add_argument("--out", default="", help="Output JSON inside logs/")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"
    t0 = time.monotonic()

    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_sym = "".join(ch for ch in args.symbol if ch.isalnum() or ch in ("_", "-")) or "SYMBOL"
    out_path = Path(args.out) if args.out else (ROOT / "logs" / f"kronos_multitf_{safe_sym}.json")
    if out_path.exists():
        stem, suf = out_path.stem, out_path.suffix or ".json"
        out_path = out_path.with_name(f"{stem}_{ts_tag}{suf}")

    try:
        tf_data: dict[str, list[list[float]]] = {}
        if args.mock:
            spec: dict[str, Any] = {
                "point": 0.01, "trade_tick_size": 0.01, "trade_tick_value": 0.10,
                "trade_contract_size": 100.0, "currency_base": "XAU", "currency_profit": "USD",
            }
            live_spread = 0.32
            for tf_name, sec in (("M1", 60), ("M5", 300), ("M15", 900), ("H1", 3600)):
                tf_data[tf_name] = _compact_rows(
                    make_synthetic_bars_with_embedded_edge(n_bars=600, bar_seconds=sec, seed=13),
                    args.max_bars_per_tf,
                )
            h1_for_h4 = make_synthetic_bars_with_embedded_edge(n_bars=1600, bar_seconds=3600, seed=13)
            tf_data["H4"] = _compact_rows(resample_ohlcv(h1_for_h4, 14400), args.max_bars_per_tf)
        else:
            load_dotenv(ROOT / ".env")
            cfg = Config.from_env()
            if cfg.backend != "native":
                raise Blocked("native_backend_required")
            with loopback_native():
                feed = ReadOnlyNativeFeed(cfg.mcp_url, cfg.mcp_token, args.symbol)
                feed.connect()
                clock = feed._call("get_time_information", {})
                raw_time = find(clock, "trade_server_time") or find(clock, "trade_server_last_known_time")
                dt = timestamp(raw_time)
                if dt is None:
                    raise Blocked("timestamp_unverified")
                now_label = dt.replace(tzinfo=None)

                row = exact_symbol(
                    feed._call("get_marketwatch_symbols", {"symbol": args.symbol, "limit": 15, "include_hidden": True}),
                    args.symbol,
                )
                spec = enrich_spec(reported_spec(row), row)
                quote = parse_quote(row)
                live_spread = round(max(quote["ask"] - quote["bid"], spec["point"]), 6)

                m1_raw, _ = _paginate_period_retry(
                    feed, period="M1", now_label=now_label,
                    total_days=args.m1_days, chunk_days=1, stop_after_consecutive_empty=4,
                )
                m5_raw, _ = _paginate_period_retry(
                    feed, period="M5", now_label=now_label,
                    total_days=args.m5_days, chunk_days=4, stop_after_consecutive_empty=4,
                )
                m15_raw, _ = _paginate_period_retry(
                    feed, period="M15", now_label=now_label,
                    total_days=args.m15_days, chunk_days=12, stop_after_consecutive_empty=4,
                )
                h1_raw, _ = _paginate_period_retry(
                    feed, period="H1", now_label=now_label,
                    total_days=args.h1_days, chunk_days=45, stop_after_consecutive_empty=4,
                )
                h4_raw = resample_ohlcv(h1_raw, 14400)

                tf_data["M1"] = _compact_rows(m1_raw, args.max_bars_per_tf)
                tf_data["M5"] = _compact_rows(m5_raw, args.max_bars_per_tf)
                tf_data["M15"] = _compact_rows(m15_raw, args.max_bars_per_tf)
                tf_data["H1"] = _compact_rows(h1_raw, args.max_bars_per_tf)
                tf_data["H4"] = _compact_rows(h4_raw, args.max_bars_per_tf)

        basis_name, mult_val = resolve_pricing_basis(spec, "auto")
        bundle: dict[str, Any] = {
            "schema": "kronos-multitf-redacted-ohlcv/v1",
            "redacted": True,
            "symbol": args.symbol,
            "spread_px": live_spread,
            "pricing_basis": basis_name,
            "mult": round(float(mult_val), 4),
            "exported_at_utc": datetime.now(timezone.utc).isoformat(),
            "timeframes": tf_data,
        }
        assert_redacted_bundle(bundle)
        _save_private_json(out_path, bundle)
        size_mb = round(out_path.stat().st_size / (1024 * 1024), 2)
        counts = {k: len(v) for k, v in tf_data.items()}
        print(
            f"OK=True ORDERS=0 REDACTED=True SYMBOL={args.symbol} SPREAD={live_spread} "
            f"FILE={out_path} SIZE={size_mb}MB BARS={counts} ELAPSED={round(time.monotonic() - t0, 2)}s"
        )
        return 0
    except (Blocked, McpError, Exception) as exc:
        print(json.dumps({"ok": False, "orders_submitted": 0, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
