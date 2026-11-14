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

## Integrated web agents using the OpenCode model server

There are two distinct connections:

- OpenCode -> trade-mcp MCP tools: coding/operating the application, including
  `list_web_agents`, `create_web_agent`, and `run_web_agent`.
- trade-mcp web agents -> OpenCode HTTP server: bounded model decisions, with all
  tools denied in a fresh v1 session or through stateless v2 text generation. The local execution engine alone validates and
  creates proposals. Every OpenCode proposal requires explicit human confirmation,
  even if an existing agent was previously set to guarded/auto mode.

### One launcher for MT5, OpenCode server and the web console

Prerequisites: working OpenCode CLI with provider authentication, the project venv,
and a running native MT5 MCP server. No second model-provider key is required.

```bash
.venv/bin/python scripts/mt5_mcp_connect.py --console --enable-demo-trading --opencode --opencode-model opencode/big-pickle
```

The model ID above is an example matching the reported OpenCode setup; it must be
available to your installed OpenCode account. Omit `--opencode-model` to use the
server's default model, which is not necessarily the model selected in another
interactive OpenCode session.

Start the launcher first, then click MT5 MCP Copy at its prompt, return and press
Enter only. Never paste the copied configuration into a shell. The launcher checks
MT5 and the demo account, reuses a healthy OpenCode server on loopback port 4096 or
starts `opencode serve` there, then starts the console. Terminal credentials are
not passed to the OpenCode child process. An owned OpenCode process is stopped when
the console exits; a pre-existing server is left alone. Provider credentials stay
in OpenCode. Startup health only tests the HTTP server, not model access.

Open `http://127.0.0.1:8790`, select Settings, and click the model-response test.
This sends one generic request and may incur provider usage charges. Then create
or run an agent in the Agents tab. Reports show the backend, actual model usage,
decisions and errors. Pending proposals are reviewed/confirmed in the existing
proposal UI. No trade is sent by creating/running an OpenCode-backed agent.
Insufficient sizing budget can still block a proposal; integration does not raise
risk limits or promise a profitable trade.

### API compatibility and security boundary

This adapter auto-detects OpenCode v1 and v2. The **v1 server API** was checked
against upstream v1.2.15 source: `GET /global/health`, `POST /session`,
`POST /session/{id}/message`, `POST /session/{id}/abort`, `DELETE /session/{id}`.
V1 requires an echoed session permission rule denying `*` for pattern `*` before
sending market context. **V2 support is checked against the v2.2.7 tagged source
and OpenAPI schema**: `GET /api/info` detects the version, then
`POST /api/experimental/generate/text` sends `{prompt, model?: {providerID, id}}`
and reads `data.text`. This is stateless model generation, not an agent session:
no shell, MCP or repository tools are supplied and no session is created.
The generation endpoint is experimental and may change in future releases.

V2 requires server authentication and otherwise generates an unknown password.
When starting an owned server, the launcher generates an ephemeral password and
passes it to the server and console through their environments only. It is not a
model-provider key or MT5 key and is never printed or written to a config file.
For an existing protected server, supply its password securely via
`OPENCODE_PASSWORD` (preferred) or `OPENCODE_SERVER_PASSWORD` before launching.
A server on port 4096 with unknown credentials is not replaced or killed.

Unknown APIs, unavailable models, invalid decisions and connection failures
fail closed: no deterministic decision is substituted for OpenCode.

Only market summaries and whitelisted account metrics are provided, not account
IDs or terminal keys. The summary can reach the model provider configured in
OpenCode. Cleanup is attempted after each request, but provider/server retention
policies still apply. The model has no shell, file, MCP or execution tools in these
analysis requests. Do not weaken the deny-all rule to fix a connection error.
No live provider or native MT5 integration is exercised by the offline tests.

For an already managed server, set `TRADE_MCP_OPENCODE_URL` in the console process
instead of using the launcher flag. Optional `TRADE_MCP_OPENCODE_MODEL` uses
`provider/model` format. Optional `OPENCODE_PASSWORD` (legacy alias `OPENCODE_SERVER_PASSWORD`) uses the default
OpenCode username `opencode`. The URL must be loopback HTTP without embedded
credentials. OpenCode takes precedence over the direct OpenAI-compatible adapter.

The external MCP tools share agents/reports with the console only when both use
the same journal path. `create_web_agent` creates a disabled advisory agent;
`run_web_agent` requires OpenCode configuration in that MCP process and runs once
without enabling the scheduler or confirming anything. The web console's process
environment is not automatically inherited by an independently launched MCP
process. Use the console for running agents if that MCP process is not configured.

### Attach to the running desktop app, without starting a CLI server

If the desktop application already owns an authenticated server, use its actual
loopback URL instead of `--opencode`. Example using an observed desktop port:

```bash
.venv/bin/python scripts/mt5_mcp_connect.py --console --opencode-url http://127.0.0.1:49374 --opencode-model opencode/big-pickle --opencode-password-prompt
```

This prompts first for the **OpenCode server password**, hidden and process-only,
then asks you to click MT5 Copy and press Enter only. The server password is not a
provider API key and not an MT5 key. Do not guess it or send it in chat. Desktop
ports may change after restarting the app. This command keeps trading disabled,
uses native MT5 data after the clipboard check, and opens the web console.

An explicit `--opencode-url` (or inherited `TRADE_MCP_OPENCODE_URL`) means
**attach-only**: on any connection/authentication failure, no alternate OpenCode
process is spawned, no desktop process is stopped, and startup fails closed.
The application server and console remain distinct processes; neither inherits
configuration changes made to the other after launch.

`TRADE_MCP_OPENCODE_PASSWORD` is accepted by Config and has precedence over the
standard `OPENCODE_PASSWORD` and legacy `OPENCODE_SERVER_PASSWORD` aliases.
Configured password values are never included in the settings response.
For successful model decisions reports use `source: opencode` and
`llm_status: used`, not `llm_status: opencode`.

### Automatic private desktop discovery (recommended for OpenCode 2.2.7)

You do not need to view or copy the server password. Keep the desktop app open:

```bash
.venv/bin/python scripts/mt5_mcp_connect.py --console --opencode-desktop --opencode-model opencode/big-pickle
```

This reads only the standard private service registration at
`$XDG_STATE_HOME/opencode/service.json` (default
`~/.local/state/opencode/service.json`), following OpenCode's v2 discovery contract.
It requires a regular, non-symlink, current-user-owned file with no group/other
permissions. It authenticates to the advertised loopback server and compares the
server PID with the registration before accepting it. Passwords remain in process
memory/environment; the source file is not modified and its contents are never
printed. No process environments, shell histories or credential directories are
searched. No alternate server is started on failure. Custom release-channel
registration filenames are not automatically searched.

The desktop is checked before MT5 clipboard setup, avoiding repeated MT5 steps
when desktop discovery fails. Trading stays disabled unless explicitly enabled by
the separate demo-trading flag. Do not cat, paste or upload the registration file.

### Run the desktop-discovered native MCP server without a shell wrapper

Configure OpenCode's local `trade` MCP command as an argv array consisting of the
absolute venv Python path, absolute `scripts/mt5_mcp_connect.py` path, and:

```text
--serve --opencode-desktop --opencode-model opencode/big-pickle
```

No password or terminal key belongs in the OpenCode config. This mode reads the
private desktop registration, then the MT5 Copy block from the clipboard, and
launches `trade-mcp serve` with backend=native and trading=false, overriding stale
mock/trading settings. It never prompts on protocol stdin or writes diagnostic
text to protocol stdout. Failures produce a generic stderr message and no fallback.
It does not run doctor; call `mt5_status` after the handshake to verify native auth.

After saving configuration, click MT5 MCP Copy immediately before reconnecting the
`trade` MCP server in the desktop app. Do not copy shell commands afterward, since
that replaces the clipboard. This is required on each MCP restart; no terminal key
is persisted. The console remains a separate process using the same journal path.
An already running MCP process must be restarted to load this launcher/configuration.
