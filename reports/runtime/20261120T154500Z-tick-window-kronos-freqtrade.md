# Tick window (M1 momentum) + Kronos/Freqtrade dependency check (DEMO, authorized)

- **Report id:** `20261120T154500Z-tick-window-kronos-freqtrade`
- **Generated (UTC):** 2026-11-20T16:56:30Z
- **Branch:** `arena/f81b6b27-trade-mcp`
- **Loaded/executed commit:** `1b2fa59b3967853e472001e19c8db60d55306655` (includes `9635e19`)
- **Doc:** `docs/TICK-KRONOS-FREQTRADE.md` read and supported local workflow followed.

> Executed under explicit user authorization in this session. `execution_authorized_by_report: false` in schema; one invocation only, no retry/second window, no forced signal/threshold change, no `run_rules_window`.

## Tests
- `pytest -q` (full suite): **pass** (skips as applicable).

## Preflight (fresh, complete) and guardian
- DEMO/USD (MetaQuotes-Demo), **positions=0, orders=0**, durable guard **unresolved=0, not latched, streak=0**.
- Cost snapshot/reconciliation: v2 source present, `cost_reconciliation` **ok**.
- Guardian مستقل **سالم با poll تازه** (`ok/enabled/running`, بدون blocker، unresolved=0).
- بودجه ابری **revoked**، page cycle/scheduler و سایر چرخه‌های ورود **متوقف**. اجرای demo فقط پس از پیش‌پرواز در فرایند کنسول فعال شد (`ALLOW_TRADING=true`, `DEMO_CASH_POLICY=true`, `PAPER_TRADING=false`).

## Kronos/Freqtrade (فقط وابستگی‌های ابری ازپیش‌موجود بررسی شد)
- **هیچ worker/وابستگی ابری ازپیش‌تأمین‌شده محلی موجود نیست:** checkout پین‌شدهٔ Kronos + دایرکتوری‌های tokenizer/checkpoint و/یا محیط ابری تأییدشدهٔ Strategy001 (Freqtrade) محلی یافت نشد؛ `TRADE_MCP_KRONOS_FORECAST_PATH` تنظیم/آرتیفکت موجود نیست. این وابستگی دقیق ثبت شد؛ هیچ دانلود وزن/نصب محلی انجام نشد و نتیجه‌ای جعل نشد. **تست‌های adapter استنتاج/اجرای واقعی upstream Kronos/Freqtrade نیستند.** کار tick-strategy به‌صورت مستقل ادامه یافت.

## فراخوانی یکتا (tick strategy کاربر)
- `POST /api/agents/a76d1cff5b` با `{"action":"run_tick_window","duration_seconds":300,"consecutive_ticks":3}` (ایجنت تأییدشدهٔ تک‌نماد **XAUUSD**، enabled، `max_lot=0.02`, `max_risk=0.5`). بدون retry/پنجرهٔ دوم/سایر فراخوانی ورود.

## نتیجهٔ پنجره (واقعی)
- HTTP **200**: `{"ok":true,"evaluations":290,"stopped":false,"duration_limit_seconds":300,"orders_guaranteed":false,"executed_entries":0,"pending_entries":0,"halt_reason":null,"refusal_counts":{"other_guard":290}}` (elapsed ~300s).
- **ارزیابی‌ها:** 290، **executed/pending entries:** 0، **proposal/trade جدید در پنجره:** 0. refusalها `other_guard` (شرایط گیت tick-momentum: تغییرات متوالی تازهٔ دوسویهٔ quote همسو با کندل M1 در حال تشکیل، بدون stale/duplicate/gap/reset/exposure/رد proposal در این بازه برآورده نشد). بدون تغییر آستانه/سیگنال اجباری.

## بروکر، پر شدن‌ها، مالکیت، سیاست USD2 persisted، خروج‌ها
- **پر شدن‌های واقعی بروکر:** **0**. **معامله/ورود اجراشده:** 0. **positions/orders پس از اجرا:** 0.
- **مالکیت:** موقعیت/intent جدید ایجاد نشد؛ intent durable موجود بدون تغییر (بسته، سابقهٔ مالکیت قبلی). تداخل مالکیت/intent حل‌نشده ایجاد نشد.
- **سیاست ضرر USD2 persisted:** در این اجرا ورودی انجام نشد، بنابراین intent جدید با cap ضرر USD2 persisted نشد. intent موجود legacy/بسته باقی مانده است.
- **خروج‌ها:** در این پنجره خروجی رخ نداد (بدون موقعیت/ورود).

## پس از اجرا / ایمنی
- بروکر **flat** (`positions=0, orders=0`)، durable guard **unresolved=0, latched=false**. موقعیت بازی نبود؛ حفظ guardian+SL بروکر موضوعیت نداشت. کنسول به حالت ایمن بازگردانده شد: **`ALLOW_TRADING=false`, `DEMO_CASH_POLICY=false`, `PAPER_TRADING=false`**.

## عدم ادعا
- تست‌های adapter استنتاج/اجرای واقعی upstream **نیستند**. پذیرش پیش‌بینی ساعتی یا پذیرش معاملهٔ خودکار **کامل اعلام نمی‌شود**.

## شواهد
- خام محلی (git-ignored، owner-only `700/600`): `logs/session-20261120T154500Z-tick-window/{preflight.json,postflight.json}`
- sanitize‌شده (commit): این گزارش + JSON + manifest.
