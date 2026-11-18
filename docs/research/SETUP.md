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
