# Continue XAUUSD research on the saved samples, with no new MT5 requests

**Historical campaign, completed once; do not rerun or remove its marker.**
Its 25/25 reported tests use the legacy last-interior-quote horizon, now
explicitly selected in this fixed campaign. The later
[boundary comparison](../reports/research/20261122T124500Z-xauusd-final-boundary-replay.md)
corrected only the original and two-change/$1-cap variants; other variants here
have not had that correction applied. Earlier positive or open marks must not
be treated as corrected results.

The five-window legacy run showed the 2-change/$1-spread-cap variant
had a better gross end mark than the original in all five windows, but four
end marks were still negative and three entries remained open. This is not a
continuous account, net result or validated strategy.

This single offline campaign uses **only existing files**. It first tests that
same 2-change/$1 candidate on the separately archived previous sample. Its
original baseline result was already published, so it is **not a blind
holdout**; one more short window cannot validate profitability. It then tests
four fixed additional hypotheses on all six archived samples:

- 2 changes without a spread cap, to compare with the already-reported $1 cap;
- 2 changes with a $0.50 spread-cost cap;
- 2 changes with a $0.75 spread-cost cap;
- 3 changes with a $0.75 spread-cost cap.

The $0.75 midpoint and hypotheses were selected after seeing prior aggregate
results. Multiple retrospective comparisons on six sparse windows are
exploratory, not an unbiased optimization. Equal reported metrics do not prove
that hypothetical entries were identical. Do not rank a no-entry result as a
profitable strategy, interpret an open mark as a realized profit, combine
independent windows into an account equity curve or enable trading from this.

Each scenario retains 0.02 lots, next-quote assumed fills, gross -$2/+$2
exits with overshoot, one position and its own simulated three-loss latch. The
spread cost is `(ask - bid) * saved contract_size * 0.02`, checked at signal
and the next quote; it does not include additional slippage or fees. Actual
account risk, fills, historical contract specifications, full coverage, UTC,
commission, swap and net PnL remain unknown. The live strategy, real latch and
all protections remain unchanged.

## Historical execution (already completed)

The [sanitized campaign report](../reports/research/20261122T122500Z-xauusd-offline-next-tests.md)
records the one execution. The bounded job performed **25 new simulations**,
plus six original baseline replays solely to verify published metrics. No history request,
network/model call, installation, service restart, settings change or order is
made. It never modifies the v1/v2 archives or old markers. The additional
sample's returned labels are checked for overlap with v2 labels, but the old
requested bounds, UTC and shared clock basis are not independently verified.
Matching prior baseline metrics is not a cryptographic identity check.

Losses, no-entry outcomes, a failed simulation or one missing/invalid sample do
not stop independent remaining tests. Errors are recorded with fixed labels;
raw inputs and provider errors are never printed. The only output is the new
owner-only `logs/research-inputs/xauusd-offline-next-tests-v1/summary.json`.
It is exclusive and must not be overwritten. The reported execution completed
all 25 tests and reproduced six baselines. Raw tick data and the economics
snapshot must not be committed.
