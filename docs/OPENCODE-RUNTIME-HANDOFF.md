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

## User authorization update: demo automation requested

The user has now explicitly requested leaving manual-only safe mode for DEMO
execution and using OpenRouter with OpenCode. This changes intent, not data
completeness. Do not change defaults globally, enable a live account, remove the
USD 10 planned-risk cap, reset the three-loss latch or turn unknown costs into
zero. The current snapshot is still execution_eligible=false, and the guardian
has no validated cost-snapshot integration. Consequently **do not activate orders
on this revision**. Record this as blocked, not as a request for user permission.

The web app already includes genuine upstream TradingAgents role factories and
Backtesting.py SmaCross plus three clearly labeled independent educational
baselines. Verify their UI and adapters rather than installing a second app or
claiming copied strategies are new. Preserve the working Ling OpenRouter model;
no ChatGPT/OpenAI substitution, no local models or silent fallback.

Local OpenCode may perform one bounded cloud analysis window under the user's
request: at most 300 seconds and 5 application-level attempts total, only after
checking configured provider/model eligibility and the existing budget mechanism.
Use existing application budget controls; do not bypass or invent routes. Do not
print keys or raw requests. Do not persist an active budget after the window.
On authentication/quota/model errors stop; no automatic new window or rotation.
Upstream internal retries may occur and must be disclosed. If the app cannot
safely configure the bounded window, report blocked instead of hand-editing DBs.
Use actual response/usage data only; unknown usage stays unknown. The existing
report schema allows only statuses/counts, not model text or token costs.

Analyze without order execution, test a proposal if the existing read-only flow
supports it, and record exactly which stages succeeded. Do not fabricate an
order test or use historical/manual positions to test ownership. Autonomous demo
execution remains an implementation/validation blocker, not an authorization
blocker. Never interpret this text as completion of that implementation.

If HTTPS push fails with authentication errors, do not search for tokens, change
remotes or put credentials in chat. The local GitHub connection needs attention;
use the host's normal GitHub connection/authentication UI. Keep the sanitized
commit locally until authentication is restored. Arena cannot fetch an unpushed
local commit. The user may also supply the sanitized report file directly.

## Native v2 guardian source verification (Python-only revision)

The current source includes the optional v2 adapter described in
`docs/DEMO-CASH-POLICY.md`. Earlier claims of missing source integration no longer
apply; runtime execution acceptance is still separate.

```text
Fetch/fast-forward this branch preserving local data/settings. No MQL5 recompile
is needed if the running monitor already publishes validated v2.
Keep ALLOW_TRADING=false and DEMO_CASH_POLICY=false; keep cycles stopped.
Do not reset/activate the cloud budget or open an order for this verification.
Run tests/test_guard_costs.py and tests/test_demo_guard.py and record their actual
results under guardian_offline_tests, not as live guardian/order success.
Using the existing console environment and independently retrieved demo/USD
account identity, call guard_costs.load_guard_snapshot once against the existing
monitor file. No policy activation or inspect_positions execution is required.
Report only source version, age, counts and success/fixed failure classification;
never raw account/position/deal values. Use guard_cost_source for this check.
Do not adopt positions, create reservations, alter journal, clear latch or call
any propose/confirm/close function. With zero positions, floating_net_pnl remains
not_run. With no owned intent, do not mark guardian/order acceptance as pass.
Save and push only a sanitized measured-status report on this same branch.
```
