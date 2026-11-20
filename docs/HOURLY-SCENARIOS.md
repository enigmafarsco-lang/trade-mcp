# One-hour scenario view

`GET /api/chart?symbol=XAUUSD&tf=M15&full=1` includes `hourly_scenarios`.
The terminal displays the 15/30/45/60-minute values in two read-only forms:
the comparison panel's scenario table and a clearly separated **dashed future
scenario overlay** on the chart (drawn only from available closed-bar data for
the current symbol/timeframe).

This is an illustrative, uncalibrated volatility scenario view — **not** a
reliable prediction, not a calibrated probability, not a buy/sell signal, and
not future candles. `execution_eligible=false` always and the overlay is never
connected to order execution. The Persian UI label states this explicitly.

The independent baseline uses the latest 30 closed-candle log returns to
estimate sample volatility, holds the last closed price as a zero-drift center,
and scales an illustrative one-volatility width by the square root of time.
There are NO calibrated coverage probabilities. This model ignores trends,
news, spread and fees; it is not a buy/sell signal. `execution_eligible=false`
always. Existing order validation/confirmation and guardian remain unchanged.

Supported inputs: M1/M5/M15/M30/H1. At least 31 closed bars are required;
forming bars are excluded, invalid OHLCV rejected and stale data suppressed.
The source remains the selected broker/backend, including mock if configured;
the scenario itself does not independently attest live source identity.
No model call, budget activation, order submission or persistent setting change
is made by this view. No DeepSeek credentials are needed or configured.

Local deployment: fast-forward the current branch, preserve environment and
all local data. Restart only the console with trading and demo cash policy OFF
if there are no positions/orders/unresolved intents; otherwise do not interrupt
the guardian. Keep cloud budget revoked and entry cycles stopped. Validate
`hourly_scenarios` with one read-only chart request and inspect the terminal's
comparison panel. Do not claim an actual order/exit test from this check.

## Horizon colors

Future horizon markers/ranges are cyan (15m), amber (30m), violet (45m) and
rose (60m). The separate future area maps elapsed scenario minutes across the
available space (not historical candle spacing); each marker is labeled.
These are volatility scenarios, not calibrated price predictions.
