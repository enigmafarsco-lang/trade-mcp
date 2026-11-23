# Forecast suppression deploy/verification and test-hermeticity fix (DEMO, safe/read-only)

- **Report id:** `20261120T153000Z-forecast-suppress-and-test-hermeticity`
- **Generated (UTC):** 2026-11-20T16:36:00Z
- **Branch:** `arena/f81b6b27-trade-mcp`
- **Loaded/deployed commit:** `5a10aa78975715ea0f73dfe4aef0f52336e72f8c` (ff-only through `5a10aa7`)

## Deploy (ایمن)
- پیش‌پرواز تازه: DEMO/USD **flat** (`positions=0, orders=0`)، durable guard **unresolved=0, not latched**، داده/تنظیمات/proposalها حفظ شدند.
- کنسول با کد جدید ری‌استارت شد: **`ALLOW_TRADING=false`, `DEMO_CASH_POLICY=false`, `PAPER_TRADING=false`**، بودجه ابری **revoked**. وضعیت پس از deploy ایمن/flat تأیید شد.

## Verification (یک درخواست تازه M15): `GET /api/chart?symbol=XAUUSD&tf=M15&full=1`
- `trend_forecast.available=false`, `reason="failed_historical_baseline_check"`, `validated_for_trading=false`, `execution_eligible=false`, **بدون نقاط قابل رسم** (`points` absent) و **با diagnostics** (15/30/60m، n=20: MAE بالاتر/برابر no-change و direction_accuracy ≤ 0.5). سرکوب خطوط پیش‌بینی در شکست معیار مبنا (تشخیصی، نه اعتبارسنجی خارج‌نمونه/سودآوری) تأیید شد.
- **توضیح فارسی UI:** در `terminal.js` برای `failed_historical_baseline_check` پیام «پیش‌بینی پنهان شد: خطای تاریخی/جهت بهتر از مبنا نیست» نمایش داده می‌شود (بدون رسم خط). **بازرسی بصری مرورگر در این محیط در دسترس نیست**؛ API و منطق/کد رندر بررسی شد.
- **جدول volatility قدیمی:** رندر/جدول مقایسهٔ قدیمی از UI حذف شده است (سازگاری API `hourly_scenarios` حفظ، اما به‌عنوان جدول پیش‌بینی منسوخ رندر نمی‌شود).

## بررسی 21 شکست full-suite (بازتولید، بدون رد بدون علت)
- **مشاهده:** 21 شکست (cycle/research/terminal/usage/workspace) هنگام اجرای کامل pytest با `.env` محلی موجود.
- **بازتولید/علت ریشه‌ای:** بدون آلودگی `.env` محلی، اجرای کامل **100% pass** بود. با `.env` محلی (تنظیمات opencode-session وب)، `load_dotenv` محیط فرایند تست را آلوده و فرض‌های تست‌های web/direct را تغییر می‌داد؛ شکست‌ها بازتولید و به عدم ایزوله‌بودن تست در حضور `.env` محلی نسبت داده شدند (رد بدون بازتولید انجام نشد).
- **رفع نقص واقعی (test hermeticity) + تست رگرسیون:** `config.load_dotenv` تحت pytest (یا `TRADE_MCP_SKIP_DOTENV`) بارگذاری `.env` را نادیده می‌گیرد تا suite ایزوله بماند. تست‌های رگرسیون: `tests/test_config_dotenv.py`. **هیچ راز/secret runtime تغییر نکرد**.

## تست‌ها
- `tests/test_config_dotenv.py`: **pass**.
- اجرای کامل `pytest -q` (پس از fix، با `.env` محلی موجود): **pass** (فقط skipها). تست‌های مرتبط forecast/bounded/rules/agents نیز سبز.

## عدم ادعا و ایمنی
- پذیرش پیش‌بینی ساعتی یا پذیرش معاملهٔ خودکار **کامل اعلام نمی‌شود**. بدون پنجرهٔ معاملاتی جدید، بدون درخواست ابری، بدون تغییر آستانه، بدون پیش‌بینی جعلی. trading خاموش، بودجه ابری revoked.

## شواهد
- خام محلی (git-ignored، owner-only `700/600`): `logs/session-20261120T153000Z-forecast-suppress/{state.json,chart_m15.json}`
- sanitize‌شده (commit): این گزارش + JSON + manifest.
