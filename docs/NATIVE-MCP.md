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

## ۴) بعدش چه می‌شود

با فهرست ابزارها، بک‌اند `native` در `trade_mcp` ساخته می‌شود که همان رابط
`Bridge.request(cmd, **params)` را با فراخوانی ابزارهای MCP پر می‌کند
(`account_info`, `symbols_list`, `rates`, `positions`, `trade_open`, …). در آن حالت
کنسول و ابزارهای MCP پروژه بدون اکسپرت کار می‌کنند و پل فایلی/سوکت فقط بک‌اپ می‌ماند.

کد کلاینت: `src/trade_mcp/mcpclient.py` (فقط کتابخانه‌ی استاندارد، بدون `asyncio`).
