# Execute locally: XAUUSD historical research discovery, no trading

Perform this work now in the existing user checkout. Do not ask the owner to run
Colab or relay intermediate planning messages. Arena cannot reach the owner's
localhost MT5; you must execute the local read-only check and report its real result.

1. Stay on `arena/f81b6b27-trade-mcp`. Fetch origin and fast-forward only. Preserve
   all existing local fixes, secrets, journals, raw archives and runtime settings.
2. Run exactly once, from the repository root:

```bash
.venv/bin/python scripts/xauusd_research_check.py \
  "logs/research-inputs/xauusd-capabilities-$(date -u +%Y%m%dT%H%M%SZ).json"
```

3. Inspect the resulting safe report locally. Do not claim data/backtest success
   based on tool names. If discovery fails, stop without retries/restarts/installs.
4. If there is no advertised historical tick/quote tool, report that concrete
   limitation. Do not fabricate tick data from OHLC, sample current weekend quotes,
   or substitute a different market. Preserve the report as evidence.
5. If a candidate exists, inspect its actual schema and read-only semantics locally.
   Do not invent tool/parameter names. Only after confirming it is history-read-only,
   make ONE bounded request for already-closed XAUUSD tick history: at most five
   minutes of the last known session, at most 10000 rows, no retries or fallback.
   Use only explicitly supported bounds; if a bounded call is impossible, stop.
   Keep the raw response owner-only under ignored logs. Verify actual bid AND ask,
   timestamp semantics, ordering, continuity/coverage and whether these are true
   historical ticks or received samples. Unknown clock stays unknown, not UTC.
   Report presence/counts/verified capabilities only, never prices/accounts/raw text.
6. If a valid sample is obtained, report its owner-only path and actual limitations.
   Do not call that sample a completed profitability test. Do not install packages,
   change Wine/MT5 settings, compile MQL5 or add another quote writer.
7. Trading, paper/demo execution policy and revoked cloud budget remain OFF.
   No order/account tools, no paid calls, no model inference, no Binance requests.
   Maintain existing protections. Do not clear unresolved intents or loss latches.
8. Write a sanitized factual result under reports/research, test any source edits,
   then explicitly stage intended sanitized files, commit and push only the current
   branch. Never commit raw inputs, schemas containing secrets or .env. If push
   reports an error, verify origin before asking for Desktop action.
9. Return ONE concise Persian completion report: command actually executed,
   historical-tick capability verified or not, actual data sample obtained or not,
   exact blocker, sanitized report commit. No promises of completed backtesting.
