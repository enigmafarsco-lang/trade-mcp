# Execute one complete local XAUUSD cash-scenario research job

Do this work now; do not return a plan or ask the owner to run Colab/commands.
Keep current branch arena/f81b6b27-trade-mcp. Fetch and fast-forward only.
Commit483d85b already exists on origin; no authentication work for that report.

Run exactly once on the archived ticks already present:

```bash
.venv/bin/python scripts/run_xauusd_cash_research.py \
  logs/research-inputs/xauusd-ticks-corrected-20261122T073154Z/raw-response.json \
  "logs/research-inputs/xauusd-cash-research-$(date -u +%Y%m%dT%H%M%SZ)" \
  --symbol XAUUSD_i
```

The job validates existing ticks, initializes native MCP, checks the advertised
schema, and calls ONLY get_marketwatch_symbols once with symbol filter and limit10.
It extracts only actual reported economics, no account/balance/position/deal/order
reads. It does not fetch quotes/ticks or use NativeBridge's read-retry wrapper.
No retries, fallbacks, installs, restarts, model/cloud/Binance/paid calls.

If a required property is missing or profit calculation mode is unsupported,
the job stops with a factual summary. Do NOT provide defaults (100-ounce contract,
USD currency, zero commission etc.). Do not make additional calls to fill gaps.
Report exact fixed missing field names from summary, not a guessed result.

When economic fields are complete, the job executes the gross USD research
scenario automatically on the same sample. This is NOT broker-validated PnL:
- actual TickMomentum.observe, three aligned changes; first/last minute excluded;
- one simulated position, fixed0.02lot, entry at NEXT observed quote within same
  minute/max2sec; buy ask/exit bid, sell bid/exit ask; spread included;
- gross cash thresholds -2/+2, at first observed crossing with overshoot preserved;
- simulated three-consecutive-loss latch, no auto-reset; no change to real latch;
- no same-quote reentry, no forced close at sample end; residual mark reported;
- commission/swap/net PnL remain NULL, never assumed0;
- no actual broker fills, historical contract-rule verification, equity/margin or
 0.5% risk sizing/other execution guard proof. Model results do not authorize trades.

Preserve all raw/economic/full scenario files owner-only under ignored logs.
Inspect summary. Commit/push only a sanitized factual report under reports/research
on the current branch (counts, gross-model results if produced, assumptions,
net=NULL, blockers). Do not commit prices, contract snapshot, accounts or secrets.
Check origin after any push error before reporting authentication trouble.
Keep trading OFF, cloud budget revoked, guardian and all settings/journals intact.
Return one Persian completion report containing actual execution and results.
