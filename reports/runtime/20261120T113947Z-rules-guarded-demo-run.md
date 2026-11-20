# Bounded DEMO acceptance — one-shot `run_rules_guarded` (OPEN, protected)

- **Report id:** `20261120T113947Z-rules-guarded-demo-run`
- **Generated (UTC):** 2026-11-20T11:39:47Z
- **Base commit:** `276b7c22110bd887c93c160424d16d0018a0411c` (`arena/f81b6b27-trade-mcp`)
- **Scope:** monitor restored via dynamically discovered native MT5 MCP tools; fresh preflight; exactly **one** `run_rules_guarded` invocation. No cloud calls, no forced signal, no threshold/cap/SL changes, no repeat invocation.

> `execution_authorized_by_report: false`.

## 1) Monitor blocker resolved (native MT5 tools discovered dynamically)
- **Discovery:** native MT5 MCP endpoint tools listed dynamically (107 tools), including `list_open_charts`, `chart_open`, `chart_add_expert`, `get_expert_advisor_parameters`. Chart/EA tools were **available** (trade-mcp text facade not relied upon).
- **Actual inspection:** `list_open_charts` showed only connector chart `105274268700280` (XAUUSD H1, `TradeMCPConnector`) — **TradeMCPCostMonitor absent** (not inferred from logs alone).
- **Recovery (verified procedure, docs/RULES-GUARDED.md):** opened one dedicated chart `105389480649722` (XAUUSD H1) and attached existing compiled `TradeMCPCostMonitor.ex5` with verified bounded inputs `From=1767225600, MaxDeals=10000, IntervalSeconds=5`. Connector chart untouched. **No** MT5/Wine restart, recompile, input change, duplicate writer, or snapshot edit.
- **Verification:** charts show connector + dedicated monitor only; snapshot observed updating multiple times (~5s, fresh); cost reconciliation `ok:true` (diagnostic, snapshot fresh).

## 2) Fresh preflight (complete, passed)
- DEMO/USD (MetaQuotes-Demo), broker `positions=0`, `orders=0` before call.
- Demo cash guard: no latch, no unresolved intents. Agent `a76d1cff5b` (“اسکالپ طلا”, guarded, enabled, single symbol `['XAUUSD']`, `max_lot=0.02`, `max_risk=0.5`, hours empty) preserved.
- Fresh v2 snapshot and cost/account reconciliation **pass** (diagnostic). Budget **revoked**, scheduler/page cycles **stopped**. Paid API disabled.
- Test console started (process env only) with `TRADE_MCP_ALLOW_TRADING=true`, `TRADE_MCP_DEMO_CASH_POLICY=true`, `TRADE_MCP_PAPER_TRADING=false` after preflight; broker preflight repeated before call (guardian active in process).

## 3) Single invocation
- `POST /api/agents/a76d1cff5b {"action":"run_rules_guarded"}` — **once** (HTTP 200, ~1.57s). No cloud call/window, no forced signal, no threshold/cap/SL change, no retry.

## 4) Outcomes reported separately (unobserved ≠ pass)
- **Model:** not applicable (deterministic internal rules path; no cloud request).
- **Signal:** not observed in captured API response fields (execution/proposal evidence present).
- **Sizing (observed in proposal):** XAUUSD sell, volume `0.02` (capped by configured `max_lot`), SL `3598.85`, TP `3575.36`, demo cash policy context.
- **Proposal:** `7f5e35c98b06` — **executed** (XAUUSD sell).
- **Broker acceptance (observed):** retcode `10009` (“done”), deal recorded, position ticket `152939567958`, fill ~`3585.38`, volume `0.02`.
- **Ownership (observed/tracked):** open position XAUUSD sell `0.02` (ticket `152939567958`) with broker **SL present** (`3598.85`) and TP present; demo cash guard intent registered/tracked for the owned intent (comment-reservation, awaiting reconciliation). Guardian protection retained.
- **Floating net P/L (observed at capture):** **open/floating** (small floating loss at capture), not a realized exit.
- **Guardian exit:** **unobserved** — position **OPEN**. No exit forced.

## 5) State and protection
- **Position:** **OPEN** (1 position, 0 pending orders). Guard not latched; unresolved intent present.
- **Protection retained:** test console remains running with `trading` and `demo cash policy` **enabled** (process env) to preserve independent guardian protection and broker SL for the owned open position. Flags **not** returned to false because the account is not flat and an unresolved intent remains (per policy: never stop protection while a position remains / only when flat with no orders or unresolved intents).
- Budget remains **revoked**; entry cycles/scheduler stopped; paid API disabled. No additional invocations.

## 6) Evidence
- Raw evidence (owner-only, `700/600`, git-ignored): `logs/session-20261120T113947Z-rules-guarded/` (monitor restore, pre-call preflight, single run response, post-state, console log) — sanitized of secrets/credentials/raw provider payloads.
- Sanitized reports committed (see commit hash/push result). Prior reports preserved.
