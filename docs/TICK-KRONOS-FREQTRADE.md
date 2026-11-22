# M1 tick momentum, Kronos candles and upstream Freqtrade

## 1. User M1 tick momentum — explicit execution path

**M1 is the signal timeframe, not a one-minute maximum holding period.**
There is no automatic close solely because 60 seconds pass or the entry candle
ends. The 300-second value below limits the entry-evaluation window, not the
lifetime of an open position; an owned position must remain protected after the
window ends. The example is historical documentation, not authorization to
start a new trading window.

`POST /api/agents/{id}`:

```json
{"action":"run_tick_window","duration_seconds":300,"consecutive_ticks":3}
```

Use 2 or 3, never another number. This is a distinct user-defined strategy, NOT
the generic guarded confidence ensemble. It requires 2/3 consecutive fresh quote
CHANGES (initial observation plus 2/3 subsequent observations). Both bid and ask
must move in the same direction and agree with the forming M1 candle's direction.
Unchanged quotes, duplicates, out-of-order timestamps, stale ticks, spread-only
moves and candle rollovers do not create entry signals. Timestamp milliseconds
are preferred; seconds-only feeds count at most once per second. The polling
path does not claim to receive every broker tick. A gap >2s resets the streak.

The bounded rules-window preflight/guardian/demo flags and single-symbol enabled
agent checks apply. No cloud is called. This strategy bypasses neither propose
nor confirm nor risk validation: its signal replaces the unrelated 62-point
ensemble score, not execution/ownership safeguards. No simultaneous positions,
unknown intents or loss-latch bypass. Do not run other entry calls concurrently.
The same window lock/stop action applies to tick and rules windows.

Volume floors to the stricter agent/config/broker cap, never up to minimum.
Broker protective SL is derived from at most min(USD2, configured percent budget).
TP is approximately USD2 price profit. Broker stop-distance, spread, margin and
risk checks can refuse entry. Quotes changing before proposal refuse that signal;
no automatic retry. The lower loss limit is persisted in the OWNED intent so a
regular guardian process still enforces it after restart. Legacy intents retain
their USD10 limit. Proposal and confirmation bind/recheck the stricter cash cap.

Cash guardian uses fee-inclusive observed P/L when available; SL/TP alone are
price-based. Poll cadence, latency, slippage, gaps and costs mean actual exits
can exceed +/-USD2. Guardian's existing 2-second cadence is unchanged; this is
not guaranteed HFT or one executed order every second. Three realized losses
retain the durable review latch. Never auto-reset it. Existing manual/old pending
proposals are preserved and are not auto-confirmed by this endpoint.

The previous local-operator instructions do not authorize another trading
window. Any future bounded DEMO execution would require explicit authorization
and fresh verification of DEMO/USD, exposure, unresolved intents, loss latch,
cost source and guardian health. Preserve protection while an owned position
exists. Report actual fills/ownership/exits separately from evaluations; source
and tests do not establish broker acceptance.

## 2. Actual Kronos adapter, not an imitation

Upstream: https://github.com/shiyu-coder/Kronos
MIT, inspected commit `67b630e67f6a18c9e9be918d9b4337c960db1e9a`.
Adapter calls the upstream `KronosPredictor.predict(df, x_timestamp, y_timestamp,
pred_len=60, ...)`. Accepts closed contiguous M1 OHLCV, returns 60 validated future
OHLCV candles. Invalid generated OHLC bounds are rejected, not repaired silently.
A final 60-bar holdout is withheld from that origin's prediction context; report
15/30/60 errors against no-change. One origin is NOT robust trading validation.
No guarantee of accuracy, nonlinear waves or profitability.

`scripts/upstream_strategy_worker.py` supports an EXPLICIT already provisioned
isolated CLOUD worker. No weight download/package install/local model setup was
performed or authorized. It requires clean pinned Kronos source and already
available tokenizer/checkpoint directories; Hugging Face offline mode is forced.
Model/checkpoint authenticity is the cloud operator's responsibility; source
pin alone does not verify checkpoint bytes. Do not run model weights on the
user's machine under this cloud-only requirement.

Cloud command template (paths refer to the CLOUD worker, not the MT5 machine):

```bash
python scripts/upstream_strategy_worker.py kronos input.json output.json \
  --kronos-repo /cloud/Kronos --tokenizer /cloud/tokenizer \
  --checkpoint /cloud/checkpoint
```

Input `{ "symbol":"XAUUSD", "rows":[[epoch,open,high,low,close,volume], ...] }`
needs >=180 closed M1 bars. Keep market-data files owner-only and out of Git.
No cloud endpoint or credentials are invented. Provisioning/cost approval for a
real cloud worker remains an external dependency, not a completed runtime test.

To display actual results locally, securely transfer the worker output and set
`TRADE_MCP_KRONOS_FORECAST_PATH` to that owner-only file. Console chart responses
include `kronos_forecast`; on M1 only, fresh symbol-matched artifacts with pinned
provenance and passing limited holdout screens draw 60 translucent/dashed FUTURE
candles, separated from historical candles, with 15/30/60 labels. No fake artifact
is generated when the worker is unavailable. Artifacts expire after 120 seconds;
no silent reuse, in-chart weight loading, cloud retry or order authorization.
The statistical trend fallback remains explicitly separate, not labeled Kronos.

## 3. Genuine upstream Freqtrade Strategy001 adapter

https://github.com/freqtrade/freqtrade-strategies
Pinned commit `f3340ce11f5bdf62f598522e64d1f5638eaa13f5`, GPL-3.0.
No GPL strategy source is vendored or relicensed as MIT. Use a separately
provisioned compatible cloud research environment with Freqtrade/pandas/TA-Lib.
The adapter verifies Strategy001.py SHA256 before executing its class and actual
populate_indicators/populate_entry_trend/populate_exit_trend methods. Other files
in the external environment are not verified by this single-file hash.

```bash
python scripts/upstream_strategy_worker.py freqtrade input.json output.json \
  --strategy-source /cloud/freqtrade-strategies/user_data/strategies/Strategy001.py
```

Requires >=200 closed contiguous 5m candles. Strategy001 is not a tick/M1 strategy,
not automatically compatible with MT5 lots/margins, and not authorized for broker
execution. Research signals are returned with provenance; no fictitious confidence
or inherited crypto ROI/stop-loss is applied to MT5. Live Freqtrade-to-MT5 execution
and profitability acceptance are NOT implemented by this research adapter.
Review GPL obligations before distributing a combined product or modified source.

## Evidence / remaining dependencies

Tests of adapter protocols use doubles/redacted data and are not actual Kronos
or Freqtrade model executions. No packages or weights were installed. Real cloud
checkpoint inference and upstream strategy execution remain unverified without
provisioned dependencies. Keep those statuses separate from tick strategy tests,
chart rendering, actual broker fills and actual guardian exits.

## Native clock / refusal diagnostics correction

The native history contract historically preserves broker wall-clock timestamps,
while quote observations use UTC receipt time. Comparing those directly can
reject every forming M1 candle. Native bars now additionally provide `time_utc`,
using the existing broker/UTC clock offset (timezone-aware timestamps are not
adjusted twice). Legacy `time` is unchanged. Tick momentum uses `time_utc` when
provided. This is a source-level mismatch; it is NOT proof that every refusal in
the archived 290-iteration run had this cause. The previous `other_guard` aggregate
lost that evidence. Individual bounded, whitelisted tick refusal codes are now
retained instead of collapsed, allowing the actual runtime blocker to be measured.

The current native quote endpoint has `time_source=received`, NOT a verified
broker tick ID/time. Signals based on these observations are labeled
`received_quote_samples`; do not call them proof of 3 consecutive broker ticks.
A true all-tick strategy requires a verified broker tick feed. Polling can miss
intermediate reversals and is not equivalent to that feed. No time or price is
invented to satisfy the entry conditions.

Local continuation: deploy after safe preflight; preserve old evidence. Verify
actual native M1 `time_utc` encloses current quote receipt time. If not, diagnose
clock source/format before trading; do not disable the candle check. One bounded
authorized tick window can then report specific refusal counts, actual broker
fills and per-intent loss policy. If quotes/candles stay stale or conditions fail,
report the measured code, not a generic claim that the strategy was tested live.
Do not automatically repeat windows, lower thresholds or manufacture trades.

Kronos/Freqtrade still require an already provisioned isolated cloud environment;
searching the local filesystem does not provision one. Worker adapters and chart
support are implemented, not actual upstream inference. No paid cloud compute,
remote account setup or local weight installation is implied by these docs.
