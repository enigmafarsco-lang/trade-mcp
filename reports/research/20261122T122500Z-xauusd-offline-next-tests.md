# XAUUSD offline next-tests campaign (executed once)

- Branch: `arena/f81b6b27-trade-mcp`
- HEAD at execution: `d7eb696`
- Command executed (once): `.venv/bin/python scripts/xauusd_offline_next_tests.py`
- MT5 history requests: **0**; network calls: **0**; models called: **0**; orders sent: **0**. Trading OFF, cloud budget revoked.
- Output: `logs/research-inputs/xauusd-offline-next-tests-v1/summary.json` (owner-only, gitignored; exclusive, not overwritten).
- **Overall: `completed_all_new_tests`** — 25/25 new tests; 6/6 baseline checks reproduced (`all_baselines_reproduced=true`); 0 failures. Losses/no-entry/failed tests did not stop independent tests.

## additional_sample (prior archived 1015-row sample; baseline was known, not a blind holdout)
| Variant | Entries | Profit exits | Loss exits | Gross total mark (model) |
|---|---:|---:|---:|---:|
| 2_changes_spread_cap_1_00 | 14 | 4 | 9 | -10.780000000002474 |
| 2_changes_no_spread_cap | 14 | 4 | 9 | -10.780000000002474 |
| 2_changes_spread_cap_0_50 | 0 | 0 | 0 | 0 |
| 2_changes_spread_cap_0_75 | 14 | 4 | 9 | -10.780000000002474 |
| 3_changes_spread_cap_0_75 | 5 | 1 | 4 | -6.239999999999782 |

## v2 windows (4 new variants each; gross mark, delta vs original and vs previous 2-changes/$1-cap)
| Window | Variant | Entries | Profit | Loss | Gross total | vs orig | vs prev 2c/$1 |
|---|---|---:|---:|---:|---:|---:|---:|
| w1 | 2_changes_no_spread_cap | 2 | 1 | 0 | 1.2199999999993452 | +3.4799999999995634 | 0.0 |
| w1 | 2_changes_spread_cap_0_50 | 0 | 0 | 0 | 0 | +2.2600000000002183 | -1.2199999999993452 |
| w1 | 2_changes_spread_cap_0_75 | 2 | 1 | 0 | 1.2199999999993452 | +3.4799999999995634 | 0.0 |
| w1 | 3_changes_spread_cap_0_75 | 4 | 1 | 2 | -2.2600000000002183 | 0.0 | -3.4799999999995634 |
| w2 | 2_changes_no_spread_cap | 2 | 0 | 1 | -3.0 | +3.359999999998763 | 0.0 |
| w2 | 2_changes_spread_cap_0_50 | 0 | 0 | 0 | 0 | +6.359999999998763 | 3.0 |
| w2 | 2_changes_spread_cap_0_75 | 3 | 0 | 2 | -5.519999999998618 | +0.8400000000001455 | -2.5199999999986176 |
| w2 | 3_changes_spread_cap_0_75 | 2 | 0 | 1 | -2.2999999999992724 | +4.059999999999491 | 0.7000000000007276 |
| w3 | 2_changes_no_spread_cap | 4 | 1 | 3 | -5.200000000002547 | +1.8799999999991996 | 0.0 |
| w3 | 2_changes_spread_cap_0_50 | 0 | 0 | 0 | 0 | +7.080000000001746 | 5.200000000002547 |
| w3 | 2_changes_spread_cap_0_75 | 0 | 0 | 0 | 0 | +7.080000000001746 | 5.200000000002547 |
| w3 | 3_changes_spread_cap_0_75 | 0 | 0 | 0 | 0 | +7.080000000001746 | 5.200000000002547 |
| w4 | 2_changes_no_spread_cap | 3 | 0 | 3 | -6.3199999999987995 | +0.820000000001528 | 0.0 |
| w4 | 2_changes_spread_cap_0_50 | 0 | 0 | 0 | 0 | +7.140000000000327 | 6.3199999999987995 |
| w4 | 2_changes_spread_cap_0_75 | 3 | 0 | 3 | -6.3199999999987995 | +0.820000000001528 | 0.0 |
| w4 | 3_changes_spread_cap_0_75 | 6 | 1 | 4 | -7.140000000000327 | 0.0 | -0.820000000001528 |
| w5 | 2_changes_no_spread_cap | 5 | 2 | 2 | -1.0199999999986176 | +3.280000000000655 | 0.0 |
| w5 | 2_changes_spread_cap_0_50 | 0 | 0 | 0 | 0 | +4.299999999999272 | 1.0199999999986176 |
| w5 | 2_changes_spread_cap_0_75 | 5 | 1 | 4 | -6.579999999999018 | -2.2799999999997453 | -5.5600000000004 |
| w5 | 3_changes_spread_cap_0_75 | 4 | 1 | 3 | -4.259999999999309 | +0.03999999999996362 | -3.240000000000691 |

## No-entry / failures
- No-entry (0 entries): `2_changes_spread_cap_0_50` on all v2 windows; `2_changes_spread_cap_0_75` and `3_changes_spread_cap_0_75` on w3 (spread gate filtered all signals). No-entry is **not** a profitable strategy; open marks are not realized profit.
- Failures: none. All baselines reproduced.

## Limits
- Retrospective exploration selected after prior outcomes (not a blind/unbiased holdout). Six sparse windows; not profitability evidence. 0.02 lots, next-quote assumed fills, gross -2/+2 exits with overshoot; spread cap is a research filter, not a fee estimate. Real latch/settings/orders untouched. Commission/swap/net PnL remain **NULL**. No continuous equity curve. Raw archives/economics not committed.
