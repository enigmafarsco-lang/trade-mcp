# Sanitized runtime evidence

Only reports produced by `scripts/publish_runtime_report.py` belong here.
Raw logs, snapshot files, accounts, prompts, provider responses, credentials and
financial values must never be committed. Reports contain status enums/counts
only. Unknown checks are `not_run`; unknown counts are null, never invented zero.
Reports are evidence, not permission to trade. No runtime result exists merely
because this directory exists.

## Offline Day Trader test evidence

`daytrade_offline_tests` records the result of the complete synthetic
`tests/test_daytrade.py` run, not a live broker or browser acceptance check.
Its `commit` is the source revision actually tested, even when the report
publisher itself was added or updated in a later revision. `generated_at` is
when the report was created, not necessarily when the tests ran.

For a `pass`, all six measured count fields are required:
`daytrade_pytest_passed`, `daytrade_pytest_skipped`, `daytrade_pytest_failed`,
`daytrade_pytest_errors`, `daytrade_pytest_total`, and
`daytrade_pytest_exit_code`. The exit code must be zero, at least one test must
pass, failures/errors must be zero, and total must equal passed plus skipped.
A skipped test remains skipped: do not label the skipped UI/browser check or any
live MT5 validation as passed. When evidence is incomplete, use `blocked` and
omit unknown counts rather than inventing zeros. Raw pytest output and local
paths remain private, never in this report.
