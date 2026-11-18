# Trading frameworks: Linux and MT5 integration assessment

Implementation update: a bounded TradingAgents tool-free analyst bridge and an
optional Backtesting.py worker are now implemented. See [SETUP.md](SETUP.md).
The assessment below records the pre-integration selection rationale; it does
not describe a full graph integration or a StockSharp migration.

Scope: source inspection of selected official upstream repositories, not a claim
that all names in the operator's list have been verified. Star counts and names
without repository URLs are not used as evidence. No upstream code was installed,
copied, executed, or given broker credentials during this assessment.

## Existing constraints

The web application is Python >=3.10; the operator's local environment is Python
3.10. MT5 runs separately through the existing native bridge (under Wine on the
operator's Linux host). Analysis plugins must not become another order executor.
Keep proposals, deterministic broker-aware sizing, demo restrictions and explicit
human approval in trade-mcp. Cloud model calls must pass the application budget
and ledger; installing an agent framework does not supply cloud-model entitlement.

## Selected projects

| Project | Evidence / fit | Recommendation |
| --- | --- | --- |
| TradingAgents | Python multi-agent LLM research; Apache-2.0; inspected pyproject requires >=3.11 and substantial LangChain/LangGraph/data dependencies; README requires model access | Candidate isolated research service, not an in-process dependency in the current 3.10 environment. Bound debate rounds and record every underlying call before enabling |
| ai-hedge-fund | MIT; README requires financial data access and a model provider; equity/fundamental investor agents | Candidate report-only service for appropriate instruments, not a ready XAUUSD/MT5 execution plugin |
| Freqtrade/FreqAI | GPL-3.0; README Python 3.12+, Linux support, crypto exchange orientation, ML research and dry-run | Separate crypto project if needed; do not install into this app or substitute MT5 symbols with exchange pairs |
| Qlib | MIT quantitative research platform | Later offline research/training candidate, not a hosted free LLM or direct MT5 connector |
| backtesting.py | AGPL-3.0 backtest library | Candidate isolated offline backtesting integration, subject to license review and spread/commission/next-bar validation. Not a cloud AI agent |
| StockSharp | See detailed findings below | No wholesale migration yet |

Official repositories:
- https://github.com/TauricResearch/TradingAgents
- https://github.com/virattt/ai-hedge-fund
- https://github.com/freqtrade/freqtrade
- https://github.com/microsoft/qlib
- https://github.com/kernc/backtesting.py
- https://github.com/StockSharp/StockSharp
- https://github.com/StockSharp/Connectors

## StockSharp findings

The inspected StockSharp LICENSE is a **custom proprietary notice**, explicitly
stating that the repository is not under a general-purpose open-source license
and referring users to the vendor EULA. A public GitHub repository and the README
word "free" must not be interpreted as MIT/Apache rights or as all connectors
being cost-free. Do not vendor its code into this MIT project without checking
applicable terms. Connector licensing/availability must be reviewed independently.

Evidence:
- https://github.com/StockSharp/StockSharp/blob/master/LICENSE
- https://github.com/StockSharp/StockSharp/blob/master/README.md
- https://github.com/StockSharp/StockSharp/blob/master/props/common_target_net.props
- https://github.com/StockSharp/StockSharp/blob/master/props/common_target_netwindows.props
- https://github.com/StockSharp/StockSharp/blob/master/props/common_target_avalonia.props

The repository has platform-neutral .NET targets, Windows-specific targets and
Avalonia sample applications. Thus "StockSharp cannot run on Linux" would be
incorrect, but so would "all StockSharp GUI products and MT5 connectors work on
Linux". Building/running the desired component has not been verified. The README
lists MT4/MT5 connections, but that alone does not establish connector price,
license, dependencies or compatibility with the operator's MT5/Wine deployment.
The separately inspected connectors listing includes MetaApi; this must not be
confused with proving a native, free, direct Linux MT5 connector.

A migration proof of concept needs all of:
1. Vendor terms for the exact core, UI and connector components.
2. A successful Linux .NET build/run of the chosen component in isolation.
3. Read-only connection to the actual demo MT5 account, with no credentials in logs.
4. Broker symbol/contract/tick/margin parity against the existing bridge.
5. A preserved human-approval boundary; no second live order execution path.
6. A reliable account of any cloud LLM or market-data charges.

No item above is proven merely by this repository assessment. Do not replace the
working Python/MT5 application until those checks pass.

## Proposed integration order (not installed)

1. One report-only TradingAgents sidecar in a separate Python 3.11+ environment,
   using controlled data inputs and a budgeted model adapter. Start with a single
   analyst; do not assume a full graph run equals one provider request.
2. One offline backtesting engine, with licensing reviewed and MT5 data exported
   without account identity. Preserve time ordering, warmup and execution costs.
3. Optional Qlib research, only after enough clean history and train/validation/
   out-of-sample splits exist.

News/fundamental stock agents are not automatically meaningful for a broker's
XAUUSD CFD. A model-free rules/ML strategy can run without token charges, but is
not equivalent to a cloud conversational agent. No framework fixes the observed
OpenCode free-quota rejection by itself.

## Acceptance gates for a later implementation

- Separate process/container; no untrusted repository install in the live venv.
- Pin a reviewed upstream revision and its dependencies/licenses.
- No broker keys or order tools for the research component.
- Treat returned text as untrusted, validate structured decisions and prices.
- Route all LLM generations/tool-induced generations through usage accounting.
- Disabled by default; missing dependencies/providers produce explicit blockers.
- Offline regression and Linux smoke tests before any optional demo connection.

This file is a decision record. The bounded implementation is documented in
SETUP.md; Linux StockSharp execution and live cloud success remain unverified.
