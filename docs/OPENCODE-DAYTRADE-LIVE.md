# OpenCode workstation job: read-only MT5 Day Trader verification

**New request; no live result has been produced by preparing this file.** Execute
this job on the workstation with the running MT5 terminal, not in Arena's
sandbox. Do the work yourself; do not ask the user to run individual tests.
Record measured outcomes even when a check is blocked or fails. A weekday is not
evidence that the broker's gold session is open.

## Scope and hard boundaries

- Work in `/media/joshua/SATA500/Trade/trade-mcp` on the existing branch
  `arena/f81b6b27-trade-mcp`. Check for tracked/staged changes; fetch this branch
  and fast-forward only. If dirty, diverged, a checkout conflict would occur, or
  GitHub authentication fails, stop and report the category. Never reset,
  rebase, switch branches, clean, force push, or overwrite existing user files.
  Preserve untracked files, nested repositories, secrets, settings, journals,
  guard state, archives and completed one-shot markers.
- **No order, proposal, confirmation, position change, account/position/trade-history
  inspection, symbol selection, trading-flag change, guardian/latch change,
  notification-permission prompt, model request, installation or service
  restart.** Do not run `trade-mcp doctor`; it writes to the journal and can
  display account details. Do not call `resolve_symbol`/`symbol_info` through
  the high-level client: they may select a symbol. Use the exact symbol
  `XAUUSD_i` below, if available; do not silently substitute or select one.
- The probe permits only a loopback native terminal MCP connection, checks the
  advertised schemas, does not follow redirects, and makes at most one call each
  to `terminal_get_info`, `get_time_information`, `get_chart_history`, and two
  calls to `get_marketwatch_symbols`, five seconds apart. It makes no retries.
  Chart history is at most 24 hours of broker-clock labels, `M1`, limit 1500.
  An unsuccessful call is not a reason to try a different symbol, endpoint or
  time window. The test-process `ALLOW_TRADING=false` override below does not
  change any running process or broker setting.
- Use the **existing configured native terminal endpoint and key** from the
  process environment or existing local `.env`; never echo the key, inspect
  another process's environment, paste keys into commands/chat, or change the
  endpoint or MT5 settings. If absent or unreachable, record the actual blocker.
  No credentials, raw errors, account/financial data, prices, bars, ticks,
  private paths or terminal payloads may be pasted into chat or committed.
- Never rerun or bypass the closed research jobs or their functions on the old
  archives, including `xauusd_m1_previous_direction.py`, the sparse screens and
  final-boundary replay. Reading their existing *sanitized* reports for context
  is allowed; it is not a fresh forward validation.

## 1. Isolated preflight and live probe

After the clean fast-forward, record `git rev-parse HEAD` as the **tested** full
commit. Confirm `.venv` already exists; do not install anything. Run the three
probe/report pytest files first with mock data and an isolated HOME/environment.
Use a newly created unique `logs/daytrade-preflight-<unique-id>/` directory with
mode `0700`; files must be `0600`. For example, in a shell with `umask 077`, run
pytest once with a fresh JUnit XML and redirect stdout/stderr to private files:

```sh
umask 077
mkdir -p -m 700 logs
PRE="logs/daytrade-preflight-$(.venv/bin/python -c 'import uuid; print(uuid.uuid4().hex)')"
if ! mkdir -m 700 "$PRE"; then echo 'Preflight directory unavailable'; exit 1; fi
if ! mkdir -m 700 "$PRE/home" "$PRE/tmp"; then echo 'Preflight setup failed'; exit 1; fi
probe_rc=0
env -i PATH="$PATH" HOME="$PWD/$PRE/home" TMPDIR="$PWD/$PRE/tmp" \
  PYTHONDONTWRITEBYTECODE=1 TRADE_MCP_SKIP_DOTENV=1 TRADE_MCP_BACKEND=mock \
  TRADE_MCP_ALLOW_TRADING=false TRADE_MCP_DEMO_CASH_POLICY=false \
  "$PWD/.venv/bin/python" -m pytest -q -p no:cacheprovider \
  --junitxml="$PWD/$PRE/probe.xml" \
  tests/test_daytrade_mt5_readonly_check.py tests/test_daytrade_runtime_report.py \
  tests/test_runtime_report.py >"$PRE/probe.log" 2>&1 || probe_rc=$?
printf '%s\n' "$probe_rc" > "$PRE/probe.exit"
```

Capture the actual exit status before running another command. The tests use
synthetic responses and a disposable loopback mock console; they do not contact
MT5. If they fail, skip the native probe, report the failure and publish a
blocked live check as described below. A missing dependency is `blocked`, not a
successful check; do not install it.

If preflight passes, run the following **once**, with `OUT` set to a new,
nonexistent, direct child of the ignored `logs/` directory (e.g. a UUID-based
name). The program creates this directory itself with mode `0700`, exclusively
creates files with mode `0600`, and refuses a dirty tracked revision. Do not
create `OUT` before invoking it or reuse a previous run:

```sh
umask 077
OUT="logs/daytrade-live-$(.venv/bin/python -c 'import uuid; print(uuid.uuid4().hex)')"
TRADE_MCP_BACKEND=native TRADE_MCP_ALLOW_TRADING=false \
  .venv/bin/python scripts/daytrade_mt5_readonly_check.py "$OUT" --symbol XAUUSD_i
```

Its stdout is status/count categories only; `"$OUT"/report-input.json` and all
raw native responses, advisory inputs/outputs, diagnostics and call counts are
private local evidence. The script does **not** publish or push anything. A
successful process exit means it completed logging, not that every check passed.
Its handshake and `connected` flag do not cryptographically establish that the
endpoint is the real terminal rather than a mock. Independently verify that the
configured loopback endpoint is the running MT5 terminal before publishing any
MT5-input passes. If the source cannot be verified, mark terminal and all
broker-dependent results blocked, not live; retain the raw evidence privately.
If it aborts, retain its partial evidence, do not retry, and use only results
actually verifiable from that attempt; record remaining checks blocked/not_run.

The code discovers the 11 advisory tools in the current Python process, then
runs buy/sell entries, pattern and range scans, buy/sell hypothetical exit plans,
snapshot and latched alert rules, cost-unknown sizing and limits, historical
replay and the seeded Time Machine where enough contiguous M1 data exists. It
reuses those inputs in a disposable loopback mock-console HTTP server with no
broker, journal, guardian, scheduler or model. Both paths are non-executable.
The 20 short historical chunks are arbitrary non-overlapping slices, **not 20
independent market sessions**; simulation is a smoke test, not validated odds or
net profitability. The news, SEC facts and House disclosure checks use labeled
synthetic fixtures, not real-time MT5 data. US-equity/NYSE/SEC/House/Alpaca
features are not claimed as MT5 gold ports.

| Skill | What this job can actually test |
| --- | --- |
| scanner | Two M1 scanner modes on newly returned broker bars, if sufficient and contiguous. |
| entry | Buy and sell first-closed-bar logic on those bars; no signal can still be a correct result. |
| exit | Hypothetical buy/sell stop/target calculations from the observed snapshot, no broker SL/TP. |
| Time Machine | Gross historical replay and a 20-path seeded bootstrap, when enough data exists. |
| budget | Reported symbol fields plus a hypothetical budget; unknown costs must yield no volume. |
| risk | Missing costs/limits must yield an incomplete advisory review; not a live risk-gate test. |
| broker | Order/proposal/confirmation paths are deliberately not run; no broker execution acceptance. |
| alerts | Buy/bid and sell/ask snapshot checks plus a two-call latch, without delivery or fills. |
| news | Synthetic RSS parser fixture only; no current feed or real news relevance verified. |
| analyst | Synthetic US-company XBRL fixture only, not a gold fundamental. |
| Pelosi | Synthetic US House disclosure fixture only, not a gold signal. |

Each `pass` is an assertion about that narrow function's behavior on the stated
inputs. `fail` means the check ran and its assertion failed; `blocked` means an
essential input/dependency/evidence was unavailable; `not_run` means it was not
attempted. Never promote a partial, skipped or stale check into a broader pass.

## 2. Session, timestamps and deployed UI

Inspect only this run's own private evidence. `daytrade_mt5_quote_update=pass`
means two valid returned bid/ask snapshots differed; unchanged snapshots do not
prove a closure. The script marks quote freshness and broker session `blocked`
because receipt time is not a verified tick timestamp and a terminal connection
is not a session schedule. Its automatic M1 freshness check passes only for an
explicitly offset-aware final bar, an offset-aware broker UTC clock matching the
host within 30 seconds, and a bar timestamp within 120 seconds; a naive broker
label or numeric timestamp is not assumed UTC. The script's 24-hour query can
use a last-known server clock, so successful retrieval alone proves no freshness.

If available **without changing settings**, inspect the existing terminal's
documented session/symbol information and this run's timestamps to determine
whether the broker's *actual XAUUSD_i session* is open now, including any known
maintenance/holiday break. Record `daytrade_mt5_session=pass` only with that
independent current evidence, `blocked/market_closed` if explicitly closed,
otherwise `blocked/session_unverified`. Do not infer open from Monday, a quote,
a trade-mode default, or a server receipt time. Quote freshness can be `pass`
only if the source and UTC/broker-time basis of the actual tick event timestamp
are documented, the relevant clocks agree, and its measured age is at most 30
seconds; otherwise retain `blocked/timestamp_unverified`. Save any supporting
raw evidence owner-only. Do not reinterpret old broker-wall-clock strings as UTC.

The local FastMCP discovery and isolated HTTP results do **not** prove the
running OpenCode/console loaded this revision. If an already-running OpenCode
MCP connection or console can be independently verified as this exact tested
commit, check its corresponding read-only Day Trader integration using captured
inputs, with no restart or additional native reads, and record
`daytrade_opencode_connection` / `daytrade_live_console_http` separately.
Otherwise leave them `blocked` or `not_run` with a fixed reason.

A real visible-browser monitor is separate from the synthetic DOM/HTTP tests.
Exercise it only if an already-available, safely isolated visible page can be
shown to use the tested revision and actual terminal without altering an active
page, starting an agent/cycle, selecting a symbol, prompting for notification
permission or changing settings. Bound it to at most 15 seconds, stop it, and
verify it stops on hide/error as applicable. Do not claim actual notification
delivery, broker-side SL/TP, or freshness from a UI display. If safe isolation
or revision verification is unavailable, record `daytrade_live_browser_monitor`
as blocked/not_run rather than changing a running service. This optional browser
check can cause extra read-only quote polls; it cannot submit or modify orders.

There is no forward tick-strategy test in this bounded probe. Two snapshots or
M1 OHLC do not reproduce the earlier bid/ask tick comparator. This job does not
authorize another tick-history request or a replay of the closed jobs; set
`daytrade_prior_m1_forward=blocked` with `not_exercisable`. Do not present old
retrospective results as a new test or infer profitability from them.

## 3. Offline regression and separate evidence

Using the same isolated `env -i`, mock backend, private HOME/TMPDIR and `umask`
as preflight, run **once each**:

1. `tests/test_daytrade.py` with its own private pytest log, JUnit XML and exit
   status. Record it as `daytrade_offline_tests`, with the six
   `daytrade_pytest_*` fields.
2. `tests/test_daytrade_expanded.py` with separate log, JUnit XML and exit
   status. Record `daytrade_expanded_offline_tests` and the six
   `daytrade_expanded_pytest_*` fields.
3. `.venv/bin/python scripts/test_upstream_daytrader.py` with a separate private
   log and exit status. The runner disables sockets/DNS and uses a disposable
   cwd/HOME. Record `daytrade_upstream_tests` and the six
   `daytrade_upstream_*` fields only when the actual run's total, passed,
   skipped, failures, errors and exit status are verifiable. Expected failures,
   unexpected successes, missing or ambiguous counts must not be silently
   counted as passes; use `blocked` and omit unknown counts if necessary.

JUnit XML contains private paths and failure text: keep it private. Count passed,
skipped, failed, errors, total and the actual exit code separately for each
suite. A passing suite must have exit code 0, at least one pass, zero failures
and errors, and `passed + skipped = total`; skipped is not passed. Preserve the
original `daytrade_offline_tests` meaning—do not combine it with the expanded
suite. The upstream and stock fixtures are offline, not live XAUUSD validation.
Record the three preflight files' real aggregate under
`daytrade_probe_offline_tests` / `daytrade_probe_pytest_*` using the same rules.

## 4. Publish a sanitized report and return the result

Start with the probe's `report-input.json`; do not overwrite it. Make a **new**
owner-only local input JSON with only the tested `commit`, allowed `checks`,
allowed `counts` and optional fixed `reasons` from
`scripts/publish_runtime_report.py`. Add verified offline results and any
independently established session/UI findings. Remove a prior reason if changing
a check from `blocked` to `pass`. Unknowns stay absent, hence `not_run`/null;
never invent zeros. If the probe could not run, use the actually verified tested
commit and the measured offline/blocked statuses instead; do not manufacture
partial live passes. `order` remains `not_run` and `orders_submitted` stays null.
A status `pass` is a test of the named behavior, never order authorization.

Publish using the provided publisher to a new unique filename under
`reports/runtime/`, e.g. `*-daytrade-mt5-readonly.json`. It refuses overwrites.
Review the output for enums/counts only, verify `commit` is the revision actually
tested (not the later report commit), and stage **only that exact sanitized
report filename**. Confirm the index had no pre-existing staged changes, check
the staged diff, commit on this branch and run:

```sh
git push origin arena/f81b6b27-trade-mcp
```

Do not add private inputs, logs, XML, secrets, `.env` or data. If push fails,
retain the local commit; never force or ask for credentials. For a GitHub auth
error, report that the connection needs attention through the usual UI. Return
one concise Persian report with: tested commit and report commit/push status;
actual terminal/session/quote/M1 freshness states; per-capability pass/fail/
blocked/not_run; offline pass/skip/fail/error counts; and the real browser,
previous-M1 and non-MT5/stock limitations. Do not claim an order or any live
check occurred merely because it was planned, and do not call a passing test a
profitability result.
