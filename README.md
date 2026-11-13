# trade-mcp

**MetaTrader 5 ↔ OpenCode (و هر کلاینت MCP دیگر) — بدون پایتون ویندوزی و بدون دردسر Wine.**

یک سرور MCP پایتونی که روی لینوکس اجرا می‌شود، از طریق یک سوکت TCP با یک اکسپرت MQL5
داخل خود متاتریدر حرف می‌زند و ابزارهای آماده‌ی تحلیل، ریسک و اجرای سفارش را به مدل می‌دهد.

```
        Ubuntu / Linux (native)                        Wine یا Windows (هر جا که MT5 هست)
┌───────────────────────────────┐            ┌──────────────────────────────────────┐
│  OpenCode (مدل AI)            │            │  MetaTrader 5                        │
│        │  MCP (stdio)         │            │    └── TradeMCPConnector.mq5  (EA)   │
│  trade-mcp server             │  TCP/JSON  │          ↕ Market Watch / OrderSend   │
│    ├── analysis (indicator)   │◄──────────►│  broker feed (XAUUSD, ...)           │
│    ├── risk / position size   │ 8787       │                                      │
│    └── journal (SQLite)       │            │                                      │
└───────────────────────────────┘            └──────────────────────────────────────┘
```

## چرا این ساختار؟ (پاسخ به مشکل Wine)

پکیج رسمی `MetaTrader5` برای پایتون یک DLL ویندوزی را صدا می‌زند؛ برای همین مجبور می‌شوی
پایتون را هم زیر Wine ببری و بعد با نسخه‌ی Wine، مسیر فایل‌ها، پورت‌ها و کرش‌های ترمینال
بجنگی. این پروژه آن وابستگی را حذف می‌کند:

| بخش | کجا اجرا می‌شود | چرا |
|---|---|---|
| پایتون، MCP، تحلیل، ژورنال | لینوکس بومی | سریع، قابل دیباگ، قابل تست |
| دسترسی به بازار و `OrderSend` | داخل خود ترمینال MT5 (هر جا که هست) | تنها جایی که API متاتریدر وجود دارد |
| ارتباط دو طرف | **پل فایلی** (پیش‌فرض) یا سوکت TCP + JSON خط‌به‌خط | بدون DLL؛ پل فایلی هیچ مجوز شبکه‌ای نمی‌خواهد |

`WebRequest` لازم نیست. درباره‌ی سوکت یک نکته‌ی مهم: در بیلدهای جدید MT5، `SocketConnect`
هم از همان «فهرست مجاز» تغذیه می‌کند و در غیر این صورت `err=4014` می‌دهد؛ آن فهرست هم
در `Config/settings.ini` رمزگذاری‌شده زندگی می‌کند و ویرایش فایلی روی آن اثر ندارد. به همین
دلیل **پل فایلی پیش‌فرض شد**: اکسپرت و پایتون از طریق `MQL5/Files` حرف می‌زنند، بدون هیچ
پورت، مجوز یا تنظیم شبکه‌ای. جزئیات: [docs/FILE-BRIDGE.md](docs/FILE-BRIDGE.md).

## چه چیزی داخلش هست

```
trade-mcp/
├── src/trade_mcp/
│   ├── server.py        # ۱۸ ابزار MCP + ۲ پرامپت آماده
│   ├── analysis.py      # تحلیل مولتی‌تایم‌فریم + سناریوی قیمت‌خورده
│   ├── indicators.py    # EMA/RSI/ATR/ADX/Bollinger/Donchian/Swing (بدون numpy)
│   ├── risk.py          # حجم بر اساس ریسک، اعتبارسنجی سفارش، گاردها
│   ├── execution.py     # جریان دو مرحله‌ای: propose → تأیید انسان → اجرا
│   ├── store.py         # ژورنال SQLite (پیشنهادها، معاملات، یادداشت‌ها، ایجنت‌ها)
│   ├── agents.py        # ایجنت‌های مقیدشده (قید نماد/لات/ریسک/ساعت) + موتور LLM
│   ├── console.py       # اپلیکیشن محلی با تب‌ها (http://127.0.0.1:8790)
│   ├── web/index.html   # رابط کاربری همان کنسول (فارسی، RTL)
│   ├── news.py          # خواننده‌ی RSS برای تب اخبار
│   └── bridge/          # پل فایلی + سرور سوکت + کلاینت + بک‌اند mock
├── mt5-connector/
│   └── TradeMCPConnector.mq5   # اکسپرت متاتریدر
├── docs/                # نصب، OpenCode، معماری، پروتکل
├── examples/opencode.jsonc
└── tests/               # ۵۸ تست، کامل آفلاین
```

## نصب سریع (۵ دقیقه)

```bash
git clone https://github.com/enigmafarsco-lang/trade-mcp.git
cd trade-mcp
ls pyproject.toml        # باید وجود داشته باشد؛ اگر نبود: git pull origin main

python3 -m venv .venv
.venv/bin/pip install -e .

# ۱) تست بدون متاتریدر (داده‌ی ساختگی):
.venv/bin/trade-mcp --mock doctor
.venv/bin/trade-mcp --mock serve        # همین را OpenCode اجرا می‌کند

# ۲) اتصال واقعی به MT5:
cp .env.example .env                    # و مقادیر را تنظیم کن
.venv/bin/trade-mcp doctor              # منتظر اتصال EA می‌ماند
```

> اگر `pip install -e .` گفت *"does not appear to be a Python project"* یعنی کلون قدیمی
> است: `git checkout main && git pull` و دوباره نصب کن.

اگر `doctor` همه‌چیز را `[ OK ]` داد، آماده‌ای. اگر نه، بخش
[عیب‌یابی](docs/SETUP-LINUX-MT5.md#عیبیابی) را ببین.

## اکسپرت را در متاتریدر نصب کن

1. فایل `mt5-connector/TradeMCPFile.mq5` را در
   `MQL5/Experts/` ترمینال (از منوی MT5: `File → Open Data Folder`) کپی کن.
2. در MetaEditor کلید `F7` (Compile) — باید بدون خطا کامپایل شود.
3. در MT5 یک چارت باز کن و از پنجره‌ی Navigator اکسپرت را روی چارت بکش
   (یک چارت کافی است؛ همان پل همه‌ی نمادها را می‌بیند).
4. تیک **Allow Algo Trading** را بزن (در تنظیمات اکسپرت و دکمه‌ی AutoTrading در نوار ابزار).
5. در تب **Inputs** فقط `InpDir` را چک کن (`trade-mcp`); برای فاز مشورتی
   `InpAllowTrading=false` بگذار (توصیه‌ی مستندات).
6. تب **Experts** باید بنویسد: `[trade-mcp] 1.00 started | folder=MQL5\\Files\\trade-mcp`.

جزئیات و مشکلات رایج: [docs/SETUP-LINUX-MT5.md](docs/SETUP-LINUX-MT5.md).

## وصل کردن به OpenCode

```bash
# خودکار (فرمت درست نسخه‌ی نصب‌شده را خودش می‌نویسد):
opencode mcp add trade --env TRADE_MCP_ALLOW_TRADING=false \
  -- /مسیر/کامل/trade-mcp/.venv/bin/trade-mcp serve

opencode mcp list          # باید trade را connected ببینی
```

یا دستی در `opencode.json` پروژه (نمونه‌ی کامل: [examples/opencode.jsonc](examples/opencode.jsonc)):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "trade": {
      "type": "local",
      "command": ["/home/USER/trade-mcp/.venv/bin/trade-mcp", "serve"],
      "environment": {
        "TRADE_MCP_ALLOW_TRADING": "false",
        "TRADE_MCP_SYMBOL": "XAUUSD"
      }
    }
  }
}
```

> نسخه‌های جدید OpenCode نام سرورها را زیر `mcp.servers` می‌خواهند. هر دو شکل در
> [docs/OPENCODE.md](docs/OPENCODE.md) آمده است.

در OpenCode:

```
/trade:gold_session_briefing XAUUSD
```

یا مستقیم:

> «طلا را در M15 و H1 و H4 بررسی کن، سناریوی معاملاتی با حد ضرر و دو هدف بده و
> حجم را بر اساس ۰.۵٪ ریسک حساب حساب کن.»

## داشبورد محلی با تب‌های ایجنت (`trade-mcp console`)

علاوه بر OpenCode، یک اپلیکیشن محلی هم داری که همان هسته را با تب و دکمه نشان می‌دهد:

```bash
TRADE_MCP_BACKEND=file \
TRADE_MCP_FILES_DIR="/media/.../MetaTrader 5/MQL5/Files" \
.venv/bin/trade-mcp console
# مرورگر: http://127.0.0.1:8790
```

| تب | کار |
|---|---|
| نمای کلی | حساب، پوزیشن‌ها، پیشنهادهای در انتظار با دکمه‌ی تأیید |
| تحلیل | تحلیل چند تایم‌فریمی + سناریو + نمودار |
| ایجنت‌ها | ساخت ایجنت با قید (نماد، سقف لات، ریسک، ساعت، حالت) و اجرا |
| اخبار | سرخط‌های RSS |
| ژورنال | یادداشت و معاملات |
| تنظیمات | پیکربندی فعال + بلوک آماده‌ی OpenCode |

ایجنت داخلی بدون هیچ API key کار می‌کند (موتور قاعده‌محور خودِ trade-mcp) و اگر LLM
سازگار با OpenAI بدهی، تصمیم‌ها را مدل می‌گیرد:

```bash
export TRADE_MCP_LLM_BASE_URL="https://api.openai.com/v1"
export TRADE_MCP_LLM_MODEL="gpt-4o-mini"
export TRADE_MCP_LLM_API_KEY="sk-..."
```

هر ایجنت سه حالت اختیار دارد: `advisory` (فقط پیشنهاد)، `guarded` (خودکار در محدوده‌ی
اطمینان)، `auto` (خودکار کامل). در هر سه حالت `TRADE_MCP_ALLOW_TRADING` قفل نهایی است.
جزئیات: [docs/CONSOLE.md](docs/CONSOLE.md).

## ابزارهایی که مدل در اختیار دارد

| ابزار | کار |
|---|---|
| `mt5_status` | اتصال EA، ترمینال، حساب، گاردهای فعال |
| `account_info` | بالانس، اکوییتی، مارجین آزاد، اهرم |
| `find_symbol` | پیدا کردن نام دقیق نماد در بروکر (`XAUUSD.a`, `XAUUSDm`, …) |
| `symbol_spec` | مشخصات قرارداد: دیجیت، تیک‌ولیو، حجم min/max/step، اسپرد، استاپ‌لِوِل |
| `get_candles` | کندل‌های OHLC به شکل فشرده |
| `market_snapshot` | خوانش اندیکاتورها در چند تایم‌فریم |
| `analyze_market` | بسته‌ی کامل شواهد + سناریوی ورود/استاپ/دو هدف/RR/اطمینان/حجم |
| `position_size` | محاسبه‌ی حجم از ریسک و فاصله‌ی استاپ |
| `list_positions` | پوزیشن‌های باز و سفارش‌های معلق |
| `trade_history` | آمار معاملات بسته‌شده: وین‌ریت، پروفیت‌فاکتور، انتظار ریاضی |
| `risk_report` | ریسک فعلی، بدترین حالت اگر همه‌ی استاپ‌ها بخورند، تمرکز نماد |
| `propose_order` | **مرحله ۱**: ساخت و اعتبارسنجی پیشنهاد (هیچ سفارشی ارسال نمی‌شود) |
| `propose_manage_position` | **مرحله ۱**: پیشنهاد بستن/جابه‌جایی استاپ (گسترده‌کردن استاپ ممنوع) |
| `confirm_proposal` | **مرحله ۲**: ارسال به بروکر — فقط بعد از تأیید صریح انسان |
| `list_proposals` | تاریخچه‌ی پیشنهادها و سرنوشتشان |
| `cancel_proposal` | لغو پیشنهاد |
| `journal_add` / `journal_list` | یادداشت و ژورنال محلی |

## ایمنی: چرا دو مرحله‌ای؟

پیش‌فرض `TRADE_MCP_ALLOW_TRADING=false` است. در این حالت مدل می‌تواند تحلیل کند و
پیشنهاد بسازد، ولی `confirm_proposal` صریحاً رد می‌کند. حتی با روشن‌بودن مجوز:

- سقف حجم (`TRADE_MCP_MAX_LOT`)، سقف ریسک (`TRADE_MCP_MAX_RISK_PERCENT`)،
  سقف تعداد پوزیشن، سقف اسپرد، لیست نمادهای مجاز و کف/سقف حجم بروکر چک می‌شوند؛
- اگر قیمت بیش از ۱R از لحظه‌ی پیشنهاد حرکت کرده باشد، اجرا رد می‌شود؛
- پیشنهاد بعد از `TRADE_MCP_PROPOSAL_TTL` ثانیه منقضی می‌شود؛
- گسترده‌کردن حد ضرر (افزایش ریسک) مسدود است.

هیچ‌کدام از این‌ها جای مدیریت ریسک انسانی را نمی‌گیرد. اول روی **حساب دمو** تست کن.

## تست

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest        # ۵۸ تست، بدون نیاز به MT5 و اینترنت
```

## مستندات

| فایل | محتوا |
|---|---|
| [docs/SETUP-LINUX-MT5.md](docs/SETUP-LINUX-MT5.md) | نصب MT5 روی Wine/ویندوز، نصب EA، عیب‌یابی سوکت |
| [docs/OPENCODE.md](docs/OPENCODE.md) | کانفیگ OpenCode، اجازه‌ها، نمونه گفتگو |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | تصمیم‌های طراحی و لایه‌بندی |
| [docs/FILE-BRIDGE.md](docs/FILE-BRIDGE.md) | پل فایلی: چرا، چگونه، عیب‌یابی (مسیر پیش‌فرض) |
| [docs/CONSOLE.md](docs/CONSOLE.md) | کنسول محلی: تب‌ها، ایجنت‌ها، حالت‌های اختیار، API |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | پروتکل پیام‌ها (همان قرارداد برای سوکت و فایل) |

## وضعیت پروژه

- [x] لایه‌ی اتصال + ۱۸ ابزار MCP + پرامپت‌های آماده
- [x] تحلیل مولتی‌تایم‌فریم، سناریوساز، محاسبه‌ی ریسک و حجم
- [x] جریان دو مرحله‌ای سفارش با ژورنال SQLite
- [x] بک‌اند ساختگی (mock) برای توسعه بدون متاتریدر
- [x] پل فایلی (`MQL5/Files`) — اتصال بدون هیچ مجوز شبکه‌ای، مقاوم زیر Wine
- [x] کنسول محلی با تب‌ها + ایجنت‌های مقیدشده (قاعده‌محور یا LLM)
- [ ] بک‌تست روی کندل‌های تاریخی (مرحله‌ی بعد)
- [ ] پشتیبانی از چند حساب/چند ترمینال همزمان

---

<details>
<summary><b>English summary</b></summary>

`trade-mcp` is an MCP server for MetaTrader 5 that runs entirely on native Linux.
The Windows-only part (market access + `OrderSend`) stays inside an MQL5 Expert
Advisor; Python talks to it over a newline-delimited JSON socket, so there is no
Windows Python, no `MetaTrader5` pip package and no DLL mess under Wine.

* 18 MCP tools: account/symbol data, candles, multi-timeframe analysis with a
  priced scenario, position sizing, risk report, trade history, and a two-step
  order flow (`propose_order` → human confirms → `confirm_proposal`).
* Hard guard rails: trading off by default, max lot / max risk / max positions /
  max spread / symbol allow-list, 1R price-drift check, proposal expiry,
  stop-widening refused.
* `--mock` backend and 41 offline tests so you can develop without MetaTrader.
* Works with OpenCode, Claude Desktop or any MCP client over stdio.

Quick start: `python3 -m venv .venv && .venv/bin/pip install -e . && .venv/bin/trade-mcp --mock doctor`

Not financial advice. Test on a demo account first.

</details>
