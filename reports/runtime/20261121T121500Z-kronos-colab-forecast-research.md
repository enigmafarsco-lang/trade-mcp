# Kronos Colab forecast — offline research inspection

- **Report:** `20261121T121500Z-kronos-colab-forecast-research`
- **HEAD:** `e6459f88d83b26565016b2a726ca9ac37a147a03`
- **Trading/budget/guardian:** OFF / revoked / preserved. No execution connection.

## Artifact validation (raw prices not printed; raw file not committed)
- Preserved owner-only gitignored: `logs/research-kronos-20261121T090633Z/kronos_forecast_XAUUSD_M1.json` (dir 700, file 600), sha256 `afb43301c379f6a49daa8b18eec282bd5147dbfa5b11c37a33ba25a22380c4a3`
- Schema/source: `XAUUSD M1`, `source=official_KronosPredictor.predict`, upstream `67b630e67f6a18c9e9be918d9b4337c960db1e9a`, 60 candles schema OK.
- Timestamps/freshness: `forecast_origin=1795202940` (`2026-11-20T19:29:00Z`), age ~36453s at inspection, **fresh_120s=false (stale)**.
- Postprocessing audit (disclosed): `prices_modified=true`, OHLC policy `envelope_preserve_open_close_with_raw_audit` (3 adjustments), volume policy `negative_to_zero_with_raw_audit` (37 adjustments).
- Evaluation: 1 independent origin, 60 holdout candles, `validated_for_trading=false`, warning “One holdout origin is not profitability or generalization proof”.
- Baseline screen (preserved, not rewritten): `passed=false`, failed horizons `[15,30,60]`, independent_origins=1.

| Horizon (min) | Passed baseline | Error/Baseline ratio |
|---:|:---:|---:|
| 15 | No | 1.117746 |
| 30 | No | 2.501242 |
| 60 | No | 7.691637 |

## Offline research chart
- Generated and presented: `logs/research-kronos-20261121T090633Z/kronos_research_failed_baseline_chart.svg` (gitignored).
- Label: **"Historical Kronos research — failed baseline — not a live signal"**. Context (180 closed M1, redacted, verified UTC) + 60 predicted candles separated; 15/30/60m marked; OHLC/volume postprocessing disclosed. No fabricated context/future prices.

## Not taken
- No `TRADE_MCP_KRONOS_FORECAST_PATH` set to this failed/stale artifact. No live-loader/execution bypass. No baseline rewrite. No new inference/retry/cloud budget activation/trading.
