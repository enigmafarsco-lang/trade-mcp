# XAUUSD gross-scenario formula cross-check

The actual saved metadata was reported as `cfd leverage` with USD profit
currency. The earlier research scenario used `(close - open) * contract_size *
lots` for a long, and the sign-reversed price difference for a short. Bid/ask
was used for simulated entry and exit. It did not simulate broker margin or
convert currencies, and the saved snapshot did not independently bind its
symbol or prove historical contract rules.

MetaQuotes' MQL5 symbol-properties documentation lists the CFD leverage **profit**
formula as `(close_price - open_price) * Contract_Size * Lots`. Leverage appears
in its margin formula, not in that listed gross profit formula:
https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants

Thus the chosen generic gross linear formula is consistent with the documented
CFD leverage formula. This source-only cross-check is **not** an actual broker
OrderCalcProfit comparison or broker execution; it does not prove that the
snapshot, currency conversion, historical specification, simulated fills or
account margin are correct. Commission/swap and net PnL remain unknown. It does
not supply additional market data or establish strategy profitability.

No order, MT5 request, retry, account read, live change, cloud model call or
raw data disclosure was made for this cross-check. The prior one-window model
result remains -6.24 USD gross, not a real loss or a net backtest.
