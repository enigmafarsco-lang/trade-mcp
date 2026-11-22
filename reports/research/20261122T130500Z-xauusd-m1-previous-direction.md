# بررسی آفلاین假癢ِ جهت دقیقهٔ قبل برای سیگنال M1 (اجرای یک‌باره)

- شاخه: `arena/f81b6b27-trade-mcp`
- HEAD در زمان اجرا: `8f684993ffac34bb4c8a36ca0aebda35677ef531`
- دستور اجرا (یک‌بار): `.venv/bin/python scripts/xauusd_m1_previous_direction.py`
- تست‌های مصنوعی قبل از اجرا: `tests/test_m1_previous_context_replay.py tests/test_xauusd_m1_previous_direction.py` → **۳۲ تست، RC=0**
- درخواست تاریخچه جدید: **۰**؛ تماس شبکه: **۰**؛ فراخوانی مدل استراتژی: **۰**؛ سفارش: **۰**
- trading OFF، cloud budget revoked. خروجی: `logs/research-inputs/xauusd-m1-previous-direction-v1/summary.json` (فقط محلی، owner-only، `0700`/`.0600`، از قبل نبود، overwrite نشد).

## نتیجه کلی
- `status=completed_all_paired_comparisons`؛ **۱۲/۱۲** مقایسه جفتی انجام شده؛ **۰ شکست**.
- **۱۲/۱۲** بررسی baseline تطابق یافت.
- `commission/fees/swap/net_pnl = null` (نامعلوم، نه صفر).
- `profitability_proven=false`. این بررسی بازنگرانه است، نه تست کور و نه اثبات سودآوری.

## خلاصهٔ هر نمونه/مدل (parikh: Pav = فقط’avant 있었던’؛ Pda =avantو جهت-matched)
نمایش: ورود / سود/ضرر / gross_total_mark (مدل) / باز در پایان / لاچ

| نمونه | مدل | Pav_entries | Pavprofit/loss | Pav_mark | Pda_entries | Pdaprofit/loss | Pda_mark | Pav−Pda (ناخالص کنترل) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| additional_sample | original_3_changes | 5 | 1/4 | -6.08 | 5 | 1/4 | -6.08 | 0.0 |
| additional_sample | 2_changes_spread_cap_1_00 | 10 | 3/7 | -8.54 | 6 | 2/4 | -4.02 | **+4.52** |
| w1 | original_3_changes | 4 | 2/2 | 0.0 | 3 | 1/2 | -2.12 | **-2.12** |
| w1 | 2_changes_spread_cap_1_00 | 3 | 2/1 | +2.14 | 2 | 1/1 | +0.10 | **-2.04** |
| w2 | original_3_changes | 3 | 0/2 | -5.68 | 1 | 0/1 | -2.08 | **+3.60** |
| w2 | 2_changes_spread_cap_1_00 | 2 | 0/2 | -4.22 | 1 | 0/1 | -2.14 | **+2.08** |
| w3 | original_3_changes | 6 | 1/4 | -8.66 | 2 | 1/1 | -0.08 | **+8.58** |
| w3 | 2_changes_spread_cap_1_00 | 6 | 1/4 | -8.86 | 2 | 1/1 | -0.58 | **+8.28** |
| w4 | original_3_changes | 3 | 1/1 | -0.96 | 2 | 0/1 | -2.96 | **-2.00** |
| w4 | 2_changes_spread_cap_1_00 | 3 | 1/2 | -1.70 | 2 | 0/1 | -2.98 | **-1.28** |
| w5 | original_3_changes | 4 | 1/3 | -4.72 | 4 | 1/3 | -4.72 | 0.0 |
| w5 | 2_changes_spread_cap_1_00 | 5 | 1/4 | -5.92 | 4 | 1/3 | -4.26 | **+1.66** |

## محدودیت‌ها (آلثاً)
- فرضیه و انتخاب دو مدل بعد از مشاهده نتایج؛ نه تست کور و نه دادهٔ واقعاً ندیده؛ نه سود خالص و نه مجوز معامله.
- دو quote و eligibilidad دقیقهٔ قبلی، پوشش کامل تیک یا open/close واقعی کندل را اثبات نمی‌کنند؛ UTC و سلامت feed تأیید نشده.
- هر پنجره/مدل با latch فرضی مستقل شروع می‌شود؛ مسیر معاملات بعدی می‌تواند متفاوت باشد.
- تفاوت مثبت ممکن است صرفاً ضرر کمتر یا عدم ورود باشد؛ **اثبات بهبود واقعی نیست**. صفر بدون معامله را موفقیت معرفی نکن.
- diaphragmatic: spread مدل‌شده شامل است؛ کارمزد، سواپ، سایر هزینه‌ها و خالص واقعی ناماند.

## ثبت/پوش
- گزارش پاک‌سازی‌شده: `reports/research/20261122T130500Z-xauusd-m1-previous-direction.{json,md}`
- خام داده‌های تیک/اقتصاد commit نشده‌اند.
