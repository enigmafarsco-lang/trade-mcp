# AGENTS.md — راهنمای عامل‌های کدنویس (OpenCode …)

این ریپو یک **سرور MCP برای MetaTrader 5** است. قبل از تغییر کد این‌ها را بدان.

## اجرا و تست

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest                 # ۵۸ تست آفلاین، بدون MT5
.venv/bin/trade-mcp --mock doctor          # سلامت کامل بدون متاتریدر
.venv/bin/trade-mcp --mock serve           # اجرای MCP روی stdio
.venv/bin/trade-mcp tools                  # فهرست ابزارها
.venv/bin/trade-mcp discover               # پیدا کردن MQL5/Files (پل فایلی)
.venv/bin/trade-mcp console                # اپ محلی روی http://127.0.0.1:8790
```

## قواعد سخت (نقض نکن)

1. **هیچ‌وقت** در `stdout` لاگ ننویس. `stdout` مال پروتکل MCP روی stdio است؛ لاگ فقط
   `stderr` (از `logging`).
2. **هیچ‌وقت** مسیر معامله (`OrderSend`/`trade_open`) را بدون عبور از
   `execution.propose_* → confirm` باز نکن.
3. **هیچ‌وقت** گاردهای `risk.validate_order` را دور نزن یا شل نکن. پیش‌فرض ایمن است.
4. اعداد تحلیل را از `indicators.py`/`analysis.py` بگیر؛ در `server.py` فرمول جدید نساز.
5. تا وقتی `mcp>=1.2,<2` پین است از `mcp.server.fastmcp.FastMCP` استفاده کن
   (در mcp 2.x به `mcp.server.mcpserver.MCPServer` تغییر کرده).
6. پروتکل (`docs/PROTOCOL.md`) قرارداد بین پایتون و MQL5 است و برای **هر دو** حمل
   (فایل و سوکت) یکی است: تغییر در یکی، هم‌زمان در دیگری + به‌روزرسانی مستندات.
   هر فرمان جدید باید در `HandleLineToString` اکسپرت و در `MT5` (client.py) با هم بیاید.
7. `/home/user/...` و پورت‌های هاردکد در کد نداشته باش؛ همه از `Config` بیاید.
8. ایجنت‌ها (`agents.py`) هرگز نباید گاردهای ریسک را دور بزنند؛ سقف لات ایجنت فقط
   می‌تواند از سقف `Config` سخت‌گیرانه‌تر باشد، نه آزادتر.

## نقشه‌ی کد

| مسیر | نقش |
|---|---|
| `src/trade_mcp/server.py` | ابزارهای MCP و دستورالعمل مدل (اینجا اضافه‌کردن ابزار = فقط یک تابع `@mcp.tool()`) |
| `src/trade_mcp/analysis.py` | پروفایل تایم‌فریم، هم‌گرایی، سناریو، آمار تاریخچه |
| `src/trade_mcp/indicators.py` | اندیکاتورهای خالص پایتون (بدون numpy/pandas) |
| `src/trade_mcp/bridge/file.py` | پل فایلی (`MQL5/Files`): درخواست/پاسخ + ضربان `status.json` |
| `src/trade_mcp/agents.py` | ایجنت‌های مقیدشده + کلاینت LLM سازگار با OpenAI |
| `src/trade_mcp/console.py` | سرور HTTP محلی کنسول (بدون وابستگی اضافه) |
| `src/trade_mcp/news.py` | خواننده‌ی RSS برای تب اخبار |
| `src/trade_mcp/risk.py` | حجم، اعتبارسنجی سفارش (منبع حقیقت گاردها) |
| `src/trade_mcp/execution.py` | جریان دو مرحله‌ای و چک‌های قبل از ارسال |
| `src/trade_mcp/bridge/` | `server.py` (TCP)، `client.py` (کش + resolve نماد)، `mock.py` |
| `src/trade_mcp/mcpclient.py` | کلاینت MCP روی HTTP فقط با کتابخانه‌ی استاندارد |
| `scripts/mt5_mcp_probe.py` | دیدن ابزارهای سرور MCP داخلی MT5 |
| `mt5-connector/TradeMCPFile.mq5` | اکسپرت پل فایلی (بدون include، بدون سوکت) |
| `mt5-connector/TradeMCPConnector.mq5` | اکسپرت قدیمی TCP |

## سبک

- پایتون ۳.۱۰+، type hints، بدون وابستگی سنگین (فقط `mcp`).
- خطاها به‌صورت dict برمی‌گردند (`{"ok": False, "error": "...", "message": "..."}`)،
  هیچ ابزی نباید exception به MCP پرتاب کند (`_safe` در `server.py`).
- توضیح ابزارها (docstring) انگلیسی و کوتاه؛ پاسخ‌های کاربر فارسی.
- برای هر رفتار جدید یک تست در `tests/` (همه با بک‌اند mock).

## کارهایی که هنوز مانده

- بک‌تست روی کندل‌های تاریخی (`analysis` تقریباً آماده است؛ فقط حلقه‌ی شبیه‌سازی لازم است).
- چند ترمینال/چند حساب همزمان (نیازمند کلید `account` در پروتکل).
- هشدار ریسک روزانه (سقف ضرر روز) در `execution.py`.
