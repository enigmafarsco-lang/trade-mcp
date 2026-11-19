# Modular research and clean-text MCP

The existing Linux/Python 3.10 app, Persian UI, optional research environment,
OpenRouter configuration and MT5 execution flow are preserved. No cloud request,
installation, local weights, order or activation is performed by this addition.

## Entry points

- Existing CLI/web: unchanged (`trade-mcp console`, `trade-mcp serve`). The standard
  MCP server adds `_text` tools without changing old structured tool contracts.
- ` .venv/bin/python main.py` starts the **text-only** FastMCP facade on stdio.
  Do not run it as a web server. All tools registered here have `str` returns,
  serialized JSON without Markdown fences. No stdout logs or embedded credentials.
- OpenCode can launch that command as its MCP process using its existing local
  environment. OpenRouter models call these tools through the MCP client; this
  server does not automatically make a second model request or replace Ling.

## Implementation and provenance

1. `engines/technical.py`: validated OHLCV `[timestamp,open,high,low,close,volume]`,
   RSI14, MACD12/26/9, Supertrend10/3 and EMA20/50/200. Builtin formulas are
   independent, with Wilder ATR and SMA-seeded EMA; they are **not certified
   pandas-ta-equivalent**. `provider=pandas_ta` calls the real optional package
   directly, disables implicit TA-Lib for supported functions, and never falls
   back if unavailable. Different seeds can produce different values. Warmup
   values remain null. Caller must supply closed candles; timestamps are ordered
   but do not establish broker freshness. No timezone conversion is invented.
2. `engines/strategy.py`: independent EMA/MACD/Supertrend/RSI voting ensemble.
   BUY/SELL require at least 3 of 4 signed net votes; otherwise HOLD. The displayed
   percentage is **uncalibrated rule agreement, not win probability**. Insufficient
   warmup returns HOLD with unknown confidence. No profitability claim.
3. `engines/risk.py`: existing broker-aware position sizing, existing risk/lot caps,
   account-derived balance and maximum USD 10 planned price risk under the opt-in
   USD demo cash policy. Cannot substitute a crypto quantity for MT5 lots. Actual
   commission/gaps/slippage can increase loss. Orders still use full proposal and
   confirmation validation; sizing is not authorization.
4. `engines/exchange.py`: optional **real CCXT sandbox adapter** for ticker and
   balance. Sandbox is selected before any market/data call; unsupported sandbox
   errors do not fall back to production. Keys are read only from local environment
   `TRADE_MCP_CCXT_API_KEY`, `TRADE_MCP_CCXT_SECRET`, `TRADE_MCP_CCXT_PASSWORD`.
   Errors are withheld from text tools to avoid leaking provider bodies/secrets.
   **CCXT order execution is not implemented**: MT5 demo authorization does not
   authorize exchange/mainnet orders, and no durable exchange intent/risk/SL/net
   reconciliation adapter exists. The execution-status tool explicitly reports
   this blocker. Real MT5 order proposal/confirmation text tools delegate to the
   existing guarded execution pipeline; nothing bypasses it.

## Optional packages

No new mandatory dependency is added. Optional extras `exchange` and `candles`
provide CCXT and TA-Lib. Install only in a compatible explicitly chosen environment;
this change does not install them. pandas-ta has no automatic installation or
unverified version pin: verify the source/version and interpreter compatibility
before installing in the isolated research environment. The requested original
GitHub repository was not retrievable during this implementation. Never replace
it silently with an unrelated fork. The default builtin provider remains usable
without pandas/numpy/TA-Lib/CCXT. Missing optional packages return a clean error.

## Sources reviewed and boundaries

- https://github.com/twopirllc/pandas-ta — requested source, unavailable to the
  GitHub API check; direct optional API adapter, no code copied.
- https://github.com/TA-Lib/ta-lib-python (requested mrjbq7 link) — BSD-2-Clause;
  direct optional Doji/Hammer/Engulfing calls; signed scores are not probabilities.
- https://github.com/freqtrade/freqtrade-strategies — GPL-3.0. No strategy source
  copied into this MIT repository and no unsupported NFI/ClucMay7 attribution.
  A published backtest does not make a strategy proven profitable on MT5 gold.
- https://github.com/virattt/ai-hedge-fund — MIT. Architectural reference only;
  its agents/fundamental feeds are **not** claimed as integrated or executed.
- https://github.com/ccxt/ccxt — MIT; imported optional sandbox reader.
- https://github.com/jesse-ai/jesse — MIT; architectural reference only. Existing
  MT5 sizing remains the implementation, not a copied Jesse module.
- https://github.com/AI4Finance-Foundation/FinGPT — MIT. No FinGPT model or local
  weights loaded. Existing RSS/cloud analysis remain separate; no fabricated
  sentiment score or claim of FinGPT execution is provided.

The app already integrates genuine upstream TradingAgents role factories and
pinned Backtesting.py SmaCross separately; those do not turn this independent
voting engine into either upstream system. Provenance is explicit in outputs.

## Web and local verification

The Persian workspace has an input-data technical/signal panel. Paste closed OHLCV
JSON and choose builtin or real pandas_ta. `POST /api/engines/analyze` performs
local deterministic computation only. It does not fetch news, send a model request,
place an order or consume cloud budget. This is input analysis, not automatic
live-broker analysis or a replacement for the existing live analysis dashboard.

OpenCode deployment instructions:

```text
Fast-forward the current arena/f81b6b27-trade-mcp branch, preserving local data.
Read docs/MODULAR-ENGINES.md. Do not install packages or change model/provider.
Run the repository tests and verify main.py lists only clean-string tools.
Restart only the existing console if needed; preserve Wine/MT5 and secrets.
Verify the Persian engine panel with valid closed OHLCV and malformed input.
Exercise builtin technical_analysis_text and strategy_signal_text offline.
Test missing optional dependency behavior without installing anything.
Do not call confirmation, enable trading, access exchange balances, make cloud
requests, or interpret heuristic confidence as probability during verification.
Commit only a sanitized measured-status report, never OHLCV/account/model records.
Push that report to this same branch and return the hash.
```

The native ticket mapping is now verified by user-local overlap evidence, but
1846 native-only deals were still unmatched in the latest report. No missing
costs are filled, no input window is changed to force success, and guardian
execution remains blocked independently of these additive research modules.
