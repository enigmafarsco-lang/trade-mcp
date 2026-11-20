# Guard intent reconciliation fix (unresolved accepted-order intent)

- **Report id:** `20261120T130300Z-guard-intent-reconciliation-fix`
- **Generated (UTC):** 2026-11-20T13:03:00Z
- **Base commit:** `9e51d7c76bafd0bf7646f2eca14aab1c8fceb8f7` (`arena/f81b6b27-trade-mcp`)
- **Scope:** fix unresolved accepted-order intent via supported source changes; reconcile existing intent through supported application path in **no-order mode**. No new entry, agent invocation, cloud request, budget activation, or forced close. No reset/rebase/manual DB repair.

> `execution_authorized_by_report: false`.

## 1) Preconditions and safe state
- Fresh verification: **DEMO/USD**, broker **positions = 0, orders = 0** (flat). Unresolved durable intent/journal/evidence preserved unchanged before fix.
- Console restarted (safe deploy) with **trading = false, demo cash policy = false** (no-order mode). No position open, so guardian protection/flags were not required to remain enabled.

## 2) Actual cause (end-to-end trace)
1. **Accepted broker result → durable reservation:** guarded order accepted; intent reserved/recorded as `awaiting_position` with comment-reservation ownership (nonce marker).
2. **Position identity / lifecycle:** owned position opened and **closed between guardian polls** (broker later flat).
3. **Native/history normalization:** normalized opening-deal time for the owned deal was unavailable/unparseable (`0` epoch), while intent `started_at` was valid.
4. **Guardian reconciliation block:** `inspect_positions` required `opening_deal_time >= intent.started_at - 5` for comment-reservation and raised `Reserved opening comment and time must match position history` even though the opening deal was uniquely identifiable by the reserved marker and owned lifecycle correlation. Guardian polling caught this as generic reconciliation failure, leaving the intent durably **`awaiting_position` (unresolved, entry-blocking)**.

**Cost completeness (incident):** with v2 cost snapshot correlation/enrichment, the owned lifecycle costs were complete (all required fields present). No costs were fabricated.

## 3) Supported fix (source/tests/docs path)
- **Identity/time correlation:** accept unique comment-reservation ownership when opening-deal time is unavailable/unreliable, while preserving uniqueness and still rejecting a valid comparable time that mismatches. **Never** infer ownership from symbol/side, adopt unrelated trades, or discard an unresolved intent.
- **Read-only reconciliation separated from order submission:** reconciliation of existing durable intents can run in no-order mode (`reconcile_existing`), without enabling broker order submission/execution or demo-cash-policy entry mode. Ambiguous/incomplete/unknown-cost outcomes remain **entry-blocking**; **unknown realized net P/L is not marked zero**.
- **Sanitized guardian health:** exposes last poll, last successful reconciliation, current blocker code, and unresolved intent count (plus latched/streak summary) without raw exceptions or records. Console status/API and workspace UI updated.
- **Supported no-order reconcile action:** `POST /api/demo-cash-guard {"action":"reconcile"}` performs read-only reconciliation via the application path.
- **Regression coverage (redacted fixtures):** incident case (closed between polls, unparseable deal time, complete costs) reconciles; accepted-while-open reconciles to open/tracked; missing/changed comment remains unresolved; unknown costs preserved/blocked (not zero); partial/incomplete history blocked; process-restart/repeated reconciliation idempotent; sanitized guardian health verified.

## 4) Reconciliation outcome (existing intent, no-order mode)
- **Path:** supported console reconcile action with `trading=false`, `demo_cash_policy=false` (no order submission).
- **Intent:** **closed** (durable). **Unresolved intents: 0**. Broker flat (0 positions, 0 orders).
- **Ownership/identity:** uniquely matched by reserved comment marker with correlated owned lifecycle evidence (not symbol/side inference). Position linkage recorded.
- **Realized net P/L:** **cost-complete** net P/L recorded in durable state (exact amount excluded from Git; not fabricated). Loss latch **false**, streak **0**.
- **No new entry, no cloud/budget use, no forced close, no latch/history rewrite.**

## 5) Verification
- `ruff check src tests`: **pass**
- Full offline `pytest -q`: **pass** (including new regressions)
- `py_compile`: **pass**
- JS: `node --check` reports pre-existing nullish-coalescing syntax incompatibility under the available Node runtime (not introduced by this change); no JS lint config present.

## 6) Remaining blockers
- **None** for this incident intent (reconciled, flat, no unresolved intents, no latch).
- Prior lack of exposed guardian poll/health detail addressed via sanitized health fields.
- Installed Node runtime cannot parse pre-existing modern JS syntax in web assets (environment/tooling note only).

## 7) Evidence and commits
- Local raw evidence (owner-only, `700/600`, git-ignored): `logs/session-20261120T130300Z-guard-fix-reconcile/` (sanitized final state, checks summary, console tail). No credentials/tickets/exact prices/raw financial records committed.
- Preserved commits: `91b61d1a13ec7d2a9e2bf816a90a80c474d2aea4`, `9e51d7c76bafd0bf7646f2eca14aab1c8fceb8f7`.
- Source/tests/docs and this sanitized report committed explicitly on branch; push attempted (see result).
