# سرور MCP داخلی متاتریدر — مسیر بدون کامپایل

بیلدهای جدید MT5 خودشان سه سرور MCP دارند. این یعنی می‌شود بدون هیچ اکسپرت و بدون
هیچ `F7`، از پایتون به ترمینال وصل شد:

| سرور | آدرس | چه می‌دهد |
|---|---|---|
| **terminal** | `http://127.0.0.1:22346/mcp` | سفارش‌ها، پوزیشن‌ها، اطلاعات حساب، کنترل UI |
| **marketdata** | `https://www.metatrader.com/mcp` | قیمت، **اخبار**، تقویم اقتصادی |
| metaeditor | `http://127.0.0.1:22345/mcp` | ابزار کدنویسی و کامپایل |

هر سه با یک `Authorization: Bearer <token>` کار می‌کنند.

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
.venv/bin/python scripts/mt5_mcp_probe.py
# کلید را در همان پرامپت مخفی پیست کن؛ جای دیگری لازم نیست
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
| `HTTP 401` | کلید غلط/قدیمی | در همان تب API Key را دوباره تولید و پیست کن |
| `HTTP 406` | کلاینت `Accept` را درست نفرستاده | از همین اسکریپت استفاده کن (هر دو نوع را می‌پذیرد) |
| `session` خالی | سرور `Mcp-Session-Id` نداده | نسخه‌ی MT5 را آپدیت کن |
| جواب خالی از `curl` | `-s` خطا را پنهان می‌کند | `-i` یا `-v` بزن، یا اسکریپت بالا |

## ۴) بعدش چه می‌شود

با فهرست ابزارها، بک‌اند `native` در `trade_mcp` ساخته می‌شود که همان رابط
`Bridge.request(cmd, **params)` را با فراخوانی ابزارهای MCP پر می‌کند
(`account_info`, `symbols_list`, `rates`, `positions`, `trade_open`, …). در آن حالت
کنسول و ابزارهای MCP پروژه بدون اکسپرت کار می‌کنند و پل فایلی/سوکت فقط بک‌اپ می‌ماند.

کد کلاینت: `src/trade_mcp/mcpclient.py` (فقط کتابخانه‌ی استاندارد، بدون `asyncio`).
