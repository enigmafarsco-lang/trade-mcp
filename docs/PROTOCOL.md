# پروتکل سوکت (Python ⇄ EA)

- انتقال: TCP. پیش‌فرض `127.0.0.1:8787`. پایتون **سرور**، EA **کلاینت**.
- قالب: **NDJSON** — هر پیام یک شیء JSON در یک خط، پایان خط `\n`.
- کدگذاری: UTF-8.
- احراز هویت: هیچ. فقط روی loopback (یا تونل SSH) استفاده کن.

## شکل پیام‌ها

هنگام اتصال، EA یک hello می‌فرستد:

```json
{"type":"hello","ea":"TradeMCPConnector","version":"1.10","chart_symbol":"XAUUSD",
 "allow_trading":true,
 "terminal":{"build":4755,"trade_allowed":true,"company":"MetaQuotes","name":"..."},
 "account":{"login":5000123,"server":"Broker-Demo","currency":"USD"}}
```

درخواست پایتون:

```json
{"id":"9f2c…","cmd":"rates","params":{"symbol":"XAUUSD","timeframe":"M15","count":300}}
```

پاسخ موفق:

```json
{"id":"9f2c…","ok":true,"data":{"symbol":"XAUUSD","timeframe":"M15","count":300,"rates":[…]}}
```

پاسخ خطا:

```json
{"id":"9f2c…","ok":false,"error":{"code":"unknown_symbol","message":"symbol not found: XAUUSDX"}}
```

قواعد:

- `id` را پایتون می‌سازد (uuid hex). EA همان را برمی‌گرداند. هم‌زمانی مجاز است.
- `params` تخت (flat) است؛ پارسر MQL5 عمداً ساده است (بدون تو‌رفتگی).
- پیام بدون `id` (مثل `hello`/`bye`) به‌عنوان نوتیفیکیشن در نظر گرفته می‌شود.
- حداکثر طول خط سمت پایتون ۸ مگابایت؛ EA بافر خود را بالای ۸ مگابایت دور می‌ریزد.

## دستورها

### عمومی

| cmd | params | پاسخ |
|---|---|---|
| `ping` | – | `pong`, `server_time`, `epoch`, `requests`, `errors`, `ea_version` |
| `terminal_info` | – | `connected`, `trade_allowed`, `dll_allowed`, `build`, `company`, `name`, `path`, `data_path`, `ea_trading_allowed`, `ea_allow_trading_input` |
| `account_info` | – | `login`, `server`, `currency`, `leverage`, `balance`, `equity`, `margin`, `margin_free`, `margin_level`, `profit`, `credit`, `trade_allowed`, `trade_mode` |

### نماد

| cmd | params | پاسخ |
|---|---|---|
| `symbols_list` | `pattern?`, `limit?` | آرایه‌ی `{symbol, description, path, visible, selected}` |
| `symbol_info` | `symbol` | مشخصات کامل قرارداد (digits, point, tick_value, tick_size, contract, volume_*, spread*, stops_level, swap_*, bid, ask, trade_mode, filling_mode) |
| `symbol_select` | `symbol`, `enable?` | `{symbol, visible}` |
| `tick` | `symbol` | `time`, `time_str`, `bid`, `ask`, `last`, `volume`, `spread_points` |

### داده

| cmd | params | پاسخ |
|---|---|---|
| `rates` | `symbol`, `timeframe`, `count` | `{symbol, timeframe, count, rates:[{time, open, high, low, close, tick_volume, spread, real_volume}]}` |

`timeframe` ∈ `M1 M2 M3 M4 M5 M6 M10 M12 M15 M20 M30 H1 H2 H3 H4 H6 H8 H12 D1 W1 MN1`.
`time` یونیکس (ثانیه). `rates` صعودی (قدیمی → جدید). حداکثر ۵۰۰۰ کندل در هر درخواست.

### پوزیشن و تاریخچه

| cmd | params | پاسخ |
|---|---|---|
| `positions` | – | آرایه‌ی `{ticket, symbol, type, volume, price_open, sl, tp, price_current, profit, swap, magic, comment, time}` |
| `orders` | – | آرایه‌ی سفارش‌های معلق |
| `history_deals` | `date_from?`, `date_to?` | آرایه‌ی `{ticket, position_id, symbol, type, entry, volume, price, profit, swap, commission, time, comment}` |

### معامله

| cmd | params | پاسخ |
|---|---|---|
| `calc_order` | `symbol`, `side`, `volume`, `price?` | `margin`, `price`, `volume`, `free_margin_after` |
| `trade_open` | `symbol`, `side`, `volume`, `sl?`, `tp?`, `comment?`, `magic?`, `deviation?` | `retcode`, `retcode_text`, `order`, `deal`, `ticket`, `volume`, `price` |
| `trade_modify` | `ticket`, `sl?`, `tp?` | همان شکل نتیجه |
| `trade_close` | `ticket`, `volume?` (جزئی), `deviation?` | همان شکل نتیجه |
| `trade_close_all` | `symbol?`, `magic_only?` | `{found, closed, results:[…]}` |

`retcode` های موفق: `10008` (placed)، `10009` (done)، `10010` (done partial).

## کدهای خطا

| code | معنی |
|---|---|
| `unknown_command` | دستور ناشناخته |
| `unknown_symbol` | نماد پیدا/انتخاب نشد |
| `bad_timeframe` | تایم‌فریم نامعتبر |
| `rates_unavailable` | `CopyRates` شکست خورد (تاریخچه دانلود نشده) |
| `bad_volume` | حجم بیرون از min/max بروکر |
| `not_found` | تیکت پوزیشن پیدا نشد |
| `trading_disabled` | `InpAllowTrading` خاموش یا AutoTrading/حساب اجازه نمی‌دهد |
| `broker_rejected` | بروکر رد کرد (retcode در متن) |
| `calc_failed` | `OrderCalcMargin` شکست خورد |

## پیاده‌سازی مرجع

- سمت پایتون: `src/trade_mcp/bridge/server.py` (پروتکل) و `bridge/client.py` (فراخوانی تایپ‌شده).
- سمت MQL5: `mt5-connector/TradeMCPConnector.mq5` (کلاس `CJson` + توابع `Cmd*`).

اگر خواستی یک کلاینت دیگر (Node، Go، ربات دیگر) بنویسی، همین جدول کافی است.
