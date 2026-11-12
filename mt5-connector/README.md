# TradeMCPConnector (MQL5)

اکسپرت سمت متاتریدر. کلاینت TCP است: به پایتون وصل می‌شود، JSON خط‌به‌خط می‌خواند و
دستور را روی API متاتریدر اجرا می‌کند.

## نصب

1. `TradeMCPConnector.mq5` را در `MQL5/Experts/` ترمینال کپی کن
   (`File → Open Data Folder`).
2. در MetaEditor `F7` بزن. باید بدون خطا کامپایل شود.
3. در MT5 چارت را باز کن، اکسپرت را از Navigator روی چارت بکش.
4. در دیالوگ: **Allow Algo Trading** ✔ — و دکمه‌ی **AutoTrading** نوار ابزار سبز باشد.
5. تب **Experts** باید نشان دهد: `[trade-mcp] connected to 127.0.0.1:8787`.

## ورودی‌ها

| ورودی | پیش‌فرض | توضیح |
|---|---|---|
| `InpHost` | `127.0.0.1` | آدرس پایتون |
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

- از `SocketCreate/SocketConnect/SocketRead/SocketSend` استفاده می‌کند؛ **نه**
  `WebRequest` — پس نیازی به whitelist کردن آدرس در تنظیمات ترمینال نیست.
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
