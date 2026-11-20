# Trend forecast deployment/verification (DEMO, safe/read-only)

- **Report id:** `20261120T151800Z-trend-forecast`
- **Generated (UTC):** 2026-11-20T16:17:00Z
- **Branch:** `arena/f81b6b27-trade-mcp`
- **Loaded/deployed commit:** `2ce423a83b133e64f750a9ca3ed81a009b058997` (ff-only through `2ce423a`)
- **Doc:** `docs/TREND-FORECAST.md` read and workflow completed.

## Preflight and deploy
- پیش از ری‌استارت: DEMO/USD **flat** (`positions=0, orders=0`)، guard durable **unresolved=0, not latched**، proposalهای موجود/تنظیمات ریسک حفظ شدند.
- کنسول با کد جدید ری‌استارت شد: **`ALLOW_TRADING=false`, `DEMO_CASH_POLICY=false`, `PAPER_TRADING=false`**، بودجه ابری **revoked**، page cycle/scheduler **متوقف**. پس از deploy نیز وضعیت ایمن تأیید شد.

## trend_forecast verification (یک درخواست تازه M15)
- درخواست: `GET /api/chart?symbol=XAUUSD&tf=M15&full=1` (تک درخواست). `symbol=XAUUSD`, `tf=M15`, **available=true**.
- **مسیر جهت‌دار واحد (آزمایشی):** up — نقاط **15/30/60 دقیقه** (رنگ UI: cyan/amber/rose) با قیمت‌های `3580.251 / 3580.642 / 3581.425`، جهت `up`.
- **Provenance/method/as-of:** `method=independent_32_bar_log_ols_anchored_trend`, `source=closed_broker_bars_not_llm`, `as_of=1795191300`, `last_closed_bar_time=1795190400`, `anchor_price=3579.86`, `validated_for_trading=false`, `execution_eligible=false`, warning: «پیش‌بینی آماری آزمایشی؛ خطا دارد و سیگنال سفارش نیست».
- **خطاهای تاریخی (تشخیصی، rolling-origin):** 15m `mae=6.802, no_change_mae=6.538, direction_accuracy=0.45 (n=20)`؛ 30m `9.848 / 8.740 / 0.40`؛ 60m `13.336 / 10.978 / 0.35` (نمونه کوچک/هم‌پوشان، نه اثبات تعمیم/احتمال کالیبره/سودآوری).
- **مقیاس قیمت/رندر (بررسی کد):** `terminal.js` قیمت‌های آینده را در مقیاس y لحاظ می‌کند، یک مسیر خط‌چین واحد در ناحیهٔ آیندهٔ مجزا با نقاط/رنگ‌های 15/30/60 و برچسب «پیش‌بینی آماری آزمایشی — نه سیگنال سفارش» رسم می‌کند. **بازرسی بصری مرورگر در این محیط در دسترس نیست** (صراحتاً اعلام می‌شود)؛ پاسخ API و منطق/کد رندر تأیید شد.
- **جدول volatility قدیمی:** همچنان سناریوی نوسان تشخیصی جدا (`independent_zero_drift_log_volatility_scenarios`, `calibrated=false`, `execution_eligible=false`) با برچسب صریح «غیرکالیبره/نه پیش‌بینی قطعی/نه سیگنال» باقی مانده و **به‌عنوان پیش‌بینی برچسب‌گذاری نشده** است.

## بازبینی شواهد 257 ارزیابی (بدون پنجرهٔ معاملاتی جدید)
- منبع: پنجرهٔ bounded rules v2 (20261120T151500Z، commit 0631ca7): **257** ارزیابی/run، **0** proposal جدید، **0** معامله/ورود، `halt_reason=null`.
- رد مشاهده‌شده: `action=wait, status=refused, blocked=true, blockers=["mode=guarded: confidence 50 < 62"], source=rules, used_llm=false` (نمونه: سوگیری فروش، امتیاز سوگیری ~-0.907، اعتماد سناریو 50).
- **توضیح بر اساس ورودی‌های استراتژی:** در `analysis.build_scenario` اعتماد جهت‌دار از پایهٔ **50** شروع و با هم‌راستایی (`max(0, alignment-0.5)*40`)، ADX (`(adx-20)*0.8` محدود)، جریمهٔ تضاد (-15) و رژیم نوسان (spiking -8، compressed -4) تعدیل و در [5,92] محدود می‌شود. در اجرای مشاهده‌شده اعتماد در **50** (< آستانهٔ guarded **62**) باقی ماند و blocker تضاد/volatility-spiking ثبت نشد — یعنی تعدیل‌های خالص اعتماد را بالاتر از پایه نبردند (سهم هم‌راستایی/ADX برای آن setup کافی نبود) و دروازهٔ پیش از proposal به‌درستی بدون ایجاد proposal/سفارش رد کرد. این نتیجهٔ آستانه/ورودی‌هاست، نه باگ تأیید اجرا.
- هیچ آستانه‌ای پایین آورده نشد، سیگنالی اختراع نشد و پنجرهٔ معاملاتی دیگری شروع نشد.

## تست‌ها
- هدفمند مرتبط (forecast, bounded, rules_action, agents, agent_sizing_and_guards): **pass**.
- اجرای کامل `pytest -q` در این محیط: **21 شکست** (مجموعهٔ حساس به محیط/`.env` و تعامل opencode-session در این محیط، مشابه الگوی مشاهده‌شده پیشین؛ منتسب به تغییرات trend_forecast نیست). وضعیت واقعی گزارش می‌شود.

## عدم ادعا
- پیش‌بینی ساعتی قیمت به‌عنوان نتیجهٔ اعتبارسنجی‌شده/قابل اتکا **ادعا نمی‌شود**. `executed_entries` اپلیکیشن **به‌تنهایی اثبات fill بروکر نیست**. بازرسی بصری مرورگر انجام نشد (عدم دسترسی).
