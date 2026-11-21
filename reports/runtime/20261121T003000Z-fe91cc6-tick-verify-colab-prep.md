# fe91cc6 deploy/verification, tick preflight, and Colab notebook preparation

- **Report id:** `20261121T003000Z-fe91cc6-tick-verify-colab-prep`
- **Generated (UTC):** 2026-11-21T05:35:00Z
- **Branch:** `arena/f81b6b27-trade-mcp`
- **Loaded/deployed:** `fe91cc62bf6f2c566d95278055a7f8172d3fceac` (+ local clock-normalization fix)

## 1) Deploy ایمن و بررسی‌های execution-disabled (FIRST)
- پیش‌پرواز تازه قبل از ری‌استارت: DEMO/USD flat (`positions=0, orders=0`)، durable guard `unresolved=0, latched=false`. کنسول با کد جدید و **trading/policy OFF** مستقر شد.

### بررسی clock کندل M1 (Native)
- `time_utc` موجود است، `time_source=broker_history_clock_normalized`، روی مرز دقیقه (`%60==0`)، offset legacy→UTC = **-12600s** (UTC+3:30) — نرمال‌سازی فرمت/افست صحیح است.
- **باگ تشخیص/رفع شد:** `trade_server_last_known_time` می‌تواند stale باشد (بسته‌شدن بازار)؛ افست اکنون `local_time` نسبت به `utc_time` را ترجیح می‌دهد. تست رگرسیون: `tests/test_native_bridge.py::test_stale_trade_server_last_known_time_does_not_break_utc_normalization` **pass**.
- `matches_current_utc_minute=false` در بررسی زنده: آخرین کندل M1 نسبت به دقیقهٔ UTC فعلی stale است (بازار بسته/بدون کندل forming فعلی). این وضعیت بازار است، نه باگ فرمت/افست؛ **چک forming-candle bypass نشده** و `tick_momentum` به‌درستی `forming_M1_required` می‌دهد.

### provenance مهر زمانی Quote
- `quote.time_source="received"` — گزارش صادقانه. `tick_momentum` برای این منبع `evidence_kind='received_quote_samples'` را برمی‌گرداند (نه ادعای تیک تأییدشدهٔ بروکر).

### دلایل refusal تیک (غیر از other_guard)
- مشاهدهٔ dry (بدون اجرا): `{"action":"wait","reason":"forming_M1_required"}` — دلیل مشخص حفظ شده، به `other_guard` collapse نشده.
- تست: `tests/test_bounded.py::test_tick_refusals_are_not_lost_in_other_guard` **pass**.

## 2) پیش‌پرواز کامل DEMO/guardian/cost
- بروکر DEMO/USD flat، intents unresolved=0، latch=false.
- `cost_reconciliation`: **`ok=false, error=cost_check_failed, stage=snapshot`** (snapshot قدیمی/ستیل، ~12.4h در بررسی) — وابستگی محیطی. **پیش‌پرواز کامل pass نشد.**

## 3) پنجرهٔ tick (مجاز تک‌بار)
- **اجرا نشد** چون پیش‌پرواز کامل DEMO/guardian/cost pass نشد. بدون retry/پنجرهٔ دوم/سیگنال اجباری/تغییر حد ریسک. اگر باز شود، guardian+SL حفظ می‌شود (فعلاً موقعیت باز نیست).

## 4) Kronos/Freqtrade
- اجرای واقعی upstream ابری **تکمیل نشده** (وابستگی اجرای خارجی؛ worker/وزن/آرتیفکت provision‌شدهٔ محلی موجود نیست). ادعای اجرا/پیش‌بینی واقعی بدون worker انجام نشده است.

## 5) نوت‌بوک‌های Colab (آماده‌شده، نه اجراشده)
- `notebooks/kronos_colab.ipynb`: فقط Colab، نصب/دانلود وزن فقط در Colab، آپلود OHLCV redact‌شده، اجرای واقعی upstream Kronos (پین `67b630e67f6a18c9e9be918d9b4337c960db1e9a`)، ارزیابی hold-out و مقایسه baseline، خروجی artifact سازگار با trade-mcp (60 کندل M1 آینده)، بدون جعل نتیجه/بدون سرور عمومی/بدون سفارش MT5.
- `notebooks/freqtrade_strategy001_colab.ipynb`: فقط Colab، Strategy001 واقعی 5m upstream (پین `f3340ce11f5bdf62f598522e64d1f5638eaa13f5`, SHA256 تأیید قبل از اجرا)، GPL-3.0 رعایت‌شده/بدون vendoring، ارزیابی تحقیقاتی hold-out/baseline، خروجی تحقیقاتی؛ **M1 tick momentum نیست** و مجوز اجرای MT5 ندارد.
- تست آفلاین: `tests/test_colab_notebooks.py` **pass**. راهنمای فارسی: `docs/COLAB-NOTEBOOKS-FA.md`.
- **وضعیت:** نوت‌بوک‌ها آماده هستند؛ **اجرای ابری تکمیل‌شده محسوب نمی‌شود** مگر واقعاً در Colab اجرا شوند.

## تست‌ها
- هدفمند: **pass**. full suite: **pass** (skips مطابق محیط).

## شواهد
- خام محلی (git-ignored، owner-only): `logs/session-20261121T003000Z-fe91cc6-tick-verify/{verify_disabled.json,verify_disabled_after_fix.json,...}`
- sanitize‌شده: این گزارش + JSON + manifest.
