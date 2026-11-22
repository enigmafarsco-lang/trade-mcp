# XAUUSD final-boundary replay (executed once)

- Branch: `arena/f81b6b27-trade-mcp`
- HEAD at execution: `60c9cb1`
- Command executed (once): `.venv/bin/python scripts/xauusd_final_boundary_replay.py`
- MT5 history requests: **0**; network calls: **0**; models called: **0**; orders sent: **0**. Trading OFF, cloud budget revoked.
- Output: `logs/research-inputs/xauusd-final-boundary-replay-v1/summary.json` (owner-only, gitignored; exclusive).
- **Overall: `completed_all_comparisons`** — **12/12** comparisons completed; **0 failures**; all legacy baselines reproduced.

## What the corrected mode does
- Entry signals stay limited to interior minutes (unchanged).
- An unfilled pending entry expires at the final-minute boundary.
- All remaining observed bid/ask ticks are used **only** to mark or exit an already open hypothetical position — no new signal/entry in that minute, no forced end-of-sample close.
- Threshold overshoot and simulated three-loss latch preserved.

## Old vs corrected results (gross model USD; independent flat windows; NOT continuous)

| Sample | Variant | Legacy entries | Legacy profit/loss | Legacy gross total | Managed entries | Managed profit/loss | Managed gross total | Delta vs legacy | Legacy open at interior | Managed open at last observed | Sim latch legacy | Sim latch managed |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| additional_sample | original_3_changes | 5 | 1/4 | -6.239999999999782 | 5 | 1/4 | -6.239999999999782 | 0.0 | false | false | true | true |
| additional_sample | 2_changes_spread_cap_1_00 | 14 | 4/9 | -10.780000000002474 | 14 | 4/10 | -12.400000000002365 | -1.62 | true | false | false | true |
| w1 | original_3_changes | 4 | 1/2 | -2.2600000000002183 | 4 | 1/3 | -4.039999999999054 | -1.78 | true | false | false | false |
| w1 | 2_changes_spread_cap_1_00 | 2 | 1/0 | 1.2199999999993452 | 2 | 1/1 | -0.2000000000007276 | -1.42 | true | false | false | false |
| w2 | original_3_changes | 3 | 0/3 | -6.359999999998763 | 3 | 0/3 | -6.359999999998763 | 0.0 | false | false | true | true |
| w2 | 2_changes_spread_cap_1_00 | 2 | 0/1 | -3.0 | 2 | 0/2 | -4.1599999999998545 | -1.16 | true | false | false | false |
| w3 | original_3_changes | 3 | 0/3 | -7.080000000001746 | 3 | 0/3 | -7.080000000001746 | 0.0 | false | false | true | true |
| w3 | 2_changes_spread_cap_1_00 | 4 | 1/3 | -5.200000000002547 | 4 | 1/3 | -5.200000000002547 | 0.0 | false | false | true | true |
| w4 | original_3_changes | 6 | 1/4 | -7.140000000000327 | 6 | 1/4 | -7.260000000000218 | -0.12 | true | true | false | false |
| w4 | 2_changes_spread_cap_1_00 | 3 | 0/3 | -6.3199999999987995 | 3 | 0/3 | -6.3199999999987995 | 0.0 | false | false | true | true |
| w5 | original_3_changes | 4 | 1/3 | -4.299999999999272 | 4 | 1/3 | -4.299999999999272 | 0.0 | false | false | true | true |
| w5 | 2_changes_spread_cap_1_00 | 5 | 2/2 | -1.0199999999986176 | 5 | 2/3 | -2.099999999998545 | -1.08 | true | false | false | false |

## No-entry / failures
- No no-entry cases here (all variants had entries). All 12 comparisons completed; no failed sample stopped others.

## Limits
- Six short, separate, previously examined samples — not a continuous account or blind holdout. Final observed quote may precede requested end; complete tick coverage/UTC unverified. Open mark is not realized profit. Commission/swap/net PnL remain **NULL**. Not profitability proof. v1/v2 markers and prior archives untouched; no history request repeated.
