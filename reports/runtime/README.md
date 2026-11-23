# Sanitized runtime evidence

Only reports produced by `scripts/publish_runtime_report.py` belong here.
Raw logs, snapshot files, accounts, prompts, provider responses, credentials and
financial values must never be committed. Reports contain status enums/counts
and optional fixed, non-sensitive reason categories only. Unknown checks are
`not_run`; unknown counts are null, never invented zero.
Reports are evidence, not permission to trade. No runtime result exists merely
because this directory exists.

## Offline Day Trader test evidence

`daytrade_offline_tests` records the result of the complete synthetic
`tests/test_daytrade.py` run, not a live broker or browser acceptance check.
Its `commit` is the source revision actually tested, even when the report
publisher itself was added or updated in a later revision. `generated_at` is
when the report was created, not necessarily when the tests ran.

For a `pass`, all six measured count fields are required:
`daytrade_pytest_passed`, `daytrade_pytest_skipped`, `daytrade_pytest_failed`,
`daytrade_pytest_errors`, `daytrade_pytest_total`, and
`daytrade_pytest_exit_code`. The exit code must be zero, at least one test must
pass, failures/errors must be zero, and total must equal passed plus skipped.
A skipped test remains skipped: do not label the skipped UI/browser check or any
live MT5 validation as passed. When evidence is incomplete, use `blocked` and
omit unknown counts rather than inventing zeros. Raw pytest output and local
paths remain private, never in this report.

## Bounded MT5-input Day Trader checks

[`docs/OPENCODE-DAYTRADE-LIVE.md`](../../docs/OPENCODE-DAYTRADE-LIVE.md)
describes the workstation-only, read-only procedure and exactly what each
field means. No report here is produced by merely preparing that procedure.
The probe records a full tested commit, a newly acquired terminal response, and
individual checks; the raw responses and diagnostics remain owner-only under
ignored `logs/`. The publisher does **not** independently verify the source of
the submitted evidence, so its relationships are consistency checks, not proof.

- `daytrade_mt5_terminal`, `symbol`, `session`, `quote`, `quote_update`,
  `quote_freshness`, `m1`, and `m1_freshness` (all prefixed `daytrade_mt5_`)
  are separate facts. A received quote or changed price is not a verified broker
  tick timestamp or trading session. M1 freshness requires its own clock/time
  basis evidence. A received bar can be old; `m1=pass` alone says nothing about
  current-market freshness.
- `daytrade_mcp_discovery` and `daytrade_live_*` cover **in-process advisory
  MCP calls** using captured terminal inputs. `live` in these names means newly
  acquired from the terminal, not independently verified fresh, executable or
  profitable. A no-signal result can be a successful functional check. Sizing
  and risk checks only test that missing hypothetical costs/limits fail closed;
  they do not verify a live account, volume, margin, cost or order guard.
- `daytrade_broker_input_http` is the isolated loopback mock-console test using
  the same captured inputs; it is not a test of an already-running console.
  `daytrade_live_console_http`, `daytrade_opencode_connection` and
  `daytrade_live_browser_monitor` are separate and may pass only when those
  actual deployed components and their tested revision are independently
  verified. A mock UI test is not a real browser or notification test.
- `daytrade_fixture_news`, `daytrade_fixture_stock_facts` and
  `daytrade_fixture_stock_disclosures` are **synthetic** source-parser checks,
  not MT5 or live news/company/disclosure tests. The US-stock-only skills are
  not an XAUUSD signal. `daytrade_prior_m1_forward` is a separate future-facing
  test; old offline research and its completed one-shot jobs do not count.
- `daytrade_expanded_offline_tests` covers only `tests/test_daytrade_expanded.py`;
  it is separate from the original `daytrade_offline_tests` field for
  `tests/test_daytrade.py`. A pass requires the six measured
  `daytrade_expanded_pytest_{passed,skipped,failed,errors,total,exit_code}`
  counts. `daytrade_probe_offline_tests` covers the probe/report synthetic tests
  and requires the six analogous `daytrade_probe_pytest_*` counts.
  `daytrade_upstream_tests` likewise requires all six
  `daytrade_upstream_{passed,skipped,failed,errors,total,exit_code}` counts for
  the bundled, socket-blocked upstream suite. A skip is never a pass.
- `daytrade_mt5_quote_samples` is the number of valid snapshots (not ticks);
  `daytrade_mt5_m1_bars` is the count of valid returned rows;
  `daytrade_mt5_segments` counts arbitrary disjoint contiguous chunks for the
  smoke test, **not verified trading sessions**; `daytrade_mt5_bootstrap_paths`
  and `daytrade_http_cases` count completed simulated paths and isolated HTTP
  cases. None is a trade or profitability count.

Optional `reasons` map a failed/blocked Day Trader check to one fixed category
from `REASONS` in the publisher; no free text is accepted. Reasons must be
removed if a subsequent independently verified outcome is `pass`. The report's
`generated_at` is the publication time, not necessarily the data observation
time. `order=not_run` and `orders_submitted=null` do not assert the state of
unrelated processes; the probe itself does not submit orders. A report is never
an authorization to trade.
