# Experimental directional hourly forecast

Chart responses now include `trend_forecast`, separate from `hourly_scenarios`.
This is a genuine point extrapolation, not the previous fixed-center volatility
fan and not an LLM prediction. Independent OLS on 32 closed log prices estimates
slope; the last close anchors predictions at +15/+30/+60 minutes. The chart draws
ONE dashed path: cyan 15m, amber 30m, rose 60m, in a separate future area labeled
in elapsed minutes (not the historical candle spacing). Forecast prices enter
the y-axis scale; fabricated future candles and probabilities are not drawn.

Supported chart inputs M1/M5/M15. Requires at least 32 + 60min/timeframe + 20
closed bars, contiguous recent data and fresh last closed bar. Unsupported,
stale, gapped or unstable inputs return unavailable. No invented replacement.
A Saturday/weekend gap can legitimately suppress the forecast.

Each horizon reports rolling-origin MAE, last-price/no-change baseline MAE and
strict directional hit rate over 20 fully observed historical origins. Each
origin fits only its past 32 observations. Adjacent origins overlap and samples
are small; these are diagnostics, not independent proof of generalization,
calibrated probability or profitability. No parameter selection is performed
on these errors. Confidence intervals and news effects are not modeled.
This simple extrapolator can fail badly on reversals and must not authorize orders.
`validated_for_trading=false`, `execution_eligible=false` always.

The old volatility overlay has been replaced, not presented as directional
prediction. The explicitly labeled diagnostic table remains separate. No
LLM/cloud request, budget activation or order is made by forecast generation.

## Deployment / verification

Fetch/fast-forward current branch; preserve journal, old proposals and secrets.
Before console restart require fresh flat/no-orders/no-unresolved-intents state;
otherwise preserve guardian. Keep trading/policy off and cloud budget revoked.
Call /api/chart?symbol=XAUUSD&tf=M15&full=1 once and verify trend_forecast provenance,
as-of, points and error metrics. Inspect one future path in the browser; report
visual inspection unavailable if no browser exists. Do not repeat a five-minute
trading session just to test chart changes.

Zero orders in the reported 257-evaluation window were explained by scenario
confidence 50 below guarded threshold 62. That is not an execution-confirmation
bug. The bounded endpoint now includes categorized refusal_counts; it does not
lower thresholds or manufacture signals. Calling it more frequently cannot make
unchanged closed-bar evidence into an independent new opportunity.

## Failed-skill suppression

The deployed incident showed MAE worse than no-change at all horizons and
strict directional hit rates below 0.5. It must NOT be called a useful forecast.
The API now withholds drawable points unless every horizon beats the no-change
MAE and exceeds 0.5 directional hit rate on its small historical diagnostic.
Failure returns `failed_historical_baseline_check` and diagnostics only. Passing
this screen is not out-of-sample validation: selecting which forecasts to show
using historical errors introduces selection bias. Profitability is unproven.
The chart shows an explicit Persian unavailable/failure notice instead of lines.
The old volatility table is removed from the comparison UI (API compatibility
is retained). This does not implement an accurate nonlinear hourly price path.
No intermediate future candles are invented to make the display look realistic.
