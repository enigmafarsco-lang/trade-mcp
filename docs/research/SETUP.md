# Optional Linux research engines

## Implemented scope

- **TradingAgents**: the official market analyst's tool-free final-turn prompt is
  prepared in a separate Python process with the supplied broker snapshot. One
  generation then happens in trade-mcp's existing budgeted/ledgered web client.
  This is NOT the full graph, debate, news scraping, or automatic trader. No
  upstream tools, Yahoo ticker conversion or external data lookup are run.
- **Backtesting.py**: a fixed long-only SMA 10/30 example on up to 499 closed H1
  bars. Signals execute next bar; fixed one-unit asset sizing and 0.1% commission
  per side, with final liquidation. It is NOT MT5 lot sizing or broker P/L parity.
  There is no spread, financing or slippage simulation and no out-of-sample
  validation. Results on mock data have no market-performance meaning.

Both results are read-only. No proposal or order is created. A research report
is untrusted text displayed using textContent, not an instruction to the executor.
The analysis can consume more tokens than the normal compact agent prompt.

## Separate environment (operator opt-in)

Keep the main application's Python 3.10 environment unchanged. Review licenses
and the pinned sources before installation. Use a new Python 3.11+ environment:

```bash
python3.11 -m venv .venv-research
.venv-research/bin/python -m pip install -r research-requirements.txt
```

Only do this in a new directory, not over an existing environment. This installs
third-party dependencies and requires download space. No model weights are
installed; inference still uses the configured cloud/direct web provider.

Set this **in the process launching the MCP/console**, using an absolute path:

```bash
export TRADE_MCP_RESEARCH_PYTHON="$PWD/.venv-research/bin/python"
export TRADE_MCP_ALLOW_TRADING=false
```

Restart only the owned console/MCP through the existing safe connection workflow.
Changing shell environment does not alter an already running process. The
workspace manager propagates the explicit research interpreter to its child.
`GET /api/research/status` reports whether the interpreter exists, NOT that all
its packages have been verified. Missing packages return
`research_dependencies_missing`, version incompatibility returns
`research_worker_failed`. No automatic installation occurs.

## Web controls

Select an existing agent (the first symbol is used). Open the optional research
section in the workspace.

- Backtest: no LLM, no token-budget reservation, no provider ledger entry.
- TradingAgents report: explicit request-consent checkbox, a configured web model,
  and an active shared test budget are required. Every application generation
  passes through the same LLMClient and model_usage journal. Provider errors are
  returned; no retry, model rotation, or deterministic report substitution.

Endpoints: POST `/api/research/backtest` with `agent_id`, and POST
`/api/research/analyze` with `agent_id` and `consent_model_request: true`.
Research reports and backtest results are currently returned to the page, not
persisted as agent-run history. Model usage IS persisted in the normal ledger.

## Isolation and licensing

Child environment is allowlisted; API keys and MT5/OpenCode credentials are not
forwarded. The worker blocks ordinary Python socket connections and runs with a
30-second preparation/backtest timeout. **This is not an OS security sandbox**;
reviewed third-party Python code executes with the user's filesystem permissions.
For untrusted packages use an OS/container sandbox with a read-only filesystem
and network disabled. Installations themselves are separate network operations.

TradingAgents is Apache-2.0; Backtesting.py is AGPL-3.0. Their dependencies retain
their own licenses. Dependencies are optional and no upstream code is vendored.
Process separation does not automatically settle legal obligations: review AGPL
requirements before distributing/deploying this combination. StockSharp has not
been installed or integrated. Its custom license remains a separate decision.

## Verification

The author tested the actual pinned packages in a separate Linux Python 3.11
environment: official TradingAgents prompt preparation returned four messages
without model calls; Backtesting.py ran on synthetic candles. Regression tests
cover credential stripping, missing configuration, prompt validation, budget and
ledger routing, and no proposal creation. No live cloud success, MT5 connection,
browser smoke test or profitable strategy is implied by these tests.

## Selectable educational strategy baselines

The research panel now offers `sma_cross` (10/30), `rsi_reversion` (14-period
simple rolling RSI, enter below 30, exit above 55), and `donchian_breakout`
(previous 20-bar high entry, previous 10-bar low exit). API clients pass
`strategy` to `/api/research/backtest`; omission preserves SMA as the default.
All are long-only, fixed-unit, next-bar backtests under the existing cost
assumptions; no automatic execution or model request occurs. Thirty bars are
reserved for warmup. Signals use only the current and earlier bars.

These are our independent educational implementations, NOT imported strategies
from Freqtrade, StockSharp, Qlib or AI Hedge Fund. TradingAgents remains the
actual upstream integration (tool-free market analyst). Those repositories have
different market/data/execution assumptions; this change does not claim their
models were ported or pretrained weights installed. Backtesting.py remains the
execution engine and retains its upstream licensing requirements. No strategy
is optimized, proven profitable, or approved for live trading.

## Actual upstream strategy: official SmaCross

`upstream_sma_cross` directly imports `backtesting.test._test.SmaCross` from the
isolated pinned backtesting==0.6.5 installation. It is NOT our long-only baseline.
Reviewed source revision: `767746127e75dc1fe99bc72a3e75fe2637ceef60`, file
`backtesting/test/_test.py`, lines 57–72. No upstream source is vendored. Upstream
AGPL licensing still applies. This is a private test-module API, so the adapter
rejects other package versions rather than silently importing changed behavior.

The original class uses 10/30 SMA crossover, reverses long/short, and uses the
upstream default size (near-full available synthetic equity), NOT one unit or
MT5 risk sizing. We intentionally do not alter that behavior and label it in
both UI and output. Do not compare its returns with fixed-unit baselines without
normalizing exposure. It has no protective SL/TP and is research-only, never an
execution proposal. The same research process isolation, next-bar fill, synthetic
cash and simplified commission assumptions apply. Real isolated-worker tests
exercise this direct import on synthetic data; they do not validate profitability.

## Single-snapshot comparison and reproducible report

`POST /api/research/compare` with an existing `agent_id` fetches H1 candles once,
drops the last (potentially open) candle, and runs all four strategies in one
isolated worker on that exact input. There is no model request, budget change,
proposal, optimization, ranking or order. The comparison button is in the same
research panel. Partial strategy failures return HTTP 409 with per-strategy
results; they are not silently reported as a successful complete comparison.

Both individual backtests and comparison replies include `generated_at` (actual
UTC ISO timestamp), a `snapshot` with canonical OHLC and supplied numeric bar
times, and a SHA-256 over its canonical JSON. Missing bar times remain null in
metadata; no dates are invented. The snapshot hash identifies inputs, not their
freshness, accuracy or source authenticity. Invalid/nonpositive/nonfinite OHLC
or inconsistent ranges are rejected. Comparison uses the same candle data, not
the same exposure: original SmaCross uses default equity sizing and long/short,
whereas the three independent baselines use one unit and long-only. Returns
must not be ranked as comparable risk-adjusted performance.

The web **save backtest report (JSON)** button downloads the full response,
including candles and per-strategy provenance/assumptions, to the user's browser.
It also supports classified failed responses. This does not automatically write
a server log or include prompts, credentials or account balances. Treat exported
broker candle data according to your data-sharing restrictions. Local automation
can save the exact API JSON into an existing ignored log directory; do not use
shell text templates with unexpanded date expressions. Preserve full UTC time,
input hash, errors and exposure warnings in any derived human-readable report.

## Actual TradingAgents researcher roles

The research report selector now supports the original pinned upstream
`create_bull_researcher` and `create_bear_researcher` factories as well as the
market analyst. These are direct imports from revision
`9fa1196fffbcfd9de5146d2dd2869df84ab99838`, not our technical baselines.
They prepare one deliberately one-sided argument from the supplied broker
snapshot. News/sentiment/fundamentals are marked absent. There is no automatic
debate, consensus, trained strategy, new data tool, or trading execution. Each
explicit report uses one existing budgeted model-client call, only after consent.
Offline prompt preparation tests invoke both actual upstream factories without
calling a model. API field: `role` = `market` (default), `bull`, or `bear`.

## Alternative cloud access: explicit, read-only OpenRouter check

Official source checked: https://openrouter.ai/docs/api/reference/limits .
It documents authenticated `GET /api/v1/key` for account/key limits and a
`free_model_daily_requests` object when supplied. Do not guess quota from local
OpenCode unauthenticated endpoints. No free catalogue guarantees availability.

A new optional command, `python -m trade_mcp.cloud_access`, checks only the
fixed official OpenRouter `/key` and `/models` GET endpoints. It uses
`OPENROUTER_API_KEY` provisioned locally by the user, never CLI arguments or chat.
Without a key it returns `api_key_required` without making a network request.
It prints only safe quota numbers and explicitly zero-priced `:free` catalogue
IDs. It does not generate, select a model, change web config, activate a budget,
create an account, purchase credit or touch the usage ledger. Redirects are
refused. Errors never echo response bodies. This is not a substitute for provider
terms/privacy review or a successful bounded generation test.

If the user independently provisions access, the existing direct web backend
can use `TRADE_MCP_LLM_BASE_URL=https://openrouter.ai/api/v1`,
`TRADE_MCP_LLM_API_KEY` supplied securely, an explicitly chosen
`TRADE_MCP_LLM_MODEL`, and `TRADE_MCP_WEB_MODEL_BACKEND=direct`.
Do NOT apply these automatically or use them to bypass an existing provider
restriction. Do not use a random/free router or paid fallback. New generation
requires separate explicit budget authorization; old authorization is spent.
