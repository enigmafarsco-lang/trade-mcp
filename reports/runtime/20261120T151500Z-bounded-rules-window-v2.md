# Bounded guarded rules window v2 — pending-proposal flood correction (DEMO, authorized)

- **Report id:** `20261120T151500Z-bounded-rules-window-v2`
- **Generated (UTC):** 2026-11-20T15:31:00Z
- **Branch:** `arena/f81b6b27-trade-mcp`
- **Loaded/executed commit:** `0631ca7` (ff-only through `0631ca7`, includes `d411673`)
- **Doc:** `docs/BOUNDED-RULES-WINDOW.md` (Pending-proposal flood correction) read

> Executed under explicit user authorization in this session (single local DEMO bounded workflow). `execution_authorized_by_report: false` in schema; cloud off, other entry cycles stopped, no retry/second window, no forced signal/threshold change, existing journal proposals preserved.

## Fix deployed
- Explicit rules endpoints use `LLMClient(mode="rules")` (no provider config), independent of web OpenCode human-approval gate under explicit demo cash policy. Window halts on first `pending_confirmation` or evaluation error and never repeats pending duplicates; response includes `halt_reason`, `pending_entries`, `executed_entries` (application statuses, not independently reconciled broker fills).

## Preflight (fresh, complete)
- DEMO/USD (MetaQuotes-Demo), **positions=0, orders=0**, durable guard **unresolved=0, not latched, streak=0**.
- Cost snapshot/reconciliation **ok** (fresh). Budget **revoked**, paid/cloud use off, scheduler/page cycle **stopped**.
- Console (process-only, updated code): `ALLOW_TRADING=true`, `DEMO_CASH_POLICY=true`, `PAPER_TRADING=false`; independent guardian **healthy with fresh poll**. Existing pending proposals **preserved** (250).

## Rules client mode verification
- **Loaded mode:** `rules` (not `auto`). Evidence: `run_rules_window` constructs `LLMClient(mode="rules")` with explicit demo policy; agent runs show `used_llm=false`, `source=rules`, guarded pre-proposal refusals without provider/auto path and without pending proposal creation.

## Invocation (once)
- `POST /api/agents/a76d1cff5b` (`run_rules_window`, `duration_seconds=300`) — agent `a76d1cff5b` تک‌نماد **XAUUSD**, **guarded/enabled**, `max_lot=0.02`, `max_risk=0.5`.

## Result (actual broker/journal evidence, not HTTP 200 alone)
- HTTP **200**: `{"ok":true,"evaluations":257,"stopped":false,"duration_limit_seconds":300,"orders_guaranteed":false,"executed_entries":0,"pending_entries":0,"halt_reason":null}` (elapsed ~300s).
- **257** ارزیابی، **257** agent run، **0** proposal جدید در پنجره، **pending proposals total پس از اجرا: 250** (حفظ‌شده). **0** معامله/ورود بروکر، **0** خطا، `positions=0, orders=0`.
- تصمیم‌ها (نمونه): `action=wait, status=refused, blocked=true, blockers=["mode=guarded: confidence 50 < 62"], source=rules, used_llm=false, proposals_count=0` — blocker پیش از proposal مانع ایجاد proposal شد؛ بنابراین **flood proposal pending رخ نداد**، `halt_reason` null باقی ماند (halt ناشی از pending/خطا نبود) و هیچ ورود بروکری انجام نشد. تداخل مالکیت/intent حل‌نشده ایجاد نشد.

## Postflight / state
- پنجره تکمیل‌شده تأیید شد. بروکر **flat**، durable guard **unresolved=0, not latched**. پوزیشن بازی نبود؛ guardian+SL موقعیت باز موضوعیت نداشت. کنسول به حالت ایمن بازگردانده شد: **`ALLOW_TRADING=false`, `DEMO_CASH_POLICY=false`, `PAPER_TRADING=false`**.

## Claims (صریح)
- پیش‌بینی ساعتی قیمت **پیاده‌سازی نشده** است. `executed_entries` اپلیکیشن **به‌تنهایی اثبات fill بروکر نیست** (در این اجرا 0 و هیچ ورود/پر شدن بروکری مشاهده نشد).

## Evidence
- Raw (local, git-ignored, owner-only `700/600`): `logs/session-20261120T151500Z-bounded-rules-window-v2/{preflight.json,postflight.json}`
- Sanitized committed: this report + JSON + manifest.
