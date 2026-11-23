# Historical inventory: replay existing tick archives without MT5

**Completed once; do not rerun or remove its output marker.** The fixed
inventory explicitly retains the published legacy last-interior-quote horizon;
new generic cash research defaults to final-boundary management. See the
[boundary comparison](XAUUSD-FINAL-BOUNDARY-REPLAY.md) for the corrected
retrospective results and their limitations.

No new model or AI-provider call is needed. This job uses only existing local
research archives and the existing symbol-economics snapshot. It does not call
MT5, fetch ticks, reconnect, install, restart, change settings or place orders.
Never delete the old v1 screen directory or retry its failed request.

The [sanitized report](../reports/research/20261122T102500Z-xauusd-existing-archives-offline.md)
records the completed execution. The job searches only `logs/research-inputs`,
not `.env`, general journals or
other personal folders. It only considers `raw-response.json` and
`*-raw-response.json`, never follows symlinks, limits directory entries and file
size/count, requires an explicit successful XAUUSD_i tick payload, validates
actual two-sided quotes and strictly increasing naive timestamps, refuses data
longer than 5 minutes or containing more than 10,000 ticks per archive, and
excludes duplicates and overlapping time ranges. It never reads cached MT5
binary formats and does not alter any inputs. No UTC or coverage claim is made.

It automatically replays all qualifying non-overlapping archived samples using
the existing fixed 0.02-lot next-quote gross cash model and saved economics.
Each is a separate, initially flat hypothetical with its own simulated latch;
real persistent latch and account state are never touched. This is exploratory,
not a pre-registered continuous multi-day backtest; do not combine these windows
into an equity curve. Missing costs/net remain null; no profitability claim.

The reported output is owner-only and exclusive:
`logs/research-inputs/xauusd-existing-archives-inventory-v1.json`. Do not
overwrite it or inspect raw quotes, economics, secrets, accounts or provider
data. Do not retry the provider, substitute a different broker or invent a
backtest.
