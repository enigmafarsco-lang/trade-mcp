# Correct the final-minute position-management blind spot offline

Earlier research discarded the first and last returned minutes for **entry
signals**, because those minutes may be partial and their M1 open is unknown.
However, the old cash model also discarded final-minute ticks for an **already
open position**. Its open mark and `position_open_at_sample_end` therefore
refer to the last *interior* quote, not necessarily the final observed sample
quote. An exit, a different mark, or the simulated three-loss latch may occur
in the omitted minute. This does not prove that any past result actually changes.

The new opt-in research mode keeps entry signals limited to interior minutes,
expires an unfilled pending entry at the final-minute boundary, and uses all
remaining observed bid/ask ticks **only to mark or exit an already open
hypothetical position**. No new signal or entry is made in that minute; no
forced end-of-sample close is invented. Threshold overshoot and the simulated
three-loss latch are preserved. The mode does not touch live strategy code,
orders, settings, or the real persistent latch. The old default is retained
solely so published scenarios can be reproduced and compared without silently
rewriting them.

## Execute once on the machine with the existing archives

After fetching and fast-forwarding the current branch to the commit containing
this document, run from the repository root:

```bash
.venv/bin/python scripts/xauusd_final_boundary_replay.py
```

For each of the six already-saved samples, the job reproduces published
original and candidate results before comparing the **old** and **final-minute
managed** model for two fixed strategies: original three changes, and two changes
with the $1 spread cap. It performs at most 12 corrected-model comparisons;
losses, missing inputs, baseline mismatch or a failed scenario do not stop
other independent comparisons. It makes no MT5, network, model or order calls.
Raw tick archives and the economics snapshot are read-only and never printed.

Output is the new owner-only `logs/research-inputs/xauusd-final-boundary-replay-v1/summary.json`.
It is exclusive and never overwrites prior results. Exit 0 means all 12
comparisons completed; exit 1 means a partial run, blocker or existing output.
Report the legacy and managed gross results, boundary exits and remaining open
positions for each variant/window; commit/push only sanitized results. Do not
modify v1/v2 markers, remove prior archives or repeat any history request.

These are six short, separate and previously examined samples, not a continuous
account or blind holdout. The final observed quote may still be before the
requested end, and complete tick coverage/UTC are unverified. Fills, spread
filter execution, slippage, account risk, historic symbol rules and actual
broker protection are not verified; commission, swap and net PnL remain NULL.
A positive mark or no-entry case is not evidence of a profitable strategy.
