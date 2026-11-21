# Kronos multi-origin evaluation workflow (offline, no inference)

- **Report:** `20261121T123500Z-kronos-multi-origin-eval-workflow`
- **Branch:** `arena/f81b6b27-trade-mcp`
- **Safety:** trading OFF, cloud budget revoked, guardian preserved. No new inference/model retry/cloud budget activation. Failed/stale Kronos artifact not used as live forecast.

## Existing pieces checked
- `src/trade_mcp/engines/upstream.py`: had `kronos_predict` (postprocessing audit) and single-origin `evaluate_kronos` (final 60-bar holdout, no-change baseline only). Missing: multi-origin, fixed split labeling, simple trend baseline, OHLC/volume correction frequency/size summary, final-test separation.
- `scripts/upstream_strategy_worker.py`: single-origin worker path only; no multi-origin evaluation workflow.
- `notebooks/kronos_colab.py/.ipynb`: single-origin holdout in Colab runtime; no offline multi-origin reproducible workflow.
- `src/trade_mcp/kronos_artifact.py`: loader/gate only.

## Implemented (missing functionality only)
- `src/trade_mcp/engines/upstream.py`:
  - `simple_trend_close_baseline(...)`: deterministic linear-slope close baseline over last `lookback` closes; training rows only, no future leakage.
  - `fixed_multi_origin_splits(...)`: deterministic fixed historical splits; chronological origins with min context/horizon/step; last `final_test_origins` labeled `final_test`.
  - `evaluate_kronos_multi_origin(...)`: reproducible multi-origin evaluation over fixed splits, no leakage, same predictor/settings/postprocessing path (`kronos_predict`) per origin. Compares 15/30/60m Kronos absolute error against **no-change** and **simple trend** baselines. Reports per-horizon errors/ratios, per-split summary, overall summary, and OHLC/volume correction frequency/size. `final_test_used_for_tuning=False`.
- Tests: `tests/test_kronos_multi_origin.py` (deterministic splits/no leakage/final-test separation, simple trend baseline, multi-origin report/baselines/postprocessing aggregation). No inference; predictor mocked.

## Not done / boundaries
- No new paid service or inference run. No model tuning/selection on final test set.
- Workflow is separate from M1 tick-momentum execution strategy; does not enable trading or connect stale/failed artifact to execution.
- Failed Kronos evidence preserved in gitignored owner-only research dir (previous report).
