# XAUUSD sparse screen v2 — 5 fixed windows (bounded, executed once)

- Branch: `arena/f81b6b27-trade-mcp`
- Command executed (once): `.venv/bin/python scripts/xauusd_sparse_screen_v2.py`
- v1 untouched (`xauusd-sparse-screen-20261109-10-v1` not modified/removed). Trading OFF, cloud budget revoked. No orders/installs/restarts/model/provider calls.
- Limits: 1 read-only tick-history request/window, sequential, max 2000 rows, no retries, no alternate provider. Stop rule honored; no stop occurred.
- `status=completed_5_windows`, `attempted_history_calls=5`, `stop_reason=null`.

## Per-window actual outcomes (independent hypothetical, flat start, simulated 3-loss latch preserved per window; NOT continuous backtest)

| Window | Broker-wall-clock (UTC unverified) | Status | Returned rows | Replayed rows | Signal buy/sell | Entries | Loss exits | Profit exits | Blocked by sim latch | Gross realized USD (model) | Gross open mark USD (model) | Gross total mark USD (model) | Open at end | Sim 3-loss latch | net_pnl |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|:---:|---:|
| w1 | 2026-11-09 13:00–13:05 | simulated | 647 | 385 | 20/5 | 4 | 2 | 1 | null | -1.7199999999993452 | -0.5400000000008731 | -2.2600000000002183 | true | false | null |
| w2 | 2026-11-09 19:00–19:05 | simulated | 738 | 429 | 6/9 | 3 | 3 | null | 5 | -6.359999999998763 | null | -6.359999999998763 | false | true | null |
| w3 | 2026-11-10 09:00–09:05 | simulated | 820 | 498 | 16/10 | 3 | 3 | null | 15 | -7.080000000001746 | null | -7.080000000001746 | false | true | null |
| w4 | 2026-11-10 13:00–13:05 | simulated | 745 | 482 | 24/13 | 6 | 4 | 1 | null | -6.380000000000109 | -0.7600000000002183 | -7.140000000000327 | true | false | null |
| w5 | 2026-11-10 19:00–19:05 | simulated | 862 | 518 | 25/12 | 4 | 3 | 1 | 9 | -4.299999999999272 | null | -4.299999999999272 | false | true | null |

- No skipped/failed windows. No stop reason. Losses recorded and windows continued per rule.
- Raw data owner-only under gitignored `logs/research-inputs/xauusd-sparse-screen-20261109-10-v2-5win/`; not committed. Unknown fees/net PnL remain **NULL**. Not profitability proof.
