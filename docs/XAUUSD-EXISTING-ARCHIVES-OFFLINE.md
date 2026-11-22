# Execute locally: find and replay existing tick archives without MT5

No new model or AI-provider call is needed. This job uses only existing local
research archives and the existing symbol-economics snapshot. It does not call
MT5, fetch ticks, reconnect, install, restart, change settings or place orders.
Never delete the old v1 screen directory or retry its failed request.

Fetch and fast-forward `arena/f81b6b27-trade-mcp` to the commit containing this
document. Execute once from the repository root:

```bash
.venv/bin/python scripts/replay_existing_xauusd_archives.py
```

The job searches only `logs/research-inputs`, not `.env`, general journals or
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

Output is owner-only and exclusive: `logs/research-inputs/xauusd-existing-archives-inventory-v1.json`.
If it already exists, do not overwrite it. Inspect only its sanitized counts and
scenario results, not raw quotes, economics, secrets, accounts or provider data.
Report actual sample count, distinct date count and each window outcome or the
actual blocker. If there are no qualifying new samples, say so explicitly; do
not retry the provider, substitute a different broker or invent a backtest.

Commit/push a sanitized factual report only on the current branch. Check origin
after push errors. Keep trading OFF, cloud budget revoked and protection intact.
