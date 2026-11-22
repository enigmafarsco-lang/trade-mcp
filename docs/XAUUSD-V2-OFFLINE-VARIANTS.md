# Compare hypotheses on the already-saved XAUUSD v2 ticks

**Historical comparison, completed once; do not rerun or remove its marker.**
Its published results use the legacy last-interior-quote horizon, now explicitly
selected in this fixed job. The later
[boundary comparison](../reports/research/20261122T124500Z-xauusd-final-boundary-replay.md)
corrected only the original and two-change/$1-cap variants; other variants here
have not had that correction applied.

This was **not** another MT5 history request. The actual five v2 windows all had
negative hypothetical gross total marks; 15 of their 18 closed hypothetical
entries were losses, and two entries remained open at their respective sample
ends. These are separate, tiny windows, not a continuous account or net PnL.

The job compared four **fixed offline scenarios** per window using the saved
five raw v2 responses and symbol economics:

1. Original: 3 consecutive quote changes, no spread gate.
2. Same signal with a $0.50 maximum immediate bid/ask spread cost.
3. Same signal with a $1.00 maximum immediate bid/ask spread cost.
4. 2 consecutive quote changes with a $1.00 maximum immediate spread cost.

The spread cap is a research filter, not a fee estimate: `(ask - bid) * saved
contract_size * 0.02` in the model's USD profit currency. Check the cap at both
the signal quote and the next hypothetical fill quote. The next quote is still
an assumed fill. All scenarios retain 0.02 lots, gross -$2/+$2 exits with
overshoot, at most one open position and an independent three-loss simulated
latch. The real strategy, real persistent latch, protections and trading state
are not accessed or modified.

The hypotheses were chosen **after** seeing the five original aggregate results;
this is a retrospective exploration, not a blind holdout, an unbiased optimization
or a claim of profit. A no-entry scenario is not a profitable strategy. Each
window starts flat independently; never combine outputs into a continuous equity
curve or activate a strategy from these results. Costs/net, actual fills,
account risk, historical symbol specification, tick completeness and UTC remain
unknown.

## Historical execution (already completed)

The [sanitized report](../reports/research/20261122T120000Z-xauusd-v2-offline-variants.md)
records the one execution. The script does not read config, contact MT5, call a
model or place an order.
It reads only the fixed five `w*-raw-response.json` files, the existing saved
economics snapshot and the sanitized previous result. Before comparing a
window, it checks that replay of the original reproduces the published metrics;
this is **not** a cryptographic proof that the archive is identical. It never
prints raw quotes, account data, provider text or secrets. Its output is a new
owner-only `logs/research-inputs/xauusd-v2-offline-variants-v1/summary.json`.
It never overwrites an existing output. Neither v1/v2 archive nor their markers
are changed. It does not retry MT5 or download data.

A losing result does not interrupt the next variant or window. A missing or
invalid local file, a mismatch with its old baseline, or a failed offline
variant is recorded and the remaining independent tests continue. An unsupported
input shape is not evidence of no history: do not fabricate or bypass it.
The reported execution completed all 20 window/scenario simulations. Never
commit the raw archives or the saved economics snapshot.
