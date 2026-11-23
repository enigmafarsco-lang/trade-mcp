# XAUUSD v2 offline variants comparison (executed once)

- Branch: `arena/f81b6b27-trade-mcp`
- HEAD at execution: `9d9f4db`
- Command executed (once): `.venv/bin/python scripts/compare_xauusd_v2_offline_variants.py`
- New MT5 history requests: **0**. Orders: **none**. Cloud/model calls: **none**. Trading OFF, cloud budget revoked.
- Output: `logs/research-inputs/xauusd-v2-offline-variants-v1/summary.json` (owner-only, gitignored; did not exist before, not overwritten).
- **Overall: `completed_all_simulations`** — 20/20 window/scenario simulations ran; **0 failures**; `baseline_reproduced=true` for all 5 windows. `history_calls_made=0`.

## Per-window variant outcomes (gross model USD; independent flat window, per-window simulated 3-loss latch; not continuous)

| Window | Variant | Entries | Profit exits | Loss exits | Blocked by spread | Gross total mark (model) | Open at end | Sim 3-loss latch |
|---|---|---|---:|---:|---:|---:|---:|:---:|:---:|
| w1 (11-09 13:00) | original_3_changes | 4 | 1 | 2 | 0 | -2.2600000000002183 | true | false |
| w1 | 3_changes_spread_cap_0_50 | 0 | 0 | 0 | 25 | 0 | false | false |
| w1 | 3_changes_spread_cap_1_00 | 4 | 1 | 2 | 0 | -2.2600000000002183 | true | false |
| w1 | 2_changes_spread_cap_1_00 | 2 | 1 | 0 | 0 | 1.2199999999993452 | true | false |
| w2 (11-09 19:00) | original_3_changes | 3 | 0 | 3 | 0 | -6.359999999998763 | false | true |
| w2 | 3_changes_spread_cap_0_50 | 0 | 0 | 0 | 15 | 0 | false | false |
| w2 | 3_changes_spread_cap_1_00 | 3 | 0 | 3 | 0 | -6.359999999998763 | false | true |
| w2 | 2_changes_spread_cap_1_00 | 2 | 0 | 1 | 0 | -3.0 | true | false |
| w3 (11-10 09:00) | original_3_changes | 3 | 0 | 3 | 0 | -7.080000000001746 | false | true |
| w3 | 3_changes_spread_cap_0_50 | 0 | 0 | 0 | 26 | 0 | false | false |
| w3 | 3_changes_spread_cap_1_00 | 3 | 0 | 3 | 0 | -7.080000000001746 | false | true |
| w3 | 2_changes_spread_cap_1_00 | 4 | 1 | 3 | 0 | -5.200000000002547 | false | true |
| w4 (11-10 13:00) | original_3_changes | 6 | 1 | 4 | 0 | -7.140000000000327 | true | false |
| w4 | 3_changes_spread_cap_0_50 | 0 | 0 | 0 | 37 | 0 | false | false |
| w4 | 3_changes_spread_cap_1_00 | 6 | 1 | 4 | 0 | -7.140000000000327 | true | false |
| w4 | 2_changes_spread_cap_1_00 | 3 | 0 | 3 | 0 | -6.3199999999987995 | false | true |
| w5 (11-10 19:00) | original_3_changes | 4 | 1 | 3 | 0 | -4.299999999999272 | false | true |
| w5 | 3_changes_spread_cap_0_50 | 0 | 0 | 0 | 37 | 0 | false | false |
| w5 | 3_changes_spread_cap_1_00 | 4 | 1 | 3 | 0 | -4.299999999999272 | false | true |
| w5 | 2_changes_spread_cap_1_00 | 5 | 2 | 2 | 0 | -1.0199999999986176 | true | false |

## No-entry / failure cases
- No-entry: `3_changes_spread_cap_0_50` across all windows (all signals blocked by spread: 25/15/26/37/37). No-entry is **not** a profitable strategy.
- Failures: none. All baselines reproduced (original gross totals match the previously published v2 results for each window).

## Limits
- Retrospective exploration (hypotheses chosen after original five outcomes were known); not a blind holdout, unbiased optimization or profit claim. 0.02 lots, gross -2/+2 exits with overshoot, next-quote assumed fills; spread cap is a research filter, not a fee estimate. Commission/swap/net PnL remain **NULL**. Not a continuous equity/backtest. Raw archives untouched (v1/v2 markers unchanged).
