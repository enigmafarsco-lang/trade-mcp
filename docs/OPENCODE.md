# اجرا در OpenCode

این فایل دقیقاً همان چیزی است که برای «چطور توی opencode اجرا کنم؟» لازم داری.

## ۰. پیش‌نیاز

```bash
cd /media/joshua/Linux/Trade/AI-Trading/trade-mcp     # یا هر مسیری که کلون کردی
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/trade-mcp --mock doctor        # باید OK بدهد (بدون متاتریدر)
.venv/bin/trade-mcp tools                # فهرست ۱۸ ابزار
```

نکته‌ی مهم: در کانفیگ OpenCode باید **مسیر مطلق** فایل اجرایی داخل venv را بدهی
(`/…/trade-mcp/.venv/bin/trade-mcp`)، نه `trade-mcp` خالی؛ چون OpenCode سرور را با
محیط تمیزی اجرا می‌کند و venv تو فعال نیست.

## ۱. ثبت سرور — راه ساده (CLI)

```bash
opencode mcp add trade \
  --env TRADE_MCP_ALLOW_TRADING=false \
  --env TRADE_MCP_SYMBOL=XAUUSD \
  -- /home/joshua/trade-mcp/.venv/bin/trade-mcp serve

opencode mcp list
# ✓ trade  connected
```

`opencode mcp add` خودش فرمت درست همان نسخه‌ای که نصب کرده‌ای را می‌نویسد (پروژه‌ای
یا با `--global` برای همه‌ی پروژه‌ها).

## ۲. ثبت سرور — دستی

### نسخه‌های جدید OpenCode (نام سرورها زیر `mcp.servers`)

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "trade": {
        "type": "local",
        "command": ["/home/joshua/trade-mcp/.venv/bin/trade-mcp", "serve"],
        "cwd": "/home/joshua/trade-mcp",
        "environment": {
          "TRADE_MCP_ALLOW_TRADING": "false",
          "TRADE_MCP_SYMBOL": "XAUUSD",
          "TRADE_MCP_TIMEFRAMES": "M15,H1,H4",
          "TRADE_MCP_JOURNAL": "{env:HOME}/.local/share/trade-mcp/journal.sqlite3"
        }
      }
    }
  }
}
```

### نسخه‌های قدیمی‌تر (نام سرور مستقیماً زیر `mcp`)

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "trade": {
      "type": "local",
      "command": ["/home/joshua/trade-mcp/.venv/bin/trade-mcp", "serve"],
      "environment": { "TRADE_MCP_ALLOW_TRADING": "false" }
    }
  }
}
```

اگر شک داری کدام را بزنی: `opencode mcp add …` را اجرا کن و بعد فایل
`opencode.json` را نگاه کن؛ همان شکل، شکلِ نسخه‌ی تو است.

فایل پروژه‌ای: `opencode.json` کنار پروژه. سراسری: `~/.config/opencode/opencode.json`.

## ۲.۵ دو هشدار عملی

**الف) سرور را دو بار اجرا نکن.** OpenCode خودش `trade-mcp serve` را اجرا می‌کند. اگر در
ترمینال دیگری هم `trade-mcp serve` (بک‌اند `bridge`) اجرا کنی، هر دو می‌خواهند روی
`127.0.0.1:8787` گوش بدهند و دومی با `cannot listen ... Address already in use` بالا
نمی‌آید. برای تست دستی از این‌ها استفاده کن:

```bash
.venv/bin/trade-mcp --mock doctor     # داده‌ی ساختگی، بدون پورت
.venv/bin/trade-mcp doctor --wait 30  # تست اتصال واقعی (تا وقتی سرور OpenCode بسته است)
.venv/bin/trade-mcp bridge            # فقط سوکت، برای دیدن اتصال EA (پورت می‌گیرد)
```

**ب) اول با mock شروع کن.** در ثبت سرور، `TRADE_MCP_BACKEND=mock` بگذار تا بدون
متاتریدر همه‌چیز را ببینی؛ بعد که اکسپرت وصل شد، همین متغیر را به `bridge` عوض کن (یا
حذفش کن، چون پیش‌فرض `bridge` است).

## ۳. بررسی اتصال

داخل OpenCode:

```
/mcps
```

باید `trade` را `connected` ببینی. اگر ارور داد:

```bash
# همان دستوری که OpenCode اجرا می‌کند را دستی بزن تا خطا را ببینی (stdout لاگ MCP است، stderr لاگ برنامه)
/home/joshua/trade-mcp/.venv/bin/trade-mcp serve
```

خطاهای رایج:

| نشانه | علت | راه‌حل |
|---|---|---|
| `ENOENT` / `spawn ... ENOENT` | مسیر `command` غلط یا venv جابه‌جا شده | مسیر مطلق `.venv/bin/trade-mcp` را چک کن |
| `pip install -e .` می‌گوید `pyproject.toml` نیست | کلون قدیمی یا برنچ اشتباه | `git pull` روی `main` و مطمئن شو `pyproject.toml` و `src/` را می‌بینی |
| سرور وصل می‌شود ولی ابزاری نیست | نسخه‌ی mcp ناسازگار | `.venv/bin/pip install "mcp>=1.2,<2"` |
| ابزارها زیر `trade_*` دیده نمی‌شوند | Code Mode فعال است | پایین‌تر را ببین |
| `tools: 0` | سرویس با `--mock` بالا نیامده / کانفیگ ناقص | `trade-mcp tools` و لاگ stderr |

## ۴. Code Mode و نام ابزارها

OpenCode نام ابزار را `<server>_<tool>` می‌سازد. با سرور `trade`:

```
trade_mt5_status, trade_analyze_market, trade_propose_order, trade_confirm_proposal, …
```

اگر Code Mode روشن باشد (پیش‌فرض در نسخه‌های جدید) ابزارها داخل
`tools.trade.*` گروه‌بندی می‌شوند. برای اینکه ابزارها مستقیم و ساده در دسترس مدل باشند:

```jsonc
{ "mcp": { "servers": { "trade": { "type": "local", "codemode": false, "command": ["…"] } } } }
```

## ۵. اجازه‌ها (Permissions)

پیشنهاد: ابزارهای خواندنی آزاد، ابزارهای معاملاتی «پرسیدن از من».

نسخه‌های جدید (آرایه‌ی `permissions`):

```jsonc
{
  "permissions": [
    { "action": "trade_*", "resource": "*", "effect": "allow" },
    { "action": "trade_confirm_proposal", "resource": "*", "effect": "ask" }
  ]
}
```

نسخه‌های قدیمی‌تر:

```jsonc
{
  "permission": {
    "trade_confirm_proposal": "ask",
    "trade_propose_manage_position": "ask",
    "trade_*": "allow"
  }
}
```

این لایه‌ی دوم ایمنی است؛ لایه‌ی اول خودِ سرور است (`TRADE_MCP_ALLOW_TRADING=false`).

## ۶. پرامپت‌های آماده

دو پرامپت به‌صورت کامند در دسترس‌اند:

```
/trade:gold_session_briefing XAUUSD
/trade:trade_review 30
```

اولی: تحلیل مولتی‌تایم‌فریم + سناریو + ریسک.
دومی: مرور معاملات و انضباط ریسک ۳۰ روز گذشته.

## ۷. یک جلسه‌ی نمونه

```
شما: طلا را بررسی کن. اگر سناریوی خرید داری حد ضرر و دو هدف بده.

مدل:  → trade_mt5_status                     (EA وصل است؟ حساب دمو؟)
      → trade_analyze_market XAUUSD M15,H1,H4
      → trade_risk_report
      ← «بایاس H1/H4 صعودی، ADX=27، ناحیه‌ی ورود 2658.4–2660.1،
         استاپ 2654.2، هدف۱ 2665.9، هدف۲ 2673.7، RR=1.7 و 3.4،
         حجم 0.04 لات برای ریسک ۰.۵٪ (≈۵۰ دلار)»

شما: پیش‌نویسش را بساز.

مدل:  → trade_propose_order(side="buy", stop_loss=2654.2, risk_percent=0.5)
      ← پیشنهاد #a1b2c3d4e5f6 ساخته شد، مهلت ۱۸۰ ثانیه، ۲ هشدار

شما: تأیید می‌کنم.

مدل:  → trade_confirm_proposal("a1b2c3d4e5f6")
      ← تأیید بروکر، تیکت 51234، قیمت 2659.10
```

اگر `TRADE_MCP_ALLOW_TRADING=false` باشد، مرحله‌ی آخر با
`trading_disabled` رد می‌شود — همان‌طور که باید.

## ۸. روشن‌کردن معامله در عمل

دو لایه را با هم باز کن:

```bash
# ۱) سرور جداگانه برای معامله‌ی واقعی (ترمینال دیگر)
TRADE_MCP_ALLOW_TRADING=true TRADE_MCP_MAX_LOT=0.05 \
  /home/joshua/trade-mcp/.venv/bin/trade-mcp serve

# ۲) در OpenCode یک سرور دوم با همین env ثبت کن، یا env را موقتاً عوض کن
```

پیشنهاد عملی: در OpenCode با `ALLOW_TRADING=false` کار کن (تحلیل و پیش‌نویس)، و برای
اجرا، پیشنهاد را با `list_proposals` ببین و در ترمینال معامله‌گر خودت ثبت کن. یا یک
پروفایل دوم در OpenCode بساز که فقط برای اجرا روشن است.

## ۹. اگر OpenCode نداشتی

همین سرور با Claude Desktop، Cursor یا هر کلاینت MCP دیگری کار می‌کند:

```json
{ "mcpServers": { "trade": { "command": "/…/.venv/bin/trade-mcp", "args": ["serve"] } } }
```

یا برای تست دستی با SDK:

```bash
.venv/bin/python - <<'PY'
import asyncio, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    p = StdioServerParameters(command=".venv/bin/trade-mcp", args=["--mock", "serve"])
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            out = await s.call_tool("analyze_market", {"symbol": "XAUUSD"})
            print(json.loads(out.content[0].text)["summary_fa"])

asyncio.run(main())
PY
```
