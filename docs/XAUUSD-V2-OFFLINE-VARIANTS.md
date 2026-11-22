# Compare hypotheses on the already-saved XAUUSD v2 ticks

This is **not** another MT5 history request. The actual five v2 windows all had
negative hypothetical gross total marks; 15 of their 18 closed hypothetical
entries were losses, and two entries remained open at their respective sample
ends. These are separate, tiny windows, not a continuous account or net PnL.

Use the already-saved five raw v2 responses and saved symbol economics to compare
four **fixed offline scenarios**, per window:

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

## Run only where the saved data already exists

From this repo root, after fast-forwarding the current branch to the commit with
this document:

```bash
.venv/bin/python scripts/compare_xauusd_v2_offline_variants.py
```

The script does not read config, contact MT5, call a model or place an order.
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
Return code 0 means all 20 window/scenario simulations completed; return code 1
means a partial/blocked run or existing output. Report all actual outcomes,
including zero-entry, open positions, per-window baseline match and failures;
commit/push only sanitized results on the current branch, never the raw archives.
