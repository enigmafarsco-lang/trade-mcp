# بازپخش آفلاین آرشیوهای tick موجود XAUUSD

- Branch: `arena/f81b6b27-trade-mcp`
- HEAD at execution: `b628e02`
- Command executed (once): `.venv/bin/python scripts/replay_existing_xauusd_archives.py`
- New MT5/network calls: **0** (`history_calls_made=0`). Trading OFF, cloud budget revoked.

## نتیجه واقعی
- `status=existing_samples_replayed`
- فایل‌های آرشیو بررسی‌شده: **2**؛ حذف‌شده: `unverified_symbol_period_or_success=1`
- نمونه‌های قابل‌استفادهٔ غیرهمپوشان: **1**؛ تعداد تاریخ‌های متمایز برچسب بروکر: **1**

### پنجره 1 (فرضی مستقل، نه پیوسته)
- ورودی/بازپخش: `input_rows=1015`, `replayed_rows=614`, `interior_minutes_seen=3`
- سیگنال: `sell=17, buy=16`
- سناریوی gross نقدی (0.02 lot، -2/+2، latch سه ضرر، next-quote فرضی): `simulated_entries=5`, `cash_loss_exits=4`, `cash_profit_exits=1`, `closed_wins=1, `closed_losses=4`, `gross_realized_usd_model=-6.239999999999782`, `gross_open_mark_usd_model=null`, `gross_total_mark_usd_model=-6.239999999999782`, `simulated_three_consecutive_loss_latch=true`
- هزینه/خالص: `commission=null`, `swap=null`, `net_pnl=null`
- پرچم‌ها: `independent_hypothetical_windows_not_continuous=true`, `complete_tick_coverage_verified=false`, `utc_verified=false`, `profitability_proven=false`, `execution_authorized=false`

هیچ نمونهٔ واجد شرایط جدیدی یافت نشد؛ این بک‌تست پیوسته/اثبات سودآوری نیست. خروجی خام owner-only gitignored (`logs/research-inputs/xauusd-existing-archives-inventory-v1.json`) کامیت نشد.
