# One-shot DEMO acceptance continuation (rules-guarded) — sanitized report

- **Report id:** `20261120T100914Z-rules-guarded-demo-continuation`
- **Generated (UTC):** 2026-11-20T10:09:14Z
- **Base commit (preserved):** `302ad14b67ce926181139b7e37e74b1468db4db3` (prior preflight-blocked sanitized report retained)
- **Branch:** `arena/f81b6b27-trade-mcp`
- **Workflow:** `docs/RULES-GUARDED.md` — explicit `run_rules_guarded` (deterministic internal rules; not cloud/AI/imported strategy/hourly volatility). Exactly one invocation, no retry, no separate confirmation, no raw broker order tool, no forced signal, no threshold/cap/SL changes.

> `execution_authorized_by_report: false`. This report does not authorize execution.

## 1) Publisher diagnosis and recovery (no forbidden changes)
- **Diagnosis (MT5 native MCP, build 6327, DEMO/USD):** configured v2 snapshot path belonged to the running terminal (`MQL5/Files/trade-mcp-costs.tsv` under terminal data path) but was stale/not updating. `list_open_charts` showed only chart `105274268700280` (XAUUSD H1) with `TradeMCPConnector` attached; **`TradeMCPCostMonitor` was absent** from all charts. Monitor expert journal recent records: 0; no monitor error evidence observed. No indication of writing to a different terminal data directory.
- **Compiled monitor (existing):** `MQL5\Experts\TradeMCPCostMonitor.ex5`. Parameters independently verified via `get_expert_advisor_parameters`: `From=1767225600` (2026-01-01T00:00:00Z), `MaxDeals=10000`, `IntervalSeconds=5` — previously verified bounded inputs.
- **Recovery:** opened one new dedicated chart `105331913664529` (XAUUSD H1) and attached `TradeMCPCostMonitor.ex5` with **no `expert_parameters`** (preserved verified bounds). Connector chart `105274268700280` untouched. No recompile, no MT5/Wine restart, no monitor input change, no duplicate writer.
- **Verification:** charts after recovery — connector on `105274268700280`, monitor only on `105331913664529`. Snapshot observed updating multiple times (≥3 distinct mtime updates, ~5s cadence, fresh age). `/api/cost-reconciliation` returned `ok:true` (diagnostic; `execution_eligible:false`, `lifecycle_complete:false`) with fresh snapshot.

## 2) Fresh preflight (repeated, passed) before invocation
- DEMO/USD (MetaQuotes-Demo, build 6327), broker `positions=0`, `orders=0`, guardian durable state clean (`latched=false`, `streak=0`, `intents={}`), fresh v2 snapshot, cost reconciliation pass, budget `revoked` (no cloud request), scheduler/page cycles stopped.
- Agent `a76d1cff5b` («اسکالپ طلا», guarded, enabled, `['XAUUSD']`, `max_lot=0.02`, `max_risk=0.5`, hours empty) preserved.
- Test console started for the authorized run with process-only env: `TRADE_MCP_ALLOW_TRADING=true`, `TRADE_MCP_DEMO_CASH_POLICY=true`, **`TRADE_MCP_PAPER_TRADING=false`** (correct variable verified). Broker preflight repeated immediately before call.

## 3) Exactly one invocation
- **Call:** `POST /api/agents/a76d1cff5b {"action":"run_rules_guarded"}` (HTTP 200, ~0.292s), performed once.

## 4) Separate outcome report
- **Strategy signal (deterministic rules):** `source=rules`, `llm_status=not_configured`, no cloud request. Signal: `sell`, confidence `63` (XAUUSD), scenario entry ~3575.45 / SL 3594.65 / TP1 3546.64. Warning: `volatility is spiking - widen stops or stay out`. (Confidence/rule agreement is not a probability or profit guarantee.)
- **Risk checks / gate:** proposal **cancelled** before broker submission — `sized 0.05 lot, above the agent's 0.02 cap` (existing limit preserved). No auto-confirm reached.
- **Proposal:** created then `cancelled` (agent, XAUUSD sell, demo cash policy context); **not submitted** to broker.
- **Confirmation:** none.
- **Actual broker response:** none (no order submitted).
- **Owned-position reconciliation:** not applicable (flat).
- **Floating net-P/L evidence:** not applicable.
- **Actual exit evidence:** not applicable. No position opened; **not OPEN**.

**Result:** no order (deterministic rules decision produced; execution blocked/cancelled by existing agent risk/lot cap). No position/order/unresolved intent remains.

## 5) End state and safety
- Broker: `positions=0`, `orders=0`. Guardian durable state: no latch, no intents. Budget remains `revoked` (no cloud request).
- Test console (trading flags enabled in process env for the run) stopped; **safe console restarted** with `TRADE_MCP_ALLOW_TRADING=false`, `TRADE_MCP_DEMO_CASH_POLICY=false`, `TRADE_MCP_PAPER_TRADING=false` (trading disabled). No protection stopped while a position existed (flat throughout).

## 6) Evidence and publication
- Continuation raw evidence archived locally (owner-only, `700/600`, git-ignored): `logs/session-20261120T092406Z-continuation/` (recovery notes, pre-call preflight, single invocation response, post-state snapshots, console logs). No secrets/credentials/`.env`/keys/tokens/raw provider payloads included in sanitized reports.
- Sanitized continuation reports committed (separately from 302ad14). Push attempted on `arena/f81b6b27-trade-mcp` (see commit hash and push result).
