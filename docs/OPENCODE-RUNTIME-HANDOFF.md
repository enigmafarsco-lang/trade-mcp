# Local execution and evidence handoff

Give the following instructions to the local OpenCode agent. Repository source
changes do not prove MT5 deployment or successful trading. Do not claim that
unfinished net-P/L integration is complete.

```text
Work only on branch arena/f81b6b27-trade-mcp in
/media/joshua/SATA500/Trade/trade-mcp.
Fetch origin and fast-forward this branch without resetting local changes.
Preserve nested repos, secrets, journal, logs, settings and Wine session.
Do not install packages, restart Wine, kill processes, change cloud models,
activate cloud budget, enable trading, or place an order during this validation.

Read docs/COST-SNAPSHOT.md. Copy TradeMCPCostMonitor.mq5 and
TradeMCPCostExport.mqh together into the active terminal MQL5/Experts folder,
backing up pre-existing files locally rather than overwriting unrelated work.
Use the confirmed active data directory, not a guessed installation directory.
Compile with the existing MetaEditor. If Wine cannot launch it, request only the
specific GUI compile operation. Record the real result, including failure.
Do not treat prior compilation of the old script as this monitor's compilation.

After successful compilation, attach the monitor to a dedicated empty chart
without replacing an EA. Leave Allow Algo Trading disabled. Use a bounded From,
MaxDeals=10000, IntervalSeconds=5. Run only one writer; do not also run the script.
If GUI access is unavailable, identify the exact manual action, not a fake success.

Validate two independently updated snapshots at least five seconds apart with
read_cost_snapshot, independently obtained live account identity and default
freshness checks. Verify generation advances and both snapshots are fresh.
Record only aggregate count/status; never expose snapshot contents or identifiers.

Do not enable execution: monitor data is not yet connected to the guardian.
Inspect remaining source gaps: closed-position volume reconciliation, history
coverage, separately booked/late costs, floating net P/L and atomic account
identity. Mark unverified checks blocked or not_run. Do not bypass entry gates,
adopt historical/manual positions, reset the loss latch or fabricate results.
Existing analysis/proposal/order code is not proof of a successful live run.
No new cloud request is authorized by these deployment instructions.

Create ignored logs/runtime-report-input.json with:
{"commit":"FULL_ACTUALLY_LOADED_COMMIT","checks":{"compile":"pass"},"counts":{}}
Replace the example checks with measured states only. Allowed states:
pass, fail, blocked, not_run. Allowed checks/counts are defined in
scripts/publish_runtime_report.py. Omit unknown counts; never invent zero.
Keep diagnostic detail in ignored local logs with secrets removed.
```

Run locally after building the actual input:

```bash
.venv/bin/python scripts/publish_runtime_report.py \
  logs/runtime-report-input.json \
  "reports/runtime/$(date -u +%Y%m%dT%H%M%SZ).json"
```

Review the generated report, then stage **only its exact filename**. Never use
`git add .` or add the input/logs. Commit and push to the same branch:

```text
git add reports/runtime/EXACT_GENERATED_FILENAME.json
git commit -m "Record sanitized local MT5 runtime validation"
git push origin arena/f81b6b27-trade-mcp
```

Replace the filename placeholder with the actual output. On non-fast-forward
push, stop and report; do not force, reset, rebase or switch branches. Report the
pushed commit to the user so the repository evidence can be fetched and reviewed.
A blocked deployment still warrants a truthful report, not an absent report.
