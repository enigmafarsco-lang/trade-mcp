# OpenCode handoff: M1 saved-count extraction completed

**Respond only in English.** This handoff is a record of completed work, not
an instruction to repeat it.

## Status

The previous-minute-direction comparison ran once and is closed. OpenCode
reported creating the sanitized saved-count aggregate from its existing local
summary without replaying ticks. The aggregate is now published as
[`reports/research/xauusd-m1-saved-signal-counts.json`](../reports/research/xauusd-m1-saved-signal-counts.json)
in commit `1b94f493fe10af6128decb4cad116c5aa075c3a3` on branch
`arena/f81b6b27-trade-mcp`.

The workstation initially reported an HTTPS push authentication error. Arena
subsequently verified that the exact commit is on origin; that does not prove
how or by whom the authentication issue was resolved. Do not request or expose
credentials.

The extractor reported validating all 12 pairs and both paths against the
published M1 comparison, as well as the saved managed-baseline copies against
the earlier boundary report. Arena independently checked the published
aggregate's whitelist, arithmetic, original raw-signal totals and simulated
entry counts against the sanitized reports. The private source and raw archives
remain unavailable in Arena, so those checks neither independently reproduce
replays nor verify raw-archive identity.

## Safety and interpretation

- Do not rerun `scripts/xauusd_m1_previous_direction.py`, call its replay
  function on real archives, bypass its closure guard, remove any marker, or
  recreate/overwrite the completed aggregate—even if a different checkout is
  missing a local marker or file.
- No new history, MT5 request, order, strategy-model call, service restart,
  live-protection change or dependency installation is authorized. Git network
  operations are separate from the offline extraction. Do not publish raw
  ticks, economics, private paths, account information or credentials.
- The aggregate records eligible minute buckets and previous observed bid
  directions once per sample, plus raw buy/sell, previous-available and aligned
  counts per model. Raw signals occur **before** position, latch, spread and
  context filters: they are not trades. Direction entry-decision blocks are not
  all unaligned raw signals. Both models overlap and each window is independent.
- M1 is the signal timeframe, not a mandatory 60-second or candle-end close.
  Observed bid endpoints are not verified true candle open/close, and complete
  tick coverage, UTC, historical contract rules and actual fills remain unknown.
  Spread is included in modeled gross; commission, fees, swap, slippage and
  net remain unknown. The post-hoc results are not a blind or forward test.
- The earlier workstation pytest log was not found; the reported 32-versus-42
  test count remains unresolved. The supplied session date and sandbox clock
  also differed, so report labels and commit timestamps do not independently
  establish execution dates. No clock or recorded date was changed.

See the [updated comparison report](../reports/research/20261122T130500Z-xauusd-m1-previous-direction.md)
for the exact per-window counts and their limitations. No aggregate result is a
trading authorization or proof of profitability.
