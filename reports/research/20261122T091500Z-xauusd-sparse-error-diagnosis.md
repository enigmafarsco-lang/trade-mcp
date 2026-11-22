# تشخیص آفلاین خطای sparse screen v1 (فقط ساختاری/غیرتأییدی)

- Branch: `arena/f81b6b27-trade-mcp`
- HEAD at execution: `38398cc`
- Command executed (once): `.venv/bin/python scripts/inspect_xauusd_sparse_error.py`
- New MT5/network request: **0**. Screen retry: **no**. Directory/output deleted: **no**.

## پرچم‌های ساختاری واقعی (از پاسخ ذخیره‌شدهٔ eval-1)
- `is_error_true=false`
- `structured_content_present=false`
- `text_content_present=true`
- `structured_payload_type="dict"`
- `payload_ok_false=true`
- `payload_error_truthy=false`
- `history_calls_made=0`
- `raw_provider_text_disclosed=false`

## Phrase hints (صرفاً تطبیق تحت‌اللفظی، تأییدنشده)
- `phrase_hints_unverified=[]` (خالی؛ به معنی اثبات/رد علت نیست)

## محدودیت
- `root_cause_verified=false`. این خروجی **علت ریشه‌ای تأییدشده نیست**؛ فقط پرچم‌های ساختاری و hintهای تحت‌اللفظی غیرتأییدی است. هیچ payload/raw قیمت/حساب/credential افشا یا کامیت نشد.
