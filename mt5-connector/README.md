# TradeMCPConnector (MQL5)

اکسپرت سمت متاتریدر. دو حالت حمل دارد و در هر دو، JSON را می‌خواند و دستور را روی API
متاتریدر اجرا می‌کند:

- **`file` (پیش‌فرض، نسخه ۱.۲۰+):** پایتون درخواست را در `MQL5/Files/trade-mcp/in/`
  می‌گذارد، اکسپرت پاسخ را در `out/` می‌نویسد و هر ثانیه `status.json` را به‌روز می‌کند.
  هیچ پورت و هیچ مجوز شبکه‌ای لازم نیست — همین حالت زیر Wine مطمئن است.
- **`socket`:** کلاینت TCP؛ به پایتون وصل می‌شود. این حالت به «فهرست مجاز» MT5
  (`Ctrl+O → Expert Advisors`) نیاز دارد، وگرنه با `err=4014` رد می‌شود.

## نصب

1. `TradeMCPConnector.mq5` را در `MQL5/Experts/` ترمینال کپی کن
   (`File → Open Data Folder`).
2. در MetaEditor `F7` بزن. باید بدون خطا کامپایل شود.
3. در MT5 چارت را باز کن، اکسپرت را از Navigator روی چارت بکش.
4. در دیالوگ: **Allow Algo Trading** ✔ — و دکمه‌ی **AutoTrading** نوار ابزار سبز باشد.
5. در تب **Inputs** مقدار `InpTransport` را انتخاب کن: `file` (پیشنهادی) یا `socket`.
6. تب **Experts** باید نشان دهد — حالت فایل:
   `[trade-mcp] 1.20 file transport | folder=MQL5\Files\trade-mcp`
   و حالت سوکت: `[trade-mcp] connected to 127.0.0.1:8787`.

## ورودی‌ها

| ورودی | پیش‌فرض | توضیح |
|---|---|---|
| `InpTransport` | `file` | `file` = پوشه‌ی مشترک، `socket` = TCP |
| `InpFileDir` | `trade-mcp` | زیرپوشه‌ی `MQL5/Files` برای پل فایلی |
| `InpFilePollMs` | `150` | دوره‌ی خواندن `in/` (ms) |
| `InpStatusMs` | `1000` | دوره‌ی به‌روزرسانی `status.json` (ms) |
| `InpHost` | `127.0.0.1` | آدرس پایتون (فقط حالت سوکت) |
| `InpPort` | `8787` | پورت پایتون (`TRADE_MCP_PORT`) |
| `InpConnectTimeout` | `1500` | تایم‌اوت اتصال (ms) |
| `InpRetrySeconds` | `3` | فاصله‌ی تلاش دوباره |
| `InpTimerMs` | `50` | دوره‌ی polling سوکت |
| `InpVerbose` | `true` | لاگ هر درخواست در Experts |
| `InpAllowTrading` | `true` | **کلید سخت‌افزاری**: اگر false باشد هیچ سفارشی ارسال نمی‌شود |
| `InpMagic` | `20251112` | مجیک‌نامبر |
| `InpDeviation` | `30` | حداکثر اسلیپیج (point) |
| `InpComment` | `trade-mcp` | کامنت پیش‌فرض سفارش |

برای ایمنی بیشتر: روی چارت واقعی `InpAllowTrading=false` بگذار و فقط وقتی می‌خواهی
معامله کنی روشن کن. این مستقل از `TRADE_MCP_ALLOW_TRADING` در پایتون است؛ هر دو باید
روشن باشند.

## نکته‌های فنی

- در حالت `file` از `FileOpen/FileReadArray/FileWriteArray` با UTF-8 استفاده می‌کند و
  پوشه‌ها را خودش می‌سازد؛ حداکثر ۸ درخواست در هر دور برداشته می‌شود.
- در حالت `socket` از `SocketCreate/SocketConnect/SocketRead/SocketSend` استفاده می‌کند؛
  **نه** `WebRequest`. توجه: در بیلدهای جدید `SocketConnect` هم فهرست مجاز می‌خواهد.
- خواندن سوکت غیرمسدودکننده است (`timeout=0`) و در `OnTimer` انجام می‌شود؛ چارت قفل
  نمی‌شود.
- سمت پایتون همیشه باید در حال اجرا باشد؛ اگر نباشد، EA هر ۳ ثانیه دوباره تلاش می‌کند.
- اگر اکسپرت را از چارت برنداری، اتصال قطع می‌شود (این طبیعی است).
- سازگار با MT5 build 1900 و بالاتر (نسخه‌هایی که `Socket*` دارند).

## عیب‌یابی سریع

```
[trade-mcp] connect to 127.0.0.1:8787 failed, err=…
```

یعنی پایتون گوش نمی‌دهد: `trade-mcp bridge` یا `trade-mcp serve` را اجرا کن.
اگر پایتون روی ماشین دیگری است، `InpHost` را همان IP بگذار (و ترجیحاً تونل SSH).

```
[trade-mcp] error trading_disabled: AutoTrading is disabled …
```

دکمه‌ی AutoTrading را روشن کن و مطمئن شو حساب اجازه‌ی معامله‌ی الگوریتمی دارد.
