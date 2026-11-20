# Bounded DEMO acceptance session — sanitized session report

- **Report id:** `20261120T080951Z`
- **Session date:** 2026-11-20 (local UTC+03:30; report timestamp UTC)
- **Loaded commit:** `c3a8f7b792597f6016e3e428c6b59618235b3850` (branch `arena/f81b6b27-trade-mcp`, clean worktree for tracked files at session start)
- **MT5 build:** 6327 (terminal restarted earlier the same day from build 6323 by its own self-update; no restart performed by this session)
- **Scope:** one real cloud analysis window plus at most one automatic DEMO entry through the existing guarded AgentEngine, with the independent DemoGuardian managing the single position.
- **Authorization posture:** real cloud analysis + automatic DEMO execution only. No live account, no risk increase, no MQL5 recompile, no monitor input change, no MT5/Wine restart, no dependency install.

> `execution_authorized_by_report: false`. Passing diagnostics are not execution readiness.

## Timeline (local time, UTC+03:30)

| Time | Event |
|---|---|
| ~10:48–10:52 | Frozen snapshot detected; monitor confirmed absent from every chart after the build-6327 self-update; MT5 MCP tool schemas inspected read-only |
| ~11:07 | New dedicated chart opened; compiled `TradeMCPCostMonitor.ex5` attached with default (documented) bounds; snapshot begins refreshing every ~5 s |
| 11:11 | Read-only preflight run with both trading flags forced false → **15/15 PASS** |
| 11:12 | Console started with process-only env (flags true for this process only); scheduler and page cycle left stopped |
| 11:13–11:17 | Independent pre-cloud recheck: demo/USD, 0 positions, 0 orders, fresh v2 (2.6 s), reconciliation ok, guardian healthy, cycles stopped |
| 11:17:49 | **One** budget window activated (300 s / 5 attempts; deadline starts at first attempt) |
| 11:18:19 → 11:18:25 | **Exactly one** guarded invocation, exactly one symbol (XAUUSD) → **HTTP 429 rate_limited**, ~6155 ms, usage null; decision `wait` / `model_error`; 0 proposals |
| 11:19:28 | Budget **revoked** after **1/5** attempts; no retry, no extra window |
| after | Console + guardian left running; entry cycles stopped; broker shows 0 positions / 0 orders |

## 1. Frozen-monitor detection and successful restoration

- **Detection:** after the terminal self-update to build 6327, the only open chart ran
  `TradeMCPConnector`; `TradeMCPCostMonitor` was attached to **no chart**, the expert journal
  held zero monitor records, and `trade-mcp-costs.tsv` was static (last write 10:52:39 local,
  produced by a one-shot script with a non-default `From` value). Preflight therefore failed on
  `snapshot_v2`, `snapshot_fresh_le_30s` and `cost_reconciliation_ok`.
- **Restoration (all within constraints):** via MT5's own MCP, `chart_open` created a **new
  dedicated chart** (connector chart untouched), then `chart_add_expert` attached the **existing
  compiled** `TradeMCPCostMonitor.ex5` with **no** `expert_parameters`, so the compiled defaults
  (`From=2026-01-01`, `MaxDeals=10000`, `IntervalSeconds=5` — exactly the documented bounds) were
  preserved. No recompile, no input edit, no restart, no dependency install.
- **Verification:** snapshot republishing every ~5 s (observed ages 0.7/1.8/2.8 s), source
  `mql5_cost_snapshot_v2`, monitor journal `records_count: 0` (no failures), single writer.

## 2. Read-only preflight and independent pre-cloud checks

- Preflight script forces `ALLOW_TRADING=false`, `DEMO_CASH_POLICY=false`, `PAPER_TRADING=false`,
  performs no journal writes, no proposal, no order, no budget activation.
- **Result: `preflight_ok: true`, 15/15 checks PASS**, including `snapshot_v2`,
  `snapshot_fresh_le_30s` (0.69 s), `cost_reconciliation_ok`, `model_profile_preserved`,
  demo + USD identity, 0 positions, 0 orders, 0 unresolved intents, no loss latch,
  no live agent cash entry, native backend, comment-ownership supported.
- Independent pre-cloud recheck immediately before activation (not via the console):
  MT5 MCP `get_trading_account_info` → `type: demo`, `currency: USD`, connection synchronized,
  build 6327; `get_trading_open_positions` → `positions: []`, `orders: []`; snapshot age 2.6 s;
  `/api/cost-reconciliation` → `ok: true`; guardian `running: true, enabled: true,
  latched: false, loss_streak: 0`; scheduler `running: false`; page cycle `active: false`.

## 3. Process-only activation; unchanged saved settings

- Model and profile reconstructed in the console **process environment only**:
  `inclusionai/ling-3.2-flash:free`, profile `ling-fast-advisory-v1`,
  provider `openrouter.ai`, backend `direct`. The API key was read from the owner-only key file
  and **never printed**.
- `ALLOW_TRADING=true` and `DEMO_CASH_POLICY=true` applied to this console process only;
  `PAPER_TRADING=false`.
- `.env` **not overwritten**: still mode `600`, both flags still `false`, mtime unchanged.
- Only the console was restarted (pid 18646); `serve_forever` starts **only** the DemoGuardian —
  scheduler `running=false, runs=0`, page cycle `active=false, busy=false`.
- Saved agents unchanged (3 agents, all `max_lot 0.02`, `max_risk 0.5`, allowlist XAUUSD,
  confidence gate 62). No strategy, SL, threshold or risk cap was modified.

## 4. Exactly one guarded invocation, one symbol

- Agent: guarded (`a76d1cff5b`), enabled, `symbols = [XAUUSD]` (exactly one), 24/7 hours window.
- Structural single-entry proof obtained before activation: `run()` iterates only over
  `agent["symbols"]` (length 1) and `_run_symbol` performs at most one `propose_open` → one
  `confirm`; the route holds `state.lock`, so no concurrent invocation.
- **One** budget window activated (300 s / 5 attempts, deadline starts at first attempt),
  then **exactly one** `POST /api/agents/a76d1cff5b {"action":"run"}` — no scheduler, no retry,
  no second invocation, no separate confirmation call. Invocation duration ~6.4 s.

## 5. One actual cloud attempt — HTTP 429

| Field | Value |
|---|---|
| Request kind | `agent_run` (attempt index 1 of the window) |
| Model | `inclusionai/ling-3.2-flash:free`, profile `ling-fast-advisory-v1` |
| Result | **HTTP 429**, error code `rate_limited` |
| Rate-limit origin | **upstream-reported** (`upstream_rate_limit_reported`, evidence `provider_error_statement`), `independently_verified: false` |
| Latency | **~6155 ms** |
| Token usage | **null / unknown** (failed request; not fabricated) |
| Fallbacks | **none** — stopped on the first provider error; no retry, no second attempt, no extra window, no model/provider fallback |

Engine behaviour: web path failed closed → decision `action=wait`, `source=model_error`,
`llm_status=failed`. No deterministic substitute proposal was produced.

## 6. Zero new proposals, reservations, orders and positions

- **New proposals created: 0** (proposal list unchanged; pre-existing rows are all expired or
  historical and were not touched).
- **New reservations/intents: 0** — `demo_cash_guard` shows `intents: {}`,
  `latched: false`, `streak: 0`.
- **Orders submitted: 0.** Broker state at close: `positions: []`, `orders: []`.
- **No broker acceptance test occurred** and **no exit test occurred**, because no order was
  ever sent and no position ever existed. Reported as *not applicable*, not as a pass.

## 7. Budget revoked; entry cycles stopped

- Window **revoked at 11:19:28 local**, after **1 / 5 attempts** (`state: revoked`).
- Scheduler `running: false`; page cycle `active: false`. **No further entry is authorized
  this session**, including after any future position closes.

## 8. Console/guardian left running, armed but idle

- Console (pid 18646) and the independent DemoGuardian left **running**; guardian is
  independent of the revoked budget window.
- Broker SL management and cash-exit policy remain armed but idle — no owned position exists.
- Cost monitor remains attached to its dedicated chart; snapshot continuing to refresh.

## 9. Evidence availability

| Evidence | Status |
|---|---|
| Read-only preflight output (15/15) | preserved locally (owner-only) |
| Monitor detection/restoration observations | preserved locally (owner-only) |
| Console session log | preserved locally (owner-only) |
| Raw agent-run response (contains provider error detail) | preserved locally (owner-only) |
| Usage ledger rows (model, kind, ok, error code, HTTP status, latency, attempt index) | preserved locally; sanitized counts in the JSON report |
| Budget transitions (exhausted → active → revoked) | preserved locally; sanitized state in this report |
| MT5 expert log for the session day | preserved locally (raw, owner-only) |
| Raw cost snapshot TSV and journal DB copy | preserved locally (raw, owner-only; never committed) |
| Account identifiers, balances, raw provider request/response bodies | **not included** — local only, excluded from chat and Git |
| Broker acceptance evidence | **unavailable — no order was submitted** |
| Position ownership/exit evidence | **unavailable — no position existed** |
| Token usage for the failed request | **unavailable (null)** — recorded as unknown |

## 10. Manifest (sanitized)

| Category | Included in sanitized report? | Preserved locally? |
|---|---|---|
| Preflight results (counts/status only) | yes (JSON report + §2) | yes |
| Monitor recovery (narrative + bounds) | yes (§1) | yes (observations file) |
| Console/guardian runtime state | yes (§3, §8) | yes (console log) |
| Budget transitions | yes (§4, §7) | yes |
| Agent outcome (one invocation, one symbol) | yes (§4) | yes (raw response) |
| Cloud attempt (429, upstream-reported, ~6155 ms, usage null) | yes (§5) | yes (usage ledger copy) |
| Proposal/order/position counts (all zero) | yes (§6) | yes (journal copy) |
| Broker acceptance / exit outcomes | marked unavailable (§6, §9) | not applicable |
| Commit + MT5 build | yes (header) | yes |
| Credentials, `.env`, OpenCode config, tokens, account IDs, raw financial/provider bodies | **no** | yes, owner-only, never committed |

## 11. Review note

Staged content for this commit was reviewed line by line: only the JSON report, this Markdown
report and the manifest are staged. `logs/` (including this session's archive directory) is
git-ignored; no ignored file was force-added. No credentials, tokens, environment values,
account identifiers or raw provider payloads appear in any staged file.

## Post-review correction (no new runtime observation)

The companion JSON originally marked `guardian` and `position_lifecycle` as
`pass`. These have been corrected to `not_run`: a running guardian thread is
not evidence of a successful owned-position exit, and no position lifecycle
was exercised in this session. The historical lifecycle counters are retained
as reported diagnostics, not live execution acceptance.

This correction changes report interpretation only. It does not alter the
original measurement timestamp, loaded commit, counts, local evidence or runtime
settings. The archived session and its process-only trading flags must not be
interpreted as current observations or continuing entry authorization.
