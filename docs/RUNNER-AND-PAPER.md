# Mandatory risk, combined technical tools and bounded paper runner

## What existed and what changed

The application already had risk sizing/lot/spread/margin/SL validation, an
OpenRouter-compatible client, scheduled agents, SQLite usage accounting and
separate proposal/confirmation. It was not unrestricted LLM-to-broker execution.
This revision adds a 2% absolute ceiling to the existing validation boundary,
rejects nonfinite numbers, makes SL mandatory for new orders, and adds the
requested standalone facade. Existing stricter configured percentage, broker
volume limits, max_lot (default 0.1) and opt-in USD 10 demo cap still apply.
The ceiling is not a change of the default risk to 2%.

`risk_manager.calculate_position_size(balance, percentage, entry, stop)` computes
asset units. Percentage points: 1 = 1%. It rejects >2%, floors to step, never rounds
up to minimum, returns 0 when no permissible minimum fits. Optional broker
multiplier/min/max/step and max_cash_risk make units explicit; raw units must NOT
be sent as MT5 lots. Actual MT5 proposal/confirmation continues to use existing
broker-aware `risk.py` validation. Commissions/gaps/slippage are not guaranteed by
a planned price-risk formula.

## One-call technical report

`get_comprehensive_analysis(symbol, timeframe)` and alias
`get_full_technical_summary` return JSON strings through FastMCP. They read closed
broker bars once and include RSI14, MACD12/26/9, EMA20/50/200, Supertrend10/3, ATR14
and Bollinger20/2. Null warmup remains null. The existing Persian engine panel
also receives the added ATR/Bollinger fields. No cloud call is made by these tools.
The text-only facade now discovers 15 tools, including the two exact requested
names (these two do not end in _text, but still return clean strings).
ATR/Bollinger use the explicitly labeled built-in definitions even if other
features select optional pandas_ta. No equivalence claim is made.

## Paper-only agent.py

The new root `agent.py` delegates to `trade_mcp.runner`, reusing LLMClient in web
mode with the existing model, actual usage ledger and shared budget. No new SDK,
key file, model rotation, provider retry or local weights are added. Existing Ling
stays selected unless the user explicitly changes local configuration. Credentials
remain in the existing local environment/owner-only secret configuration, never
CLI arguments, source code or the trade log.

Each cycle obtains the combined closed-bar report, marks an existing simulated
position from fresh bid/ask, then makes at most one structured model request if
no paper position/loss latch blocks it. buy/sell/wait is a constrained decision
schema, not arbitrary model-directed code/tool execution. Confidence below 0.7
means HOLD; this model confidence is not a calibrated win probability. Wrong-side
levels, stale responses/quotes, spread/drift and risk/volume violations block
simulation. No stop/target is tightened to force acceptance.

PAPER_TRADING defaults true for this **new runner**. It also accepts prefixed
TRADE_MCP_PAPER_TRADING. The new runner defaults to paper even when no flag exists. For the legacy
web/MCP broker path, the explicit prefixed flag is a hard execution kill switch:
TRADE_MCP_PAPER_TRADING=true blocks confirmation and guardian close submission,
with no fake broker success; use the paper runner for simulated fills. It defaults
false there for backward compatibility, while ALLOW_TRADING still defaults false.
Do not switch a live owned position into paper mode: broker SL remains but the
cash guardian will not submit exits in paper mode. Setting paper false for this
standalone runner refuses startup: real/demo broker
automation remains in the existing guarded AgentEngine + guardian, not a new
unmonitored execution loop. CCXT real orders remain unsupported by this runner.

Commands (do not run a cloud request without a separately authorized active budget):

```bash
.venv/bin/python agent.py --help
# One paper cycle, using an ALREADY active authorized budget:
PAPER_TRADING=true .venv/bin/python agent.py \
  --symbol XAUUSD --timeframe M15 --interval 900 \
  --duration 300 --max-cycles 1 --consent-model-requests
```

The interval defaults to 900 seconds, but the current standing cloud limit is
300 seconds / five attempts, so a default window does not reach a second 15-minute
cycle. The runner never activates, resets, extends or automatically reopens a
budget window. Exhausted/revoked budget returns an explicit blocked result before
any request. HTTP/provider/malformed-response errors stop; no fallback or retry.
Client/server timeouts are not hard cancellation of an in-flight upstream call;
internal upstream retries remain possible. No unlimited paid background loop.
On exit existing paper positions stay unrealized in the file; no invented close
is logged and no actual broker guardian is claimed to run for them.

## Paper accounting and logging

`PaperLedger` writes `logs/trades_log.json` by default (configurable --paper-log),
with 100 simulated USD initial balance (--paper-balance). This is not account
balance or actual trading profit. File locking and atomic replace prevent
concurrent double opens; file mode 0600 and fsync are used. Corrupt files fail,
never reset silently. Keep the log ignored/local. A 10000-event/8MiB cap stops
rather than deleting history. No raw model text, key, provider body or account
identity is logged.

Only one simulated MT5-lot position is allowed, with a hard 0.1-lot cap and planned
risk <=min(2% of simulated balance, USD 10); caller/broker limits may be lower.
Three consecutive simulated closed losses latch new paper entries across restarts
without automatic reset. Spread is applied via entry ask/exit bid for buy and the
reverse for sell. Quote-based SL/TP or monitored -10/+2 closes use the observed
quote, not an invented stop fill, so gaps can exceed the planned loss. Fees are
explicitly modeled as zero **simulation assumption**, not verified broker costs.
Balances, P/L, events and IDs are labeled paper. No MT5/CCXT order API is imported
by the ledger. Use one paper file per instrument/run configuration; it is not an
exchange fill/liquidity/margin model or strategy profitability evidence.

## Local OpenCode verification (offline only)

```text
Fast-forward the current branch preserving all local data/environment.
Read docs/RUNNER-AND-PAPER.md. Run the tests and agent.py --help only.
Discover the 15 text tools and call get_comprehensive_analysis once on existing
broker data if available; no model request is needed for that tool.
Run paper and runner tests with faked LLM/quotes only; do not run an actual cloud
cycle, activate budget, enable trading or submit a broker order in this deployment.
Restart only the console if needed for the added ATR/Bollinger web output.
Do not alter the monitor, Wine/MT5, journal or loss latch.
Record sanitized measured results; never commit paper logs, OHLCV or prompts.
```

Record deployment tests under `risk_guardrails`, `comprehensive_analysis`,
`paper_ledger` and `paper_runner_offline` in the existing sanitized report tool.
A paper test is never recorded as a real order or live guardian acceptance.
The workspace propagates the prefixed paper flag and snapshot path; its managed
consoles still force ALLOW_TRADING=false. No setting here automatically changes
the user's running console or model budget.
