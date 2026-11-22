# Diagnose saved sparse-screen failure offline

Stay on `arena/f81b6b27-trade-mcp`; fetch and fast-forward to the commit containing
this document. Run exactly once from the repository root:

```bash
.venv/bin/python scripts/inspect_xauusd_sparse_error.py
```

This reads only the existing owner-only `eval-1-raw-response.json`, and writes a
new owner-only `offline-error-diagnostic.json` in the existing v1 directory.
It prints only fixed structural flags and fixed literal phrase-match labels.
No network, MT5, installs, restart, trading, cloud/model calls, or modifying or
repeating the historical screen. Do not remove/rename its directory or output.

Record only the sanitized output and the limits: literal phrase hints are NOT
verified root causes; empty hints do not establish any cause. If the file is
missing, inaccessible or unparseable, report that fixed blocker without exposing
error payload, prices, accounts, credentials or local secrets. Do not attempt a
new history request. No manual rephrasing of provider text into the Git report.

Add a sanitized report under reports/research, commit/push only that report on
this branch. Check origin after push errors; report actual diagnosis once in Persian.
Trading OFF, cloud revoked, all existing protections and journals unchanged.
