# پل فایلی (File Bridge) — اتصال بدون سوکت

این سند مسیر اتصال دوم را توضیح می‌دهد: به‌جای سوکت TCP، پایتون و اکسپرت از طریق
فایل‌های داخل `MQL5/Files` با هم حرف می‌زنند.

## چرا؟

در MT5 توابع شبکه (`SocketConnect` / `WebRequest`) به یک «فهرست مجاز» گره خورده‌اند.
در بیلدهای جدید این فهرست در `Config/settings.ini` (رمزگذاری‌شده) نگه داشته می‌شود و
`Config/common.ini` فقط بازتاب آن است؛ نتیجه اینکه ویرایش فایل بی‌اثر است و اکسپرت
همیشه `err=4014` می‌گیرد مگر از پنجره‌ی `Ctrl+O → Expert Advisors` تیک آن زده شود —
که زیر Wine گاهی دیده نمی‌شود.

فایل هیچ مجوزی نمی‌خواهد: هر اکسپرت همیشه می‌تواند در سندباکس `MQL5/Files` خودش
بنویسد و بخواند. پس این مسیر در هر نصب و هر Wine کار می‌کند.

## چه چیزی رد و بدل می‌شود

```
MQL5/Files/trade-mcp/
├── in/<id>.json       پایتون → اکسپرت   { "id": "...", "cmd": "rates", "params": {...} }
├── out/<id>.json      اکسپرت → پایتون   { "id": "...", "ok": true, "data": {...} }
└── status.json        ضربان اکسپرت      { "alive": true, "updated_at": 1794..., "symbol": "XAUUSD", ... }
```

قرارداد JSON همان قرارداد سوکت است (`docs/PROTOCOL.md`)، فقط لایه‌ی حمل عوض شده.

- اکسپرت هر `InpFilePollMs` میلی‌ثانیه (پیش‌فرض ۱۵۰) پوشه‌ی `in/` را نگاه می‌کند،
  درخواست را پردازش می‌کند، پاسخ را در `out/` می‌نویسد و درخواست را پاک می‌کند.
- هر `InpStatusMs` میلی‌ثانیه (پیش‌فرض ۱۰۰۰) `status.json` را بازنویسی می‌کند.
  پایتون اگر این فایل از `TRADE_MCP_FILE_STALE_AFTER` ثانیه (پیش‌فرض ۸) قدیمی‌تر شود،
  اتصال را قطع‌شده حساب می‌کند.
- پایتون درخواست را ابتدا به `<id>.json.tmp` می‌نویسد و بعد `rename` می‌کند تا اکسپرت
  هیچ‌وقت فایل نیمه‌کاره نخواند. پاسخ‌ها هم با همین منطق (تلاش مجدد تجزیه‌ی JSON) خوانده می‌شوند.

تأخیر واقعی: حدود **۱۵۰ تا ۵۰۰ میلی‌ثانیه** برای هر فرمان. برای M15/H1 و تحلیل
چند تایم‌فریمی کاملاً کافی است؛ برای اسکالپ تیک‌به‌تیک مناسب نیست.

## ۱) تنظیم اکسپرت (نسخه ۱.۲۰+)

در `F7` بعد از کامپایل، اکسپرت را روی چارت بگذار و در تب **Inputs**:

| ورودی | مقدار |
|---|---|
| `InpUseFiles` | `true` |
| `InpFileDir` | `trade-mcp` |
| `InpFilePollMs` | `150` |
| `InpStatusMs` | `1000` |
| `InpAllowTrading` | `false` برای فاز مشورتی |

ورودی‌های سوکت (`InpHost`, `InpPort`) در حالت `file` نادیده گرفته می‌شوند.
حتماً تیک **Allow Algo Trading** در پنجره‌ی اکسپرت و دکمه‌ی **Algo Trading** در نوار
ابزار روشن باشد، وگرنه `OnTimer` اجرا نمی‌شود و `status.json` ساخته نمی‌شود.

## ۲) تنظیم پایتون

```bash
export TRADE_MCP_BACKEND=file
export TRADE_MCP_FILES_DIR="/path/to/MetaTrader 5/MQL5/Files"
```

اگر مسیر را نمی‌دانی:

```bash
.venv/bin/trade-mcp discover
```

سپس:

```bash
.venv/bin/trade-mcp doctor --wait 30
```

انتظار:

```
  backend     : file
  files dir   : /media/.../MetaTrader 5/MQL5/Files
  [ OK ] EA connected: {'ea': 'TradeMCPConnector', 'version': '1.21', 'transport': 'file', ...}
```

مسیر پوشه در دو حالت رایج:

- Wine (همین نصب تو): `<prefix>/drive_c/Program Files/MetaTrader 5/MQL5/Files`
- ویندوز عادی (نصب در Program Files): `C:\Users\<user>\AppData\Roaming\MetaQuotes\Terminal\<id>\MQL5\Files`
  (از منوی MT5: `File → Open Data Folder`)

## عیب‌یابی

| نشانه | معنی | کار |
|---|---|---|
| `no status.json yet` | اکسپرت اجرا نشده یا `InpUseFiles` خاموش است | اکسپرت را با `InpUseFiles=true` بگذار؛ Algo Trading را روشن کن |
| `status.json is stale (Ns old)` | ترمینال بسته شده یا تایمر اکسپرت مرده | MT5 را باز کن، اکسپرت را Remove/دوباره Attach کن |
| `EA heartbeat is alive but it is not draining the inbox` | اکسپرت زنده است ولی `OnTimer` درخواست‌ها را برنمی‌دارد | احتمالاً یک نسخه‌ی قدیمی از اکسپرت روی چارت است؛ دوباره کامپایل و Attach کن |
| `files_dir_missing` | مسیر اشتباه است | با `discover` مسیر را پیدا کن |
| پاسخ‌ها خیلی کند | `InpFilePollMs` بزرگ است | روی ۱۰۰ تا ۲۰۰ بگذار |

نکته‌ی مهم: اگر MT5 را در حالت **portable** اجرا کنی، پوشه‌ی `MQL5/Files` همان پوشه‌ی
نصب است؛ در حالت عادی (نصب در Program Files) در `AppData` است. هر دو در جدول بالا هست.

## اگر MetaEditor با «fatal compiler error: unknown error» کرش کرد

این پیام خطای کد نیست؛ یعنی خودِ کامپایلر از کار افتاده. به ترتیب:

1. MetaEditor را کامل ببند (`pkill -f metaeditor64`) و `MQL5/Experts/TradeMCPConnector.ex5`
   قدیمی را پاک کن، بعد `F4` و دوباره `F7`.
2. مطمئن شو نسخه‌ی ۱.۱۰ (که قبلاً کامپایل شده بود) هم کرش می‌کند یا نه:

```bash
MT5="/path/to/MetaTrader 5"; cd /path/to/trade-mcp && \
  git show fe4407a:mt5-connector/TradeMCPConnector.mq5 > "$MT5/MQL5/Experts/TradeMCPv110.mq5"
```

اگر ۱.۱۰ هم کرش کرد ⇒ مشکل محیط است: `wineserver -k` و باز کردن دوباره‌ی MT5.
اگر ۱.۱۰ سالم کامپایل شد ⇒ ساختاری در نسخه‌ی جدید مشکل دارد؛ متن خطاها را بفرست.

نسخه‌ی ۱.۲۱ عمداً بدون `enum` سفارشی، بدون آرگومان پیش‌فرض و بدون `do-while` نوشته شد
تا کمترین سطح تماس با پارسر کامپایلر را داشته باشد.

## امنیت

- هیچ پورتی باز نمی‌شود؛ هیچ چیز از شبکه قابل دسترس نیست.
- فقط پوشه‌ی محلی `MQL5/Files/trade-mcp` نوشته/خوانده می‌شود.
- دسترسی معامله همچنان دو لایه دارد: `TRADE_MCP_ALLOW_TRADING` در پایتون و
  `InpAllowTrading` در اکسپرت.
