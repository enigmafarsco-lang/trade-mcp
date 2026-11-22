# XAUUSD final-boundary management in offline research

## Completed comparison — do not repeat

The one-shot comparison in `scripts/xauusd_final_boundary_replay.py` was
reported completed on the workstation: **12/12** comparisons, with all legacy
baselines reproduced. Do not rerun it, remove its marker, or repeat any history
request, including on a checkout that lacks the workstation's output marker.
The owner-only output was reported under
`logs/research-inputs/xauusd-final-boundary-replay-v1/summary.json`; the
[sanitized report](../reports/research/20261122T124500Z-xauusd-final-boundary-replay.md)
contains the results.

All 12 corrected hypothetical gross end marks were negative. In particular,
the two-change / $1 spread-cap mark for w1 changed from +$1.22 to -$0.20.
Five previously open hypothetical positions reached a simulated loss exit in
the final observed minute; another remained open with a different mark. The
comparison covered **only two fixed strategies** on six previously examined,
independent short samples. Other parameter variants in earlier reports have
not been corrected by that comparison. A later [aggregate-only review](../reports/research/xauusd-saved-summary-inference.md)
infers unchanged corrected gross marks for variants already flat at the last
interior quote; it does not replay ticks or infer outcomes for open positions.

## Research default and compatibility

Signals continue to exclude the first and last returned minutes, which are
potentially partial. The old cash model also ignored the last minute for an
**already open position**: its open mark and `position_open_at_sample_end`
therefore referred to the last interior quote. That could miss a subsequent
exit or a simulated three-loss latch.

`replay_cash_scenario` now defaults to final-boundary management. It expires
unfilled pending entries at the final-minute boundary and uses subsequent
observed bid/ask quotes **only** to mark or exit a position already open. It
creates no signal or entry in that minute, invents no forced end-of-sample
close, and keeps threshold overshoot and the independent simulated loss latch.
A position still open is marked at the last observed quote, which may precede
the requested end. This is research-only: no live strategy, order, real latch,
settings or broker protection is changed.

New managed scenario outputs identify themselves as
`xauusd-next-quote-cash-scenario/v2` and include
`final_boundary_management`. To reproduce the earlier horizon with the same
inputs, call the offline function explicitly with
`manage_final_boundary=False`; it returns the legacy scenario schema
`xauusd-next-quote-cash-scenario/v1` and does not manage that minute. Before
this schema change, explicitly managed scenarios also used v1: inspect their
`final_boundary_management` field rather than inferring their mode from the
old schema string alone. The comparison report's own
`xauusd-final-boundary-replay/v1` schema is a separate format.

The completed v1/v2 sparse screens, the earlier archive inventory, v2 variant
comparison and offline-next-tests campaign are pinned to their historical
legacy horizon; the boundary comparison explicitly selects both modes. Their
published reports are not rewritten. Matching summary metrics is not a
cryptographic verification of the original raw inputs. New uses of the generic
cash replay, including `run_xauusd_cash_research.py`, get managed mode by
default; supplying already-saved `--economics` avoids that script's optional
metadata network request. Do not run a completed fixed-output job again.

These are hypothetical gross marks, not verified executable fills, realized
broker results, net PnL, account-risk validation or profitability proof.
Commission, swap and net remain unknown. Historical contract rules, slippage,
full tick coverage and UTC are unverified; six retrospective windows are not
a continuous account or blind holdout. Trading state and cloud-budget status
were reported by the operator, not independently verified by this offline work.
