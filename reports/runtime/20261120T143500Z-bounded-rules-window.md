# Bounded guarded rules evaluation window (DEMO, authorized)

- **Report id:** `20261120T143500Z-bounded-rules-window`
- **Generated (UTC):** 2026-11-20T15:03:00Z
- **Branch:** `arena/f81b6b27-trade-mcp`
- **Loaded/executed commit:** `d4f687d` (ff-only from `6b8d0af` after fetch; includes `c956c9b`, docs `docs/BOUNDED-RULES-WINDOW.md` read)

> Executed under explicit user authorization in this session (single local DEMO bounded workflow). `execution_authorized_by_report: false` in schema; no second window, no retry on timeout, no other entry calls, no forced trades.

## Preflight (fresh)
- DEMO/USD (MetaQuotes-Demo), **positions=0, orders=0**, durable guard **unresolved=0, not latched, streak=0**.
- Cost snapshot present/fresh; `cost_reconciliation` **ok** (passing/diagnostic reconciliation at check).
- Cloud budget **revoked**, paid APIs inactive, scheduler/page cycle **stopped**.
- Console started (process-only, updated code): `ALLOW_TRADING=true`, `DEMO_CASH_POLICY=true`, `PAPER_TRADING=false` (cost snapshot path set). Independent guardian **healthy with fresh poll** (`ok/enabled/running`, no blocker, unresolved=0).

## Invocation (once)
- `POST /api/agents/a76d1cff5b` (`run_rules_window`, `duration_seconds=300`) — agent `a76d1cff5b` تک‌نماد **XAUUSD**, **guarded/enabled**, `max_lot=0.02`, `max_risk=0.5`. بدون retry در timeout، بدون فراخوانی ورود دیگر.

## Result
- HTTP **200**: `{"ok":true,"evaluations":244,"stopped":false,"duration_limit_seconds":300,"orders_guaranteed":false}` (elapsed ~300s).
- **244** ارزیابی تکمیل‌شده، **244** agent run، **244** proposal ایجادشده (همه `pending/open`, sell XAUUSD 0.02)، **0** معامله/ورود بروکر، **0** خطا. `used_llm=false` (rules).
- **Refusals/اجرا:** هیچ سفارش بروکری ارسال نشد؛ تصمیم‌های guarded rules به proposal در حالت pending (نیازمند تأیید صریح انسانی، بدون auto-confirm) منجر شدند. هیچ تداخل مالکیت/intent حل‌نشده ایجاد نشد. گیت‌های signal/risk/lot/SL/ownership حفظ و هیچ معامله‌ای تحمیل نشد.

## Postflight / state
- پنجره متوقف/تکمیل‌شده تأیید شد. بروکر **flat** (`positions=0, orders=0`)، durable guard **unresolved=0, not latched**.
- پوزیشن بازی باقی نماند → حفظ guardian+SL موقعیت باز موضوعیت نداشت؛ کنسول به حالت ایمن بازگردانده شد: **`ALLOW_TRADING=false`, `DEMO_CASH_POLICY=false`, `PAPER_TRADING=false`**.

## Evidence
- Raw (local, git-ignored, owner-only `700/600`): `logs/session-20261120T143500Z-bounded-rules-window/{preflight.json,postflight.json}`
- Sanitized committed: this report + JSON + manifest.
