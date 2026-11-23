# OpenCode handoff: publish the existing offline Day Trader test report

The user relayed an OpenCode run on branch `arena/f81b6b27-trade-mcp` at tested
commit `983e5039bfa421682b70e8116840ad6e493b9e8e`: pytest return code 0,
53 passed, 1 skipped, 0 failed/errors, 54 total. Its detailed output was reported
saved locally with mode `0600`. These are **reported** values, not evidence
independently examined by Arena. Confirm them from that exact existing local
run before publishing; do not replace missing evidence with assumptions.

```text
Work only in the existing workstation checkout and stay on branch
arena/f81b6b27-trade-mcp. Preserve all pre-existing untracked files, nested
repositories, local settings, secrets, logs, journals and completed job markers.
Do not reset, rebase, switch branches, overwrite, delete or clean them.

Check that no tracked or staged changes would be disturbed. Fetch this branch
and fast-forward only. If it is dirty, diverged, the update conflicts, or GitHub
authentication fails, stop and report the condition without disclosing credentials
or private paths. Do not force push. The current branch now includes a narrow
publisher schema for daytrade_offline_tests; use that updated publisher.

Use the already-saved owner-only pytest output and its actual exit status from
this run. Do NOT rerun pytest, a completed one-shot job, historical research,
MT5, quote/history requests, a model, or an order; do not restart a service or
change any live protection. Verify the revision actually tested. The report's
commit MUST identify that tested revision (reported above as 983e5039...), NOT
the newer HEAD containing the publisher. If the tested revision is unknown,
stop rather than invent it.

Inspect only the run's own known evidence; do not search unrelated logs. Verify
counts and exit code without exposing the raw output. The user's relay says
53 passed, 1 skipped, 0 failed/errors, 54 total, RC 0. If evidence differs, use
only the measured values. If complete counts cannot be verified, record the check
as blocked and leave unknown counts absent; do not infer unknowns are zero. A
skip is not a passed UI/browser or live MT5 test. No account, quote, history,
provider, system paths, raw output or secrets belong in the public report.

Create a new owner-only local input JSON under ignored logs/ using a unique name
and exclusive creation; do not overwrite the saved raw log. It must contain only
commit, checks, and counts accepted by scripts/publish_runtime_report.py. For a
verified successful run, the shape is:

{
  "commit": "983e5039bfa421682b70e8116840ad6e493b9e8e",
  "checks": {"daytrade_offline_tests": "pass"},
  "counts": {
    "daytrade_pytest_passed": 53,
    "daytrade_pytest_skipped": 1,
    "daytrade_pytest_failed": 0,
    "daytrade_pytest_errors": 0,
    "daytrade_pytest_total": 54,
    "daytrade_pytest_exit_code": 0
  }
}

This is a template reflecting the user's relay, not permission to assume its
contents. For missing/unverifiable output use blocked and only verified counts.
For an actually failing run use fail and actual verified counts; never relabel
it pass. All other checks/counts stay not_run/null through the publisher.

Before creating a report, check the tracked sanitized runtime reports for an
existing daytrade_offline_tests report for the tested commit. If the same
verified result is already recorded, do not duplicate it; verify whether its
commit is pushed instead. Do not inspect unrelated private logs.

Use .venv/bin/python scripts/publish_runtime_report.py with the exact input and
an unused, unique reports/runtime/*-daytrade-offline.json output name. Its
exclusive creation must fail rather than overwrite an existing report. The
publisher's generated_at is the report creation clock, not the test timestamp.
Review the output for only allowed fields and the independently checked counts.
No raw input or detailed evidence may be committed.

Confirm the index has no pre-existing staged changes. Stage ONLY that exact
sanitized generated report filename, inspect its staged diff, and commit it on
the same branch. Push using:

git push origin arena/f81b6b27-trade-mcp

If push fails, retain the local commit, do not force or request credentials,
and report the commit hash and failure category. On success, report the tested
commit, report commit, push status, verified pass/skip/fail/error counts and
that no tests or live operations were rerun. Do not share the raw log, private
paths, account information or tokens.
```
