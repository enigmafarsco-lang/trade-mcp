# اکسپرت‌های trade-mcp (MQL5)

سه فایل در این پوشه هست. فقط **یکی** را روی چارت بگذار:

| فایل | چه می‌کند | کِی استفاده کن |
|---|---|---|
| **`TradeMCPFile.mq5`** | پل فایلی: درخواست را از `MQL5/Files/trade-mcp/in/` می‌خواند و پاسخ را در `out/` می‌نویسد | **همیشه** — مسیر اصلی |
| `TradeMCPConnector.mq5` | کلاینت TCP (نسخه‌ی قدیمی) | فقط اگر پورت می‌خواهی؛ به فهرست مجاز MT5 نیاز دارد |
| `TradeMCPProbe.mq5` | فقط دست‌دادن فایلی را تست می‌کند و هر درخواست را با یک پاسخ ثابت جواب می‌دهد | عیب‌یابی |

## چرا پل فایلی جدا شد

`SocketConnect`/`WebRequest` در بیلدهای جدید MT5 به «فهرست مجاز» (`Ctrl+O → Expert
Advisors`) نیاز دارند که در فایل رمز‌شده‌ی `Config` نگه داشته می‌شود؛ زیر Wine عملاً
دست‌نیافتنی است و هر تلاش با `err=4014` برمی‌گشت. خواندن/نوشتن داخل `MQL5/Files` هیچ
مجوزی نمی‌خواهد، پس پل فایلی هم روی ویندوز و هم زیر Wine کار می‌کند.

نسخه‌ی ۱.۲۰ و ۱.۲۱ این پل را داخل همان فایل سوکت جا داده بودند و روی بعضی بیلدها
MetaEditor وسط کامپایل با `fatal compiler error: unknown error` کرش می‌کرد (خطای کد
نبود). برای همین پل فایلی به یک فایل مستقل و مینیمال منتقل شد:

- بدون `#include` (نه `Trade\Trade.mqh`)
- بدون `class` (به‌جای `CJson` فقط چند تابع ساده)
- بدون `enum` سفارشی، بدون آرگومان پیش‌فرض، بدون `do-while`
- بدون کد سوکت

## نصب

1. `TradeMCPFile.mq5` را در `MQL5/Experts/` ترمینال کپی کن
   (`File → Open Data Folder`).
2. در MetaEditor `F7` بزن. باید `0 errors, 0 warnings` بدهد.
3. در MT5 یک چارت باز کن و اکسپرت را از Navigator روی چارت بکش
   (هر نماد/تایم‌فریمی باشد؛ همان یک نمونه همه‌ی نمادها را سرو می‌کند).
4. در دیالوگ: **Allow Algo Trading** ✔ — و دکمه‌ی **Algo Trading** نوار ابزار سبز باشد.
5. تب **Experts** باید بنویسد:

```
[trade-mcp] 1.00 started | folder=MQL5\Files\trade-mcp | trading=ENABLED | chart=XAUUSD M15
```

اگر این خط را ندیدی، اکسپرت روی چارت نیست یا Algo Trading خاموش است (بدون آن `OnTimer`
اجرا نمی‌شود).

## ورودی‌ها (`TradeMCPFile.mq5`)

| ورودی | پیش‌فرض | توضیح |
|---|---|---|
| `InpDir` | `trade-mcp` | زیرپوشه‌ی `MQL5/Files` |
| `InpPollMs` | `150` | دوره‌ی خواندن `in/` (ms) |
| `InpStatusMs` | `1000` | دوره‌ی به‌روزرسانی `status.json` (ms) |
| `InpVerbose` | `true` | لاگ هر درخواست در تب Experts |
| `InpAllowTrading` | `true` | **کلید سخت**: اگر `false` باشد هیچ سفارشی ارسال نمی‌شود |
| `InpMagic` | `20251112` | مجیک‌نامبر |
| `InpDeviation` | `30` | حداکثر اسلیپیج (point) |
| `InpComment` | `trade-mcp` | کامنت پیش‌فرض سفارش |
| `InpMaxRatesCount` | `5000` | سقف کندل در هر درخواست |
| `InpMaxSymbols` | `2000` | سقف نماد در هر درخواست |
| `InpMaxPerTick` | `16` | حداکثر درخواست در هر دور تایمر |

برای ایمنی بیشتر: `InpAllowTrading=false` بگذار و فقط وقتی می‌خواهی معامله کنی روشنش کن.
این مستقل از `TRADE_MCP_ALLOW_TRADING` در پایتون است؛ **هر دو** باید روشن باشند.

## نکته‌های فنی

- فایل‌ها با `FileOpen/FileReadArray/FileWriteArray` و UTF-8 خوانده/نوشته می‌شوند؛
  پوشه‌های `trade-mcp/in` و `trade-mcp/out` را خود اکسپرت می‌سازد.
- `status.json` هر ثانیه بازنویسی می‌شود؛ `updated_at` همان لحظه به وقت UTC است و
  `alive` در `OnDeinit` روی `false` می‌رود.
- حداکثر `InpMaxPerTick` درخواست در هر تیک برداشته می‌شود تا چارت قفل نشود.
- سفارش‌ها با `OrderSend` خام (بدون `CTrade`) ارسال می‌شوند؛ `type_filling` از
  `SYMBOL_FILLING_MODE` نماد انتخاب می‌شود (IOC → FOK → RETURN).
- اگر پایتون بالا نباشد، اکسپرت کاری نمی‌کند و فقط `status.json` را به‌روز می‌کند —
  بدون خطا، بدون تلاش دوباره.
- تأخیر هر فرمان حدود ۱۵۰ تا ۵۰۰ میلی‌ثانیه است (دوره‌ی `InpPollMs` + نوشتن فایل).

## عیب‌یابی

| نشانه | کار |
|---|---|
| اکسپرت روی چارت است ولی `status.json` ساخته نمی‌شود | Algo Trading خاموش است؛ دکمه‌ی نوار ابزار را سبز کن |
| `doctor` می‌گوید `no status.json yet` | `InpDir` با `TRADE_MCP_FILES_DIR` یکی نیست (پوشه‌ی `Files` را چک کن) |
| `status.json is stale` | ترمینال بسته شده یا اکسپرت Remove شده؛ اکسپرت را دوباره Attach کن |
| `trading_disabled` | `InpAllowTrading` یا AutoTrading یا `TRADE_MCP_ALLOW_TRADING` خاموش است |
| `fatal compiler error: unknown error` در `F7` | اول `TradeMCPProbe.mq5` را کامپایل کن؛ اگر آن هم کرش کرد مشکل محیط است (`pkill -f metaeditor64` و `wineserver -k`) |

جزئیات پروتکل: [../docs/FILE-BRIDGE.md](../docs/FILE-BRIDGE.md)
