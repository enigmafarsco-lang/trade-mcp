# OpenCode handoff: recover saved M1 opportunity counts

**Language: respond only in English. Do not ask the user to run commands, locate
files, upload raw data, or provide credentials.**

## Status and safety

The M1 previous-minute-direction experiment has already run once and is closed.
Its sanitized report is
[`reports/research/20261122T130500Z-xauusd-m1-previous-direction.md`](../reports/research/20261122T130500Z-xauusd-m1-previous-direction.md).
Do not rerun `scripts/xauusd_m1_previous_direction.py`, call its replay
function on real archives, remove a marker, or bypass its closure guard—even in
another checkout where the original marker is absent.

The missing information is **per-window and per-model eligibility and raw
signal counts**. The operator-reported aggregate 360/232 buy/sell totals have
not been independently verified here and are not a substitute: they combine
overlapping model runs, are counted before entry filters and are not actual
trades. The saved summary and original report are retrospective; they are
neither an unseen/forward test nor proof of net profitability.

## Do this on the workstation

1. In the workstation's checkout of this repository, confirm the current branch
   is `arena/f81b6b27-trade-mcp` and inspect Git status. Only if the tree is clean
   and the update is fast-forward, fetch and fast-forward that same branch to
   the revision provided in the handoff message. Do not switch branches, stash,
   reset, delete files, rebase, force-push, or overwrite the user's work. Stop
   and report a dirty tree or divergence without modifying it.
2. If `reports/research/xauusd-m1-saved-signal-counts.json` already exists,
   stop and report only that it exists; do not print its contents, overwrite,
   delete or commit it. Otherwise run the new aggregate-only extractor once:

   ```text
   .venv/bin/python scripts/xauusd_m1_saved_signal_counts.py
   ```

   It reads only the existing private local summary from the completed job and
   the tracked sanitized M1 report. Before publishing it checks all 12 pairs, their
   baseline matches, model parameters, entries/exits, open positions, latches,
   gross marks and pair differences. It also checks the six previously
   published row counts and three-change raw buy/sell counts. A match is an
   aggregate consistency check, **not** a cryptographic raw-archive identity
   check. The extractor creates only the new sanitized aggregate JSON,
   exclusively, with permissions no broader than `0600`; it leaves source files
   and markers unchanged. It refuses symlinked inputs and output directories.
3. If it fails, stop and report its fixed, data-free error/status; a write failure
   may leave an incomplete new output, which must be left unchanged. Do not inspect
   or print raw quote archives, economics files, account data, private paths,
   exceptions or credentials; do not fetch new history, substitute data, change
   settings or rerun a replay.
4. If successful, report every sample and model separately: eligible minute
   buckets and previous observed directions; raw buy/sell signals; signals
   with an available previous minute; and those also aligned. Report simulated
   entries and entry-decision block counts as separate fields. Raw signals are
   **before** position/latch/spread/context filters and are not entries. Do not
   combine overlapping models into a continuous-account result. Preserve
   `commission`, `fees`, `swap` and `net_pnl` as null.
5. Review the generated JSON for sanitization; commit only
   `reports/research/xauusd-m1-saved-signal-counts.json` and push only the same
   current branch if it remains fast-forward and safe. Git fetch/push is the
   only network activity allowed here, separate from the extractor. If GitHub
   authentication fails, do not request or expose credentials; report that the
   GitHub connection needs attention. If publication is blocked, return the
   sanitized aggregate results and status in English.

No MT5, order, strategy-model call, new history request, live-guard change,
service restart or dependency installation is authorized. The earlier
workstation pytest log was not found; the reported 32-versus-42 test count
remains unresolved and must not be guessed. No synthetic or aggregate result
is a trading authorization.

## M1 and comparison semantics

Signals use the forming M1 minute. The replay has no mandatory 60-second or
candle-end position close. Both tested paths require an adjacent previous
interior minute with at least two observed quotes; the filtered path additionally
requires the first-to-last observed bid direction to agree with the signal.
Observed bid endpoints are not verified true candle open/close, and complete
tick coverage, UTC, historical contract rules and actual fills remain unknown.
The supplied session date and sandbox clock differed during this review; report
labels and commit timestamps do not independently establish execution dates.
Do not change clocks or infer dates from those labels. The two paths have the
same time-context eligibility but can take different
trades and develop different simulated latches. A positive Pda-minus-Pav gross
difference can merely mean a smaller loss; it is not proof of improvement in
real execution. Spread is already modeled and must not be deducted twice.
