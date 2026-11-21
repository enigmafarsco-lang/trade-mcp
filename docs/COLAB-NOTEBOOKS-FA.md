# نوت‌بوک‌های Google Colab (Kronos / Freqtrade Strategy001)

این نوت‌بوک‌ها **آماده‌شده** هستند؛ تا زمانی که واقعاً در Colab اجرا نشده‌اند، «اجرای ابری تکمیل‌شده» محسوب نمی‌شوند.

## فایل‌ها
- `notebooks/kronos_colab.ipynb` — پیش‌بینی ۶۰ کندل M1 با Kronos upstream (پین‌شده) + ارزیابی hold-out و خروجی artifact سازگار با `trade-mcp`.
- `notebooks/freqtrade_strategy001_colab.ipynb` — ارزیابی تحقیقاتی Strategy001 واقعی upstream (5m، GPL-3.0، پین‌شده)؛ **M1 tick momentum نیست**.

## قوانین مهم
- **فقط در Google Colab اجرا شود.** نصب وابستگی/دانلود وزن مدل فقط داخل runtime کولب. روی ماشین محلی MT5 نصب/دانلود نکنید.
- داده ورودی فقط OHLCV redact‌شده: بدون credential، حساب، ژورنال، توکن، اطلاعات شخصی.
- در صورت خطای inference/وابستگی، **نتیجه جعلی تولید نکنید**.
- خروجی‌ها تحقیقاتی/تشخیصی هستند؛ نه قیمت واقعی تضمین‌شده، نه اثبات سودآوری، نه مجوز معامله خودکار MT5.
- Colab را به‌عنوان سرور عمومی expose نکنید و سفارش/guardian MT5 را در Colab اجرا نکنید.

## Kronos (خروجی سازگار با trade-mcp)
1. نوت‌بوک را در Colab باز کنید.
2. سلول راه‌اندازی را اجرا کنید (clone پین‌شده + نصب Colab-only).
3. فایل JSON redact‌شده M1 آپلود کنید: `{"symbol":"XAUUSD","rows":[[epoch,open,high,low,close,volume], ...]}` (>=180 کندل بستهٔ پیوسته M1).
4. اجرای واقعی upstream انجام می‌شود؛ hold-out 60 کندل و مقایسه با baseline محاسبه می‌شود.
5. اگر معیار baseline pass شد، artifact JSON دانلود می‌شود. فایل را owner-only به ماشین محلی منتقل کنید و `TRADE_MCP_KRONOS_FORECAST_PATH` را به آن اشاره دهید. artifact پس از 120 ثانیه stale می‌شود؛ بدون اجرای واقعی معتبر، artifact جعلی نسازید.

## Freqtrade Strategy001
1. نوت‌بوک را در Colab باز کنید.
2. clone پین‌شده + بررسی SHA256 قبل از اجرا انجام می‌شود.
3. فایل JSON redact‌شده 5m آپلود کنید (>=200 کندل بستهٔ پیوسته 5m).
4. سیگنال/ارزیابی تحقیقاتی hold-out تولید و JSON دانلود می‌شود. این خروجی **مجوز اجرای MT5 نیست**.

## مجوز/سازگاری
- Kronos upstream: MIT (commit پین‌شده در نوت‌بوک).
- Freqtrade strategies: GPL-3.0؛ سورس vendored نشده، در Colab clone می‌شود. پیش از توزیع محصول ترکیبی، تعهدات GPL را بررسی کنید.

### اصلاح بازبینی
نوت‌بوک Kronos وابستگی‌ها را از `requirements.txt` همان checkout پین‌شده نصب
می‌کند؛ نصب فقط در runtime ابری است، نه سیستم MT5. نوت‌بوک همچنان اجراشده نیست.

ساعت محلی کامپیوتر، ساعت بروکر نیست. در نبود افست قابل‌قبول بروکر، مسیر تیک
با `broker_clock_unverified` متوقف می‌شود؛ بسته‌بودن بازار نباید با جایگزینی
ساعت سیستم به کندل فعلی ساختگی تبدیل شود. stale بودن snapshot نیز جداگانه
بررسی می‌شود. این اصلاح، دسترسی Google یا GPU رایگان ایجاد نمی‌کند.
