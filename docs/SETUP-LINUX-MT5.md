# نصب و اتصال: Ubuntu + MetaTrader 5 (Wine یا ویندوز)

هدف: پایتون و MCP روی لینوکس بومی، و فقط خودِ ترمینال MT5 هر جا که هست.
هیچ‌جای این مسیر به «پایتون ویندوزی» یا پکیج `MetaTrader5` نیاز ندارد.

---

## مسیر A — MT5 همین حالا زیر Wine داری (حالت تو)

فقط باید اکسپرت را نصب و اجرا کنی و مطمئن شوی سوکت دو طرف به هم می‌رسند.

### ۱. فایل اکسپرت را کپی کن

در MT5: `File → Open Data Folder` → پوشه‌ی `MQL5\Experts`.
اگر دسترسی به منو نداری، معمولاً یکی از این مسیرها است:

```bash
# حالت عادی Wine
ls ~/.wine/drive_c/users/*/AppData/Roaming/MetaQuotes/Terminal/*/MQL5/Experts
# اگر MT5 را با /portable اجرا می‌کنی
ls ~/.wine/drive_c/Program\ Files/MetaTrader\ 5/MQL5/Experts
```

```bash
cp mt5-connector/TradeMCPConnector.mq5 \
   ~/.wine/drive_c/users/$USER/AppData/Roaming/MetaQuotes/Terminal/<HASH>/MQL5/Experts/
```

### ۲. کامپایل

MetaEditor را باز کن (در Wine معمولاً همراه MT5 نصب می‌شود؛ اجرای
`wine "C:\Program Files\MetaTrader 5\MetaEditor64.exe"`)، فایل را باز کن و `F7`.
باید `0 errors, 0 warnings` بدهد.

### ۳. اجرای پایتون سمت لینوکس

```bash
cd trade-mcp
.venv/bin/trade-mcp doctor --wait 60
```

`doctor` روی `127.0.0.1:8787` گوش می‌دهد و منتظر می‌ماند.

### ۴. اکسپرت را روی چارت بگذار

- یک چارت باز کن (مثلاً XAUUSD M15)، از Navigator اکسپرت را روی چارت بکش.
- در پنجره‌ی تنظیمات: **Allow Algo Trading** را تیک بزن.
- دکمه‌ی **AutoTrading** در نوار ابزار باید سبز باشد.
- ورودی‌های اکسپرت: `InpHost=127.0.0.1`، `InpPort=8787` (همان پورت پایتون).

اگر همه‌چیز درست باشد:

```
# تب Experts در MT5
[trade-mcp] 1.10 starting | target=127.0.0.1:8787 | trading=ENABLED | chart=XAUUSD PERIOD_M15
[trade-mcp] connected to 127.0.0.1:8787

# خروجی doctor
[ OK ] EA connected from 127.0.0.1:54321 -> {'ea': 'TradeMCPConnector', 'version': '1.10', ...}
[ OK ] ping: 2026.11.12 09:41:03
[ OK ] account: 5000123@YourBroker-Demo balance=10000.0 USD equity=10000.0 leverage=1:100
[ OK ] symbol: XAUUSD digits=2 tick_value=1.0 min_lot=0.01 spread=24pt
[ OK ] M15: 50 bars, last=2657.31
doctor: OK
```

---

## مسیر B — MT5 روی یک VPS ویندوزی

پایتون روی لینوکس، ترمینال روی VPS. سوکت را از طریق تونل SSH برقرار کن تا هیچ
پورتی روی اینترنت باز نشود (پروتکل ما احراز هویت ندارد؛ هرگز `0.0.0.0` روی IP عمومی
گوش نده):

```bash
# روی لینوکس: پورت 8787 محلی را روی VPS هم expose کن
ssh -N -R 8787:127.0.0.1:8787 user@your-vps
```

در EA روی VPS: `InpHost=127.0.0.1`، `InpPort=8787`. همین.

---

## مسیر C — بدون متاتریدر (توسعه و تست)

```bash
.venv/bin/trade-mcp --mock doctor
.venv/bin/trade-mcp --mock serve
```

داده‌ها ساختگی‌اند و در همه‌ی پاسخ‌ها `"mock": true` برمی‌گردد. برای ساختن و
آزمودن پرامپت‌ها و ابزارها عالی است.

---

## متغیرهای محیطی مهم

| متغیر | پیش‌فرض | کار |
|---|---|---|
| `TRADE_MCP_BACKEND` | `bridge` | `bridge` (واقعی) یا `mock` |
| `TRADE_MCP_HOST` / `PORT` | `127.0.0.1` / `8787` | آدرس و پورت گوش‌دادن پایتون |
| `TRADE_MCP_SYMBOL` | `XAUUSD` | نماد پیش‌فرض ابزارها |
| `TRADE_MCP_TIMEFRAMES` | `M15,H1,H4` | تایم‌فریم‌های پیش‌فرض تحلیل |
| `TRADE_MCP_ALLOW_TRADING` | `false` | مجوز ارسال سفارش |
| `TRADE_MCP_MAX_LOT` | `0.10` | سقف حجم هر سفارش |
| `TRADE_MCP_MAX_RISK_PERCENT` | `1.0` | سقف ریسک هر معامله |
| `TRADE_MCP_MAX_OPEN_POSITIONS` | `3` | سقف پوزیشن باز |
| `TRADE_MCP_MAX_SPREAD_POINTS` | `60` | سقف اسپرد برای ورود |
| `TRADE_MCP_ALLOWED_SYMBOLS` | `XAUUSD,GOLD,XAU` | لیست سفید نمادها |
| `TRADE_MCP_PROPOSAL_TTL` | `180` | عمر پیشنهاد (ثانیه) |
| `TRADE_MCP_JOURNAL` | `~/.local/share/trade-mcp/journal.sqlite3` | دیتابیس ژورنال |

`trade-mcp` فایل `.env` کنار پروژه را هم می‌خواند (متغیرهای محیطی بر آن مقدم‌اند).

---

## عیب‌یابی

| علامت | علت محتمل | راه‌حل |
|---|---|---|
| `connect to 127.0.0.1:8787 failed, err=5273` | سوکت‌ها در ترمینال مسدود/قدیمی | MT5 را آپدیت کن؛ در Wine `wine-stable` ۹+ استفاده کن |
| در MT5 `connected` می‌شود ولی `doctor` می‌گوید EA نیست | پورت فرق دارد | `TRADE_MCP_PORT` و `InpPort` یکی باشند |
| `no MetaTrader EA is connected` در ابزارها | اکسپرت وصل نیست یا بین دو اجرا قطع شده | چارت را عوض کن؛ Experts را ببین؛ `trade-mcp bridge` برای دیدن اتصال |
| `rates_unavailable` | نماد در Market Watch نیست / تاریخچه دانلود نشده | نماد را در Market Watch اضافه کن و یک چارت باز کن |
| `unknown_symbol` | بروکر پسوند می‌دهد (`XAUUSD.a`) | ابزار `find_symbol` را صدا بزن |
| `trading_disabled` در `confirm_proposal` | `ALLOW_TRADING=false` | عمدی است؛ برای اجرا روشن کن |
| `broker_rejected` با `retcode 10027` | AutoTrading خاموش است | دکمه‌ی AutoTrading و تیک Allow Algo Trading |
| `broker_rejected` با `retcode 10016` | استاپ/حد سود خیلی نزدیک | فاصله را بیشتر کن؛ `symbol_spec` مقدار `trade_stops_level` را می‌دهد |
| `broker_rejected` با `retcode 10019` | مارجین کافی نیست | حجم را کم کن |
| `price_moved` | حرکت بیش از ۱R از لحظه‌ی پیشنهاد | تحلیل را دوباره بگیر |

### تست دستی سوکت بدون متاتریدر

```bash
# یک شبیه‌ساز خیلی ساده‌ی EA:
python3 - <<'PY'
import socket, json
s = socket.create_connection(("127.0.0.1", 8787))
s.sendall((json.dumps({"type":"hello","ea":"fake","version":"0"})+"\n").encode())
print(s.recv(65536).decode())   # hello از سمت پایتون فقط لاگ می‌شود
PY
```

اگر `trade-mcp bridge` اتصال را نشان داد، سوکت و پورت سالم‌اند و مشکل در سمت MT5 است.

### اگر Wine دردسر داشت

سه گزینه به‌ترتیب هزینه:

1. **همین مسیر** با `wine-stable` تازه و یک prefix تمیز (`WINEPREFIX=~/.mt5 wine …`).
2. **VPS ویندوزی کوچک** فقط برای ترمینال + تونل SSH (مسیر B).
3. **بروکر با API بومی** (cTrader Open API / OANDA v20 / Dukascopy) و کنارگذاشتن MT5.
   در این حالت فقط لایه‌ی `bridge/` عوض می‌شود؛ ابزارهای MCP و تحلیل دست‌نخورده می‌مانند.

---

## نکات امنیتی

- اتصال دو طرف **بدون رمز** است. پیش‌فرض `127.0.0.1` را تغییر نده مگر با تونل SSH.
- `InpAllowTrading` در اکسپرت و `TRADE_MCP_ALLOW_TRADING` در پایتون هر دو باید روشن
  باشند تا سفارشی ارسال شود (دو کلید مستقل).
- اول با حساب **دمو** تست کن. این پروژه توصیه‌ی سرمایه‌گذاری نیست.
