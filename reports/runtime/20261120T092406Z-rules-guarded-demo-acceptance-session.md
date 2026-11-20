# One-shot DEMO acceptance (rules-guarded) — sanitized session report

- **Report id:** `20261120T092406Z`
- **Session time (UTC):** 2026-11-20T09:24:06Z (local ~12:54 +03:30)
- **Commit:** `b0489cb229e533d3a342f5a3c436f559334b60db` (`arena/f81b6b27-trade-mcp`, fast-forwarded from `7d60f5d`)
- **Workflow:** documented authorized one-shot DEMO acceptance per `docs/RULES-GUARDED.md` and `AGENTS.md` (explicit `run_rules_guarded`, not cloud/AI/imported strategy/hourly volatility fallback)
- **Scope/constraints:** DEMO only; no live account; no risk/threshold/SL changes; no MQL5 recompile; no monitor input changes; no MT5/Wine restart; no dependency install; cloud budget kept revoked; scheduler/page cycles stopped; no raw broker order tool.

> `execution_authorized_by_report: false`. This report does not authorize execution.

## 1) Git / code state
- Fetched and fast-forwarded `arena/f81b6b27-trade-mcp` to `b0489cb` (Exposes explicit one-shot guarded rules execution and persists handoff workflow). Untracked nested `trade-mcp/` directory preserved.
- Tests run: `tests/test_rules_action.py` **3 passed**; full offline `pytest -q` completed to 100% progress (5 skips observed in progress output; summary line not captured in terminal tail).

## 2) Fresh preflight (not reused) — **FAILED (blocker)**
- **Pass:** DEMO/USD account on MetaQuotes-Demo (MT5 build 6327), broker `positions=0`, broker `orders=0` (`/api/account-state`); demo cash guard durable state clean (`latched=false`, `streak=0`, `intents={}`); chosen agent `a76d1cff5b` ("اسکالپ طلا") is `guarded`, enabled, single symbol `['XAUUSD']`, `max_lot=0.02`, `max_risk=0.5`, hours empty (limits preserved); budget `revoked` (1/5 used, 4 remaining, activation required); scheduler stopped.
- **Fail (blocking):** cost snapshot is v2 (`meta\t2\t...`) but **stale and not updating** (age ~56+ min at check; mtime/size unchanged across repeated observations). `/api/cost-reconciliation` → `{"ok":false,"execution_eligible":false,"lifecycle_complete":false,"error":"cost_check_failed","stage":"snapshot"}`. Remediation would require forbidden actions (MT5/Wine restart, MQL5 recompile, or monitor input changes); none taken.
- **Observed (non-broker, noted):** 6 journal proposals with `status=pending` but long-expired timestamps (stale DB rows); broker pending orders are 0 and guardian intents are empty.

Because fresh preflight did not pass, step 4 (start console with trading+demo cash policy enabled for the authorized test) and step 5 (one-shot invocation) were **not performed**.

## 3) One-shot invocation
- **Performed:** no.
- **Endpoint:** `POST /api/agents/a76d1cff5b {"action":"run_rules_guarded"}` — **not called** (preflight blocker).
- **Cloud request:** none. Budget remained revoked.

## 4) Separate outcome report
- **Strategy signal (deterministic rules path):** unavailable — invocation not performed.
- **Risk checks:** not executed.
- **Proposal:** none.
- **Confirmation:** none.
- **Actual broker response:** none (no order submitted).
- **Owned-position reconciliation:** not applicable (flat).
- **Floating net-P/L evidence:** not applicable.
- **Actual exit evidence:** not applicable. No position opened; not OPEN.

## 5) Safe end state (flat)
- Broker: `positions=0`, `orders=0`. Demo cash guard: no latch, no intents.
- Previous console process (pre-ff code, trading flags enabled in process env only) was stopped. New console started (pid 47727, current `b0489cb` code) with `TRADE_MCP_ALLOW_TRADING=false`, `TRADE_MCP_DEMO_CASH_POLICY=false`, `TRADE_MCP_PAPER_TRADING=false`; trading disabled/proposals-only; scheduler stopped; budget remains revoked.
- No position remained, so no guardian protection was stopped while exposed.

## 6) Evidence and publication
- Raw evidence archived locally (owner-only, `700/600`) under `logs/session-20261120T092406Z/` (git-ignored): preflight/account/overview/cost/budget/agents snapshots and safe-console log. No secrets, credentials, `.env`, keys or tokens copied into sanitized reports.
- Sanitized runtime report files (this session): committed on `arena/f81b6b27-trade-mcp` (see manifest and commit hash reported with push result).
