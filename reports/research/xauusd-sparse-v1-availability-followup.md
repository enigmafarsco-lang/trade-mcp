# Sparse XAUUSD v1: saved availability-label follow-up

This records the user's report of a local offline comparison; Arena does not have
access to the original response and has not independently re-run the comparison.

The reported failed response is for `XAUUSD_i` / `tick`, with `ok=false`, an
empty `history`, a `data_available_from` label, and no explicit error reason.
The comparison reported that the availability label is **at or before the start**
of every one of the six pre-registered windows, including the failed first
window. It therefore does **not support** the simple hypothesis that the failed
window precedes that label, if they share a common time basis. The common time
basis and provider semantics remain unverified; this is not a verified root cause,
proof that data existed, or proof of tick completeness. No provider timestamp,
prices or raw payload are included.

No fresh history call or replay was made by Arena in this follow-up. The v1 run
remains stopped after its first error and must not be retried or have its marker
removed. No extra signal or PnL result is available. The prior assessment's claim
that multiple distinct bounded history requests are inherently retries is not
correct; separately defined calls are distinct requests, but this does not waive
the v1 stop/no-repeat policy. Trading execution is not authorized by this report.
