# XAUUSD M1 previous-minute direction: retrospective comparison

## Finding

OpenCode reported **12/12 paired comparisons** and **12/12 aggregate baseline
matches** on six previously examined samples. The private source summary and
raw tick archives are not available in this environment; the results below are
read from the [sanitized report](20261122T130500Z-xauusd-m1-previous-direction.json),
not from a new replay.

The direction filter produced **six higher, four lower and two unchanged gross
marks** than its matched-availability control. The filtered mark itself was
**negative in 11 of 12 cases**. Its only positive case was **+$0.10 modeled
gross** for the two-change/$1-cap model in w1; the matched control was +$2.14.
The filter is not ready for live trading and these results do not establish
whether it is profitable out of sample.

**Sign correction:** the original table heading said `Pav − Pda`, but its
numbers are actually **Pda − Pav**. For example, the two-change additional
sample is `−4.02 − (−8.54) = +4.52`. The legacy JSON field name
`aligned_minus_marked` is retained, with its definition documented in the JSON.

## Counts by model

These are descriptive counts across six independent hypothetical scenarios
per row, not one continuous account, a live record, or an independent
statistical sample. Each scenario starts flat with its own simulated loss latch.

| Research model | Mode | Entries | Profit exits | Loss exits | Open at sample end |
|---|---|---:|---:|---:|---:|
| Three changes, no spread cap | Pav | 25 | 6 | 16 | 3 |
| Three changes, no spread cap | Pda | 17 | 4 | 12 | 1 |
| Two changes, $1 spread cap | Pav | 29 | 8 | 20 | 1 |
| Two changes, $1 spread cap | Pda | 17 | 5 | 11 | 1 |

The three-change model had two positive, two negative and two zero paired
differences. The two-change model had four positive and two negative paired
differences. These are overlapping models on the same six samples; the counts
do not validate an edge or justify switching live settings.

## Per sample and model

`Pav` is the matched control: the previous adjacent interior minute is
available with at least two observed quotes. `Pda` additionally requires the
previous observed bid direction to align with the signal. A cell shows
**entries / profit exits / loss exits**, all hypothetical. Marks and differences
are modeled gross USD. An open mark includes unrealized simulated P/L.

| Sample | Research model | Pav entries/profit/loss | Pav gross | Pav open | Pda entries/profit/loss | Pda gross | Pda open | Pda − Pav |
|---|---|---:|---:|---|---:|---:|---|---:|
| additional_sample | original_3_changes | 5/1/4 | -6.08 | no | 5/1/4 | -6.08 | no | 0.00 |
| additional_sample | 2_changes_spread_cap_1_00 | 10/3/7 | -8.54 | no | 6/2/4 | -4.02 | no | +4.52 |
| w1 | original_3_changes | 4/2/2 | 0.00 | no | 3/1/2 | -2.12 | no | -2.12 |
| w1 | 2_changes_spread_cap_1_00 | 3/2/1 | +2.14 | no | 2/1/1 | +0.10 | no | -2.04 |
| w2 | original_3_changes | 3/0/2 | -5.68 | yes | 1/0/1 | -2.08 | no | +3.60 |
| w2 | 2_changes_spread_cap_1_00 | 2/0/2 | -4.22 | no | 1/0/1 | -2.14 | no | +2.08 |
| w3 | original_3_changes | 6/1/4 | -8.66 | yes | 2/1/1 | -0.08 | no | +8.58 |
| w3 | 2_changes_spread_cap_1_00 | 6/1/4 | -8.86 | yes | 2/1/1 | -0.58 | no | +8.28 |
| w4 | original_3_changes | 3/1/1 | -0.96 | yes | 2/0/1 | -2.96 | yes | -2.00 |
| w4 | 2_changes_spread_cap_1_00 | 3/1/2 | -1.70 | no | 2/0/1 | -2.98 | yes | -1.28 |
| w5 | original_3_changes | 4/1/3 | -4.72 | no | 4/1/3 | -4.72 | no | 0.00 |
| w5 | 2_changes_spread_cap_1_00 | 5/1/4 | -5.92 | no | 4/1/3 | -4.26 | no | +1.66 |

All 12 Pda cases had at least one simulated entry. The zero Pav mark in the
three-change w1 case had four entries; it was not a no-trade case. In both w4
Pda cases the reported gross mark includes an open hypothetical position.

## Coverage, costs and verification

- The six older published row counts sum to **4,827**. The earlier three-change
  signal reports list 107 raw buys and 66 raw sells before entry filters across
  those windows. These are prior reports, not newly replayed data, and matching
  summary metrics does not prove raw-archive identity.
- The committed M1 report does not include per-window eligible-minute or
  direction-aligned raw-signal counts. A separate read-only extractor is
  prepared in `scripts/xauusd_m1_saved_signal_counts.py`; it can use the existing
  private summary without replaying ticks. Until that extraction is reported,
  those counts are unknown here.
- The observed first/last bid of a prior minute is not proof of the true candle
  open/close or complete feed. UTC, historical contract rules, actual fills,
  slippage, account risk and continuous performance remain unverified.
- Timekeeping needs reconciliation: this session's supplied date is 12 November
  2026, while the sandbox clock, report labels and inspected commit timestamps
  show 22 November 2026. Those labels do not independently verify when the
  research occurred; no clock or recorded date was changed.
- Spread is already included in modeled gross. Commission, fees, swap and net
  are unknown, not zero. The hypothesis and parameter choices were made after
  prior results; these are not blind, genuinely unseen or forward tests.
- M1 is the **signal** timeframe; there is no mandatory 60-second or candle-end
  position close. The study authorizes no order or change to trading protections.
- OpenCode reported 32 passing synthetic cases on the workstation. Before this
  report was corrected, Arena collected 42 parametrized cases from the same
  two files at the execution revision. The workstation pytest log was not
  available, so the reason for the discrepancy is unresolved.

The one-shot replay is completed and closed, even if its local marker is absent
in another checkout. Do not rerun it or remove its marker. The guarded CLI now
refuses before reading inputs or replaying; the guard must not be bypassed.
Trading-off and cloud-budget status are operator-reported, not independently
verified by this aggregate review.
