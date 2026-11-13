# سرور MCP داخلی متاتریدر — مسیر بدون کامپایل

بیلدهای جدید MT5 خودشان سه سرور MCP دارند. این یعنی می‌شود بدون هیچ اکسپرت و بدون
هیچ `F7`، از پایتون به ترمینال وصل شد:

| سرور | آدرس | چه می‌دهد |
|---|---|---|
| **terminal** | `http://127.0.0.1:22346/mcp` | سفارش‌ها، پوزیشن‌ها، اطلاعات حساب، کنترل UI |
| **marketdata** | `https://www.metatrader.com/mcp` | قیمت، **اخبار**، تقویم اقتصادی |
| metaeditor | `http://127.0.0.1:22345/mcp` | ابزار کدنویسی و کامپایل |

هر سه با یک `Authorization: Bearer <token>` کار می‌کنند.

## ۰) هر سرور کلید خودش را دارد

دکمه‌ی **Copy** در تب MCP یک بلوک JSON می‌دهد که در آن **سه کلید متفاوت** هست:
یکی برای `metaeditor` (پورت ۲۲۳۴۵)، یکی برای `terminal` (پورت ۲۲۳۴۶) و یکی برای
`marketdata`. کلید ۲۲۳۴۵ روی ۲۲۳۴۶ کار نمی‌کند و فقط `401 Unauthorized` می‌دهد.

> آن بلوک را **در شل پیست نکن**؛ چون JSON است، bash هر خطش را یک دستور می‌بیند و
> کلیدها در `~/.bash_history` ذخیره می‌شوند. کلید را از پنجره کپی کن و مستقیم به
> اسکریپت بده. اگر جایی لو رفت، در همان تب دوباره تولیدش کن.

## ۱) روشن کردن سرور

در MT5: `Tools → Options → MCP`

- تیک **Enable internal server**
- **Address** را پیش‌فرض بگذار: `http://127.0.0.1:22346/mcp`
- **API Key** را از دکمه‌ی مربوطه بگیر (یا دوباره تولید کن) و کپی کن
- **OK** (بدون OK، سرور بالا نمی‌آید و پورت بسته می‌ماند)

سرور داخلی `terminal` فقط وقتی MT5 **باز** است جواب می‌دهد.

## ۲) دیدن ابزارها

اتصال MCP یک دست‌دادن چند مرحله‌ای است (`initialize` → `notifications/initialized` →
`tools/list`) و ممکن است پاسخ را به شکل SSE بفرستد؛ یک `curl` تک‌خطی معمولاً شکست
می‌خورد و با `-s` هم هیچ خطایی نشان نمی‌دهد. برای همین یک اسکریپت آماده هست:

```bash
cd /media/joshua/SATA500/Trade/trade-mcp/trade-mcp

# با کلید هر سرور به‌صورت جدا (پیشنهادی)
.venv/bin/python scripts/mt5_mcp_probe.py \
  --url terminal=http://127.0.0.1:22346/mcp \
  --token terminal='<کلید بلوک terminal>'

# یا فقط پرامپت مخفی
.venv/bin/python scripts/mt5_mcp_probe.py
```

کلید را می‌شود در محیط هم گذاشت (در `~/.bashrc` نگذار):

```bash
export MT5_MCP_TOKEN_TERMINAL='...'
export MT5_MCP_TOKEN_MARKETDATA='...'
```

یا برای هر دو سرور با هم و ذخیره‌ی کل فهرست:

```bash
.venv/bin/python scripts/mt5_mcp_probe.py \
  --url http://127.0.0.1:22346/mcp \
  --url https://www.metatrader.com/mcp \
  --dump /tmp/mt5-mcp-tools.json
```

کلید از `$MT5_MCP_TOKEN` هم خوانده می‌شود:

```bash
export MT5_MCP_TOKEN='...'
```

اسکریپت کلید را چاپ نمی‌کند، پس خروجی‌اش برای فرستادن بی‌خطر است.

برای تست یک ابزار خاص:

```bash
.venv/bin/python scripts/mt5_mcp_probe.py \
  --call get_account --args '{}'
```

## ۳) عیب‌یابی

| نشانه | معنی | کار |
|---|---|---|
| `nothing is listening on 127.0.0.1:22346` | سرور داخلی خاموش است یا MT5 بسته است | تیک Enable internal server + OK؛ `pgrep -af terminal64` |
| `HTTP 503` روی `127.0.0.1` | پورت باز است ولی جواب از خود MT5 نیست | اول `env | grep -i proxy` (پایین)؛ بعد Endpoint را در تب MCP چک کن |
| `HTTP 401` با `Server: MetaTrader5-MCP` | کلیدِ سرور دیگری را دادی | `--token terminal=...` (۲۲۳۴۶) و `--token metaeditor=...` (۲۲۳۴۵) جدا هستند |
| `HTTP 401` روی `metatrader.com` | کلید ابری جداست | برای تب اخبار لازم نیست؛ RSS کار می‌کند |
| `HTTP 406` | کلاینت `Accept` را درست نفرستاده | از همین اسکریپت استفاده کن (هر دو نوع را می‌پذیرد) |
| `session` خالی | سرور `Mcp-Session-Id` نداده | نسخه‌ی MT5 را آپدیت کن |
| جواب خالی از `curl` | `-s` خطا را پنهان می‌کند | `-i` یا `-v` بزن، یا اسکریپت بالا |

### ۵۰۳ روی آدرس لوکال = پروکسی

نکته: `no_proxy=127.0.0.0/8` جلوی این مشکل را **نمی‌گیرد**، چون پایتون در
`proxy_bypass_environment` فقط تطبیق پسوند دامنه انجام می‌دهد و CIDR را نمی‌فهمد؛ ولی
`curl` آن را می‌فهمد. از این‌جا بود که `curl --noproxy '*'` سالم بود و پایتون نه.

`urllib` (و `requests`) متغیرهای `http_proxy`/`HTTPS_PROXY` را رعایت می‌کنند، پس درخواست
`http://127.0.0.1:22346/mcp` از پروکسی رد می‌شود و پروکسی `503` برمی‌گرداند — درحالی که
`curl --noproxy '*'` سالم جواب می‌دهد.

کلاینت `trade_mcp.mcpclient` برای آدرس‌های لوکال (`127.0.0.1`, `localhost`, `::1` و
آدرس‌های خصوصی) **همیشه** پروکسی را دور می‌زند. برای اجباری کردن:

```bash
.venv/bin/python scripts/mt5_mcp_probe.py --proxy never
```

و برای چک دستی:

```bash
env | grep -i proxy
read -rsp 'key: ' K; echo
curl -i --noproxy '*' -H "Authorization: Bearer $K" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"p","version":"0"}}}' \
  http://127.0.0.1:22346/mcp | head -30
unset K
```

### سرور marketdata

`https://www.metatrader.com/mcp` با کلید محلی ترمینال `401 MCP: Unauthorized` می‌دهد؛
آن سرویس ابری است و کلیدش به حساب MQL5.com گره می‌خورد. برای تب اخبار نیازی به آن
نیست — `news.py` با RSS کار می‌کند و همین کافی است.

## ۴) بک‌اند `native` (پیاده‌سازی شده)

`src/trade_mcp/bridge/native.py` همان قرارداد `Bridge.request(cmd, **params)` را روی
ابزارهای MCP ترمینال پیاده می‌کند؛ نه اکسپرتی لازم است، نه کامپایلی، نه فایل اکسپورت‌شده:

| دستور داخلی | ابزار MCP |
|---|---|
| `ping` | `get_time_information` |
| `terminal_info` | `terminal_get_info` |
| `account_info` | `get_trading_account_info` |
| `symbols_list` / `symbol_info` | `get_marketwatch_symbols` |
| `symbol_select` | `add_marketwatch_symbol` / `remove_marketwatch_symbol` |
| `tick` | `get_marketwatch_symbols` (quote همان نماد) |
| `rates` | `get_chart_history` |
| `positions` / `orders` | `get_trading_open_positions` |
| `history_deals` | `get_trading_history_orders` |
| `calc_order` | `trade_order_calc_margin` |
| `trade_open` | `trade_send_market_order` |
| `trade_modify` | `trade_modify_sl_tp` |
| `trade_close` | `trade_close_single_position` |

نکته‌ها:

* تیکت‌ها در MCP رشته‌ی دهدهی‌اند و در قرارداد ما عدد؛ تبدیل در همان فایل انجام می‌شود.
* `trade_close_all` پشتیبانی نمی‌شود (ابزارش وجود ندارد) و `trade_close` فقط
  بستن کامل است — اگر حجم جزئی بدهید صریحاً `partial_close_unsupported` برمی‌گردد،
  نه اینکه بی‌صدا کل پوزیشن بسته شود.
* `magic` و `deviation` در ابزارهای MCP وجود ندارند، پس نادیده گرفته می‌شوند؛
  کامنت (`trade-mcp`) پاس داده می‌شود.
* شکل پاسخ ابزارها مستند نیست، بنابراین خواندن فیلدها با جست‌وجوی بازگشتی و
  بی‌توجه به بزرگی/کوچکی حروف است (`native.find`). برای کالیبره‌کردن:

```bash
read -rsp 'terminal key: ' TK; echo
.venv/bin/python scripts/mt5_mcp_snapshot.py --token "terminal=$TK" --symbol XAUUSD --json /tmp/mt5-snapshot.json
unset TK
```

این اسکریپت فقط ابزارهای خواندنی را صدا می‌زند (هیچ سفارشی نمی‌فرستد) و کلیدها و
مقادیر هر پاسخ را چاپ می‌کند؛ اگر فیلدی در جدول بالا جا افتاده باشد، از همین
خروجی می‌شود دید و در `native.py` اصلاح کرد.

## ۵) اجرا

```bash
# کلید را فقط یک بار در همین شل بگذارید (نه در تاریخچه، نه در چت)
read -rsp 'terminal key: ' TK; echo
export TRADE_MCP_MCP_TOKEN="$TK"
unset TK

# ۱) سلامت: هندشیک، حساب، نماد، کندل، پوزیشن
TRADE_MCP_BACKEND=native TRADE_MCP_MCP_URL=http://127.0.0.1:22346/mcp \
.venv/bin/trade-mcp doctor --wait 0

# ۲) کنسول محلی
TRADE_MCP_BACKEND=native .venv/bin/trade-mcp console
# http://127.0.0.1:8790
```

`doctor` همیشه کلید را ماسک می‌کند و فقط «set / MISSING» چاپ می‌کند.

اگر خواستید کلید را در فایل بگذارید، `.env` (که در `.gitignore` هست) تنها جای درست است:

```bash
printf 'TRADE_MCP_BACKEND=native\nTRADE_MCP_MCP_TOKEN=%s\n' "$TK" >> .env
```

**کلیدهایی که در ترمینال پیست شده‌اند باید عوض شوند:** MT5 → Tools → Options → MCP →
برای هر سه سرور `Reset`/تولید کلید جدید، چون کلیدهای قبلی در `~/.bash_history` ماندند:

```bash
history -c && history -w
```

کد کلاینت: `src/trade_mcp/mcpclient.py` (فقط کتابخانه‌ی استاندارد، بدون `asyncio`).
