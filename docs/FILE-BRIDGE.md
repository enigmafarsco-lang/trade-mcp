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

- اکسپرت هر `InpPollMs` میلی‌ثانیه (پیش‌فرض ۱۵۰) پوشه‌ی `in/` را نگاه می‌کند،
  درخواست را پردازش می‌کند، پاسخ را در `out/` می‌نویسد و درخواست را پاک می‌کند.
- هر `InpStatusMs` میلی‌ثانیه (پیش‌فرض ۱۰۰۰) `status.json` را بازنویسی می‌کند.
  پایتون اگر این فایل از `TRADE_MCP_FILE_STALE_AFTER` ثانیه (پیش‌فرض ۸) قدیمی‌تر شود،
  اتصال را قطع‌شده حساب می‌کند.
- پایتون درخواست را ابتدا به `<id>.json.tmp` می‌نویسد و بعد `rename` می‌کند تا اکسپرت
  هیچ‌وقت فایل نیمه‌کاره نخواند. پاسخ‌ها هم با همین منطق (تلاش مجدد تجزیه‌ی JSON) خوانده می‌شوند.

تأخیر واقعی: حدود **۱۵۰ تا ۵۰۰ میلی‌ثانیه** برای هر فرمان. برای M15/H1 و تحلیل
چند تایم‌فریمی کاملاً کافی است؛ برای اسکالپ تیک‌به‌تیک مناسب نیست.

## ۱) تنظیم اکسپرت

فایل `mt5-connector/TradeMCPFile.mq5` را در `MQL5/Experts/` بگذار، `F7` بزن و اکسپرت را
روی چارت بکش. در تب **Inputs**:

| ورودی | مقدار |
|---|---|
| `InpDir` | `trade-mcp` |
| `InpPollMs` | `150` |
| `InpStatusMs` | `1000` |
| `InpAllowTrading` | `false` برای فاز مشورتی |

این اکسپرت فقط پل فایلی است (نسخه‌ی TCP جداگانه در `TradeMCPConnector.mq5` می‌ماند).
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
  [ OK ] EA connected: {'ea': 'TradeMCPFile', 'version': '1.00', 'transport': 'file', ...}
```

مسیر پوشه در دو حالت رایج:

- Wine (همین نصب تو): `<prefix>/drive_c/Program Files/MetaTrader 5/MQL5/Files`
- ویندوز عادی (نصب در Program Files): `C:\Users\<user>\AppData\Roaming\MetaQuotes\Terminal\<id>\MQL5\Files`
  (از منوی MT5: `File → Open Data Folder`)

## عیب‌یابی

| نشانه | معنی | کار |
|---|---|---|
| `no status.json yet` | اکسپرت اجرا نشده یا `InpDir` با مسیر پایتون یکی نیست | `TradeMCPFile.mq5` را Attach کن؛ `InpDir` و `TRADE_MCP_FILES_DIR` را هم‌راست کن |
| `status.json is stale (Ns old)` | ترمینال بسته شده یا تایمر اکسپرت مرده | MT5 را باز کن، اکسپرت را Remove/دوباره Attach کن |
| `EA heartbeat is alive but it is not draining the inbox` | اکسپرت زنده است ولی `OnTimer` درخواست‌ها را برنمی‌دارد | احتمالاً یک نسخه‌ی قدیمی از اکسپرت روی چارت است؛ دوباره کامپایل و Attach کن |
| `files_dir_missing` | مسیر اشتباه است | با `discover` مسیر را پیدا کن |
| پاسخ‌ها خیلی کند | `InpPollMs` بزرگ است | روی ۱۰۰ تا ۲۰۰ بگذار |

نکته‌ی مهم: اگر MT5 را در حالت **portable** اجرا کنی، پوشه‌ی `MQL5/Files` همان پوشه‌ی
نصب است؛ در حالت عادی (نصب در Program Files) در `AppData` است. هر دو در جدول بالا هست.

## اگر MetaEditor با «fatal compiler error: unknown error» کرش کرد

این پیام **خطای کد نیست**. نشانه‌هایش:

- شماره‌ی خط و ستون هر دو `0` است، هیچ خطای دیگری هم گزارش نمی‌شود.
- در مخزن رسمی MQL5 همین خطا برای بیلدهایی گزارش شده که حتی یک فایل ۶ خطی
  (`OnInit` + `OnTick`) را هم کامپایل نمی‌کردند و با بیلد بعدی/نصب دوباره درست شد
  ([forum/491672](https://www.mql5.com/en/forum/491672)،
  [forum/491723](https://www.mql5.com/en/forum/491723)).

ما هم دقیقاً همین را دیدیم: فایل ۱۰۲۶ خطی سوکت خطاهای عادی داد (پس کامپایلر کار
می‌کند) ولی هم فایل ۱۱۳۴ خطی و هم «probe» ی ۱۸۹ خطیِ بدون `#include` کرش کردند.

### تشخیص سه‌مرحله‌ای (سه کامپایل، یک ربع ساعت)

فایل‌های `mt5-connector/diag/` را در `MQL5/Experts/` کپی کن و به همین ترتیب `F7` بزن.
**بعد از هر کرش، اول MetaEditor را ببند** (`pkill -f metaeditor64`) چون یک کرش،
سشن ادیتور را مسموم می‌کند و کامپایل بعدی هم الکی کرش می‌کند.

| فایل | اگر کرش کرد یعنی |
|---|---|
| `Bare.mq5` | محیط/کامپایلر خراب است؛ هیچ فایل MQL5 روی این نصب کامپایل نمی‌شود |
| `WriteTest.mq5` | مسیر نوشتن (`StringToCharArray`/`FileWriteArray`) حساس است |
| `FindTest.mq5` | اسکن `in/` (`FileFindFirst/Next`) حساس است |

اگر `Bare.mq5` هم کرش کرد، مسیر درمان (به ترتیب):

1. `pkill -f metaeditor64` و بعد `WINEPREFIX="$PFX" wineserver -k`؛ MT5 را دوباره باز کن.
2. فضای دیسک را چک کن (`df -h "$PFX"`) — دیسک پر همین خطا را می‌دهد.
3. `Help → About` در MT5 و در MetaEditor: شماره‌ی بیلد باید یکی باشد. اگر نیست،
   یعنی نصب نیمه‌کاره آپدیت شده و MetaEditor با نسخه‌ی دیگری قاطی شده است.
4. اگر بیلد «بتا» است، نصب رسمی (stable) را از سایت بروکر/MetaQuotes بگیر.
5. در نهایت: نصب دوباره‌ی ترمینال. **اول پوشه‌ی MQL5 را بکپ کنید** چون در حالت
   portable همان‌جاست:

```bash
cp -a "$MT5/MQL5" ~/MQL5-backup-$(date +%F)
```

### اگر کامپایلر اصلاً درست نشد

بیلدی که داری (۶۳۱۶) سرور MCP داخلی خود MT5 را دارد: `Tools → Options → MCP`
(یا `Help → MCP`)، بعد `http://127.0.0.1:22346/mcp` با یک Bearer key. آن مسیر هیچ
کد MQL5 لازم ندارد و می‌تواند بک‌اند جایگزین باشد — ولی چون کلیدش دستی است و بعضی
بیلدها با کلید درست هم `401` می‌دهند، فعلاً پلان B است نه پلان A.

## امنیت

- هیچ پورتی باز نمی‌شود؛ هیچ چیز از شبکه قابل دسترس نیست.
- فقط پوشه‌ی محلی `MQL5/Files/trade-mcp` نوشته/خوانده می‌شود.
- دسترسی معامله همچنان دو لایه دارد: `TRADE_MCP_ALLOW_TRADING` در پایتون و
  `InpAllowTrading` در اکسپرت.
