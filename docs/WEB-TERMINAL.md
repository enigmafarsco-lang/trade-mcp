# راه‌اندازی نسخهٔ یکپارچهٔ ترمینال وب

این نسخه یک ترمینال تحلیلی و آموزشی کنار MT5 است؛ نه کلون کامل MT5.
خود MT5 باید روشن بماند. افزونه‌های EX5 در مرورگر اجرا نمی‌شوند. کتابخانهٔ
اندیکاتورها داخلی است، نه مجموعهٔ همهٔ افزونه‌های رایگان موجود در اینترنت.

## نصب و اجرا

```bash
git pull origin arena/f81b6b27-trade-mcp
.venv/bin/python -m pip install -e .
read -rsp 'terminal key: ' TK; echo
export TRADE_MCP_MCP_TOKEN="$TK"; unset TK
export TRADE_MCP_BACKEND=native
export TRADE_MCP_MCP_URL=http://127.0.0.1:22346/mcp
export TRADE_MCP_TRAINING_BUDGET=100
export TRADE_MCP_ALLOW_TRADING=false
.venv/bin/trade-mcp doctor --wait 0
.venv/bin/trade-mcp console
```

در کروم http://127.0.0.1:8790 را باز کنید و برای حذف نسخهٔ کش‌شده
Ctrl+Shift+R بزنید. کلید را در چت، کد یا بلوک JSON این صفحه وارد نکنید.
سرور کنسول را بدون احراز هویت روی شبکهٔ عمومی منتشر نکنید. این اجرا
به‌صورت پیش‌فرض به loopback محدود است.

## قابلیت‌های آماده

- صفحهٔ پیش‌فرض چندنموداری با حداکثر چهار نمودار مستقل؛ انتخاب نماد و
  تایم‌فریم M1/M5/M15/M30/H1/H4/D1/W1؛ چینش یک یا دو ستونه؛
- مارکت‌واچ با جست‌وجو در حداکثر ۲۰۰ نماد دریافتی؛ قیمت‌های پاسخ بروکر
  و تکمیل قیمت‌های مفقود برای ۳۰ ردیف اول؛ نه کل فهرست تمام نمادهای بروکر؛
- کندل، زوم با چرخ، حرکت روی تاریخچه با کشیدن، نشانگر OHLC و حجم؛
- EMA20/50/200 و Bollinger روی کندل؛ RSI/MACD/ATR/ADX/Stochastic در
  پنجره‌های پایین هر نمودار؛ انتخاب مستقل اندیکاتورها برای هر نمودار؛
- خطوط ورود، SL و TP پوزیشن‌های واقعی همان نماد، در صورت قرارداشتن داخل
  محدودهٔ قیمت قابل مشاهده؛ جدول پوزیشن‌ها و سفارش‌های باز و وضعیت حساب؛
- ابزار خط افقی، خط روند با دو کلیک، حذف آخرین خط و بازنشانی چیدمان؛
  خطوط سفارش نیستند و قابلیت drag-to-trade ندارند؛
- ذخیرهٔ نمادها، تایم‌فریم‌ها، انتخاب اندیکاتورها، خطوط و ستون‌بندی در
  localStorage همین مرورگر، بدون ذخیرهٔ کلید؛ خطوط به نماد/تایم‌فریم مقیدند؛
- قیمت نمادهای باز با نرخ هدف قابل انتخاب ۲۵۰/۵۰۰/۱۰۰۰/۲۰۰۰ میلی‌ثانیه؛
  پیش‌فرض ۵۰۰ و روشن. نمودار/اندیکاتورها با هدف ۲ ثانیه، حساب و پوزیشن‌ها
  هر ۱۰ ثانیه و مارکت‌واچ عمومی هر ۳۰ ثانیه درخواست می‌شوند. هنگام
  پنهان‌شدن صفحه یا رفتن به تب دیگر polling خودکار متوقف می‌شود؛
- درخواست قیمت هم‌پوشان نمی‌شود. نرخ واقعی تابع زمان پاسخ MCP و بار
  ترمینال است؛ زمان پاسخ و قطع/بازیابی نمایش داده می‌شوند. در خطا مکث
  افزایشی اعمال می‌شود و قیمت قبلی به‌عنوان پاسخ تازه برگردانده نمی‌شود؛
- تحلیل چندتایم‌فریمی و تعیین حجم با سقف مشترک بودجهٔ تمرینی؛ حذف آخرین
  کندل در حال تشکیل از قواعد تحلیل؛ امتیاز تحلیل، احتمال آماری برد نیست؛
- تب آموزش برای بودجهٔ مستقل، محاسبات پوینت/ریسک/هدف به ارز حساب و
  بک‌تست مقدماتی. جزئیات: [DEMO-TRAINING.md](DEMO-TRAINING.md)؛
- تب‌های ایجنت، اخبار، ژورنال و تنظیمات قبلی همچنان در دسترس‌اند.

### شروع پیشنهادی روی دمو

۱. اول در صفحهٔ ترمینال سرور و نوع حساب را بررسی کنید. MetaQuotes-Demo
را حساب آلپاری در نظر نگیرید. مشخصات نماد، مارجین و ارزش تیک باید از
بروکر متصل بیایند؛ اهرم فرضی ۱:۱۰۰۰ جایگزین آن‌ها نیست.

۲. XAUUSD را در M15 و H1 باز کنید. اندیکاتورهای دلخواه را انتخاب کنید.
روی نمودار کلیک کنید تا فعال شود، سپس «تحلیل و سناریو» را بزنید.

۳. «محاسبهٔ ریسک و بک‌تست» نماد نمودار فعال را به تب آموزش منتقل می‌کند.
بودجهٔ ۱۰۰ و ریسک ۱٪ را آزمایش کنید. در حساب USD یعنی سقف زیان تخمینی
۱ دلار؛ اگر حداقل لات ناسازگار بود، ورود رد می‌شود. «هدف سود به ارز
حساب» با «یک دلار تغییر قیمت طلا» فرق دارد.

۴. هزینه‌ها، ارزش تیک و حداقل حجم را با مشخصات خود ترمینال بررسی کنید.
این نسخه هنوز ارزش تیک را با تابع محاسبهٔ سود مستقل MT5 تأیید نمی‌کند؛
اعداد سود/زیان تخمینی‌اند. ریسک گپ، لغزش و هزینهٔ واقعی وجود دارد.

## provider اختصاصی وب

تحلیل بدون مدل زبانی نیز کار می‌کند. مسیر وب فقط یک provider مستقیم
سازگار با OpenAI را می‌پذیرد و از مسیر چت توسعهٔ OpenCode جدا است. قبل از
اجرای کنسول این‌ها را در محیط همان کنسول تنظیم کنید:

```bash
export TRADE_MCP_LLM_BASE_URL="https://provider.example/v1"
export TRADE_MCP_LLM_MODEL="model-id"
# کلید را مخفیانه وارد کنید؛ آن را در چت یا فایل پروژه نگذارید:
read -rsp 'model provider key: ' MK; echo
export TRADE_MCP_LLM_API_KEY="$MK"; unset MK
export TRADE_MCP_LLM_BILLING_PROJECT="web-analysis"   # فقط metadata
export TRADE_MCP_LLM_USAGE_CONVENTION="openai_inclusive"  # یا additive / unknown
```

`TRADE_MCP_OPENCODE_URL` و کلیدهای چت توسعه برای ابزارهای MCP و همان چت
باقی می‌مانند؛ مسیر وب هرگز به آن‌ها fallback نمی‌کند. اگر
`LLM_BASE_URL` یا `LLM_MODEL` خالی باشد، دکمه‌های مدل با خطای روشن
غیرفعال/رد می‌شوند. URL باید HTTPS باشد؛ HTTP فقط برای loopback مجاز است.
کاربر/رمز داخل URL، query و fragment پذیرفته نمی‌شوند.

برچسب `TRADE_MCP_LLM_BILLING_PROJECT` فقط برای تفکیک گزارشهاست و ثابت
نمی‌کند صورت‌حساب provider جدا است. مسئولیت محدودکردن کلید/پروژه در پنل
provider همچنان با شماست.

### بودجهٔ آزمایش مشترک (۳۰۰ ثانیه / حداکثر ۵ تلاش)

پیش از هر درخواست واقعی، بودجه را در پنل ایجنت صریحاً فعال کنید. این بودجه
بین تست مدل، اجرای ایجنت، توضیح مدل و چرخهٔ صفحه مشترک است:

- پنجرهٔ ۳۰۰ ثانیه‌ای با **نخستین درخواست مجاز** آغاز می‌شود، نه با کلیک
  فعال‌سازی؛ پیش از آن هیچ تلاشی مصرف نمی‌شود.
- حداکثر پنج تلاش واقعی در همان پنجره مجاز است. تلاش ششم با HTTP 429 و
  دلیل `test_budget_exhausted` رد می‌شود.
- رفرش صفحه بودجه را بازنشانی نمی‌کند. کنسول دوباره هم پنجرهٔ قبلی را از
  SQLite می‌خواند و خودکار پنجرهٔ تازه نمی‌سازد.
- فعال‌سازی دوباره در حین پنجرهٔ فعال، پنجره را بازنشانی نمی‌کند.
  پس از پایان/مصرف/لغو، فقط فعال‌سازی صریح پنجرهٔ جدید می‌سازد.
- درخواستی که پیش از پایان مهلت مجاز شده ممکن است پس از مهلت تمام شود؛
  نتیجه‌اش ثبت می‌شود. تلاش خودکار یا retry وجود ندارد.
- `Retry-After`/`retry_after_ms` معتبر provider به‌صورت cooldown ذخیره
  می‌شود و تا پایان آن درخواست جدید رد می‌شود. cooldown خودش تلاش مصرف
  نمی‌کند.

### دفتر مصرف provider وب

فقط تلاش‌های واقعی provider اختصاصی وب در جدول `model_usage` ژورنال ثبت
می‌شوند: تست مدل، اجرای ایجنت، توضیح مدل و چرخه. این دفتر با آمار
`session_v2` چت OpenCode جمع نمی‌شود.

- متن prompt، پاسخ مدل، کلید و مشخصات حساب ذخیره نمی‌شوند.
- هر مقدار نامعلوم `NULL` می‌ماند و در خلاصه به‌عنوان «نامشخص» شمرده
  می‌شود، نه صفر.
- `cached_input_tokens` جدا از input و `reasoning_tokens` جدا از output
  ذخیره می‌شوند؛ جمع دوباره ساخته نمی‌شود. قرارداد هر ردیف
  (`openai_inclusive`، `additive` یا `unknown`) نیز ثبت می‌شود.
- هزینه فقط وقتی ثبت می‌شود که provider خودش گزارش کند
  (`cost_source=provider`)؛ برآورد محلی جای آن را نمی‌گیرد.
- هزینه‌های ارزهای مختلف جدا نمایش داده می‌شوند و هیچ‌گاه با هم جمع
  نمی‌شوند. ردیف بدون واحد پول در سبد «واحد نامشخص» می‌ماند.

با کلیک روی دکمهٔ توضیح و تأیید، نماد، قیمت، سناریوی عددی، سوگیری و
هشدارها به سرویس تنظیم‌شده فرستاده می‌شوند. نام، شمارهٔ حساب و کلید ارسال
نمی‌شوند. این قابلیت تفسیر دادهٔ عددی چارت است، نه دریافت تصویر چارت.
توضیح مدل نباید مبنای عددسازی یا وعدهٔ سود باشد و هیچ معامله‌ای اجرا
نمی‌کند. اگر سرویس تنظیم نشده باشد یا بودجه فعال نباشد، پیام روشن نشان
داده می‌شود؛ شکست سرویس جای توضیح واقعی با متن ساختگی پر نمی‌شود.

## محدودیت‌ها و مرز نسخه

هنوز برابری کامل با MT5، DOM/order book، تستر تیک واقعی، MQL5/EX5، بازار
افزونه‌های شخص ثالث، اعلان تضمینی سیگنال و همهٔ ابزارهای رسم MT5 وجود
ندارند. اندیکاتورها پارامترهای پیش‌فرض ثابت دارند. طول نمودار ۳۰۰ کندل
است؛ بک‌تست حداکثر ۱۵۰۰ کندل و مدل هزینهٔ ساده دارد. پنل دستی Market،
تغییر SL/TP و بستن کامل دمو اکنون وجود دارد؛ با `ALLOW_TRADING=false`
فقط پیش‌نمایش است. سفارش‌های Pending/Limit/Stop و بستن جزئی در پنل جدید
پیاده نشده‌اند. شرح کنترل‌های اجرا در بخش ترید دستی پایین آمده است.

## بررسی توسعه

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/python -m pytest -q
node --check src/trade_mcp/web/terminal.js
```

تست مرورگر اختیاری `scripts/browser_smoke.cjs` با playwright-core و یک
Chromium نصب‌شده اجرا می‌شود. وابستگی مرورگر بخشی از برنامهٔ کاربر نیست.
سرور mock را جداگانه با `--mock console --console-port 8792 --no-browser`
اجرا کنید؛ سپس CHROMIUM_PATH و در صورت نیاز NODE_PATH را برای اسکریپت
تنظیم کنید. تست شامل چندنموداری، تایم‌فریم، رسم و ماندگاری، تب‌ها،
اندیکاتور، آموزش و نبود اسکرول افقی ناخواسته در عرض موبایل است.


## مسیر قیمت سریع

`GET /api/quotes?symbols=XAUUSD,EURUSD` تا چهار نماد یکتا می‌گیرد؛ کش
اشتراکی ۱۰۰ میلی‌ثانیه‌ای و سقف ۳۲ ورودی برای کاهش درخواست‌های تکراری
چند مرورگر دارد. این مسیر فقط quote می‌خواند، نه تاریخچه و حساب. پاسخ
شامل received_at_ms، sequence، elapsed_ms و errors هر نماد است.
زمان دریافت، زمان تیک بروکر نیست؛ برای native فیلد time_source=received
این تفاوت را مشخص می‌کند. در تعطیلی بازار ممکن است قیمت تغییر نکند حتی
اگر نمونه‌برداری موفق باشد. دریافت تک‌تک تیک‌های بازار تضمین نمی‌شود.

`GET /api/chart` بار اول ۳۰۰ کندل می‌گیرد و سپس سه کندل انتهایی را از
ترمینال ادغام می‌کند. هر ۶۰ ثانیه بازخوانی کامل انجام می‌شود. اندیکاتورها
روی دادهٔ ادغام‌شده دوباره محاسبه می‌شوند؛ high/low/volume یا کندل جدید
از تعداد دفعات polling ساخته نمی‌شود. خطوط BID/ASK جداگانه با quote
سریع حرکت می‌کنند و رنگ قیمت با تغییر جهت عوض می‌شود. در نمای تاریخچه
خط قیمت زنده مقیاس گذشته را تغییر نمی‌دهد.

`GET /api/account-state` مسیر جداگانهٔ حساب، پوزیشن و سفارش است. نرخ
انتخابی و روشن/خاموش بودن حالت زنده در چیدمان مرورگر ذخیره می‌شوند.
اگر دریافت قیمت بیش از پنج ثانیه عقب بماند، هشدار قدیمی‌بودن داده نشان
داده می‌شود. هیچ‌کدام از این مسیرها معامله‌ای اجرا نمی‌کنند.


## صحت تاریخچه و مقایسه با MT5

اشکال بازه/limit تاریخچه اصلاح شده: ابزار ممکن است اولین N رکوردِ بازه
را برگرداند، بنابراین سقف پاسخ اکنون کل بازه را پوشش می‌دهد و سپس N
کندل آخر انتخاب می‌شود. برای count=3، محدودیت قبلی ۹ رکورد در بازه‌ای
حدود ۲۴ کندل می‌توانست نمودار را در گذشته نگه دارد. تاریخ‌ها مرتب و
تکراری‌ها حذف می‌شوند. اختلاف ساعت UTC با ساعت آخرین سرور معاملاتی
برای درخواست تاریخچه (با کش ۶۰ثانیه‌ای) لحاظ می‌شود. زمان بدون timezone
در محور به‌صورت «feed clock» نمایش داده می‌شود، نه UTC قطعی.

مقیاس کندل دوباره فقط از OHLC محاسبه می‌شود؛ Bid/Ask بیرون محدوده،
تاریخچه را فشرده نمی‌کند. بعد از دریافت نسخهٔ جدید سرور را دوباره اجرا
کنید. در بخش «پنجرهٔ داده و مقایسهٔ کندل»، دکمهٔ بازخوانی مستقیم را بزنید
و پنج ردیف OHLC را با Data Window خود MT5 روی همان حساب/نماد/تایم‌فریم
مقایسه کنید. ردیف آخر می‌تواند باز باشد و بین دو برداشت تغییر کند؛ اول
کندل‌های بسته را مقایسه کنید. هنوز تطبیق زنده روی ماشین کاربر تأیید نشده
و اختلاف timezone، نماد دارای پسوند یا خود ابزار MCP باید در صورت باقی
ماندن اختلاف بررسی شود.

## ترید دستی وب، فقط دمو

پنل شامل Buy/Sell مارکت با SL اجباری، حجم مستقیم یا تعیین حجم از ریسک،
پیش‌نمایش مارجین و خطاهای ریسک، تغییر SL/TP و بستن کامل تیکت است.
خالی‌گذاشتن SL/TP در تغییر پوزیشن، مقدار قبلی را حفظ می‌کند. حذف SL
محافظتی و افزایش فاصلهٔ آن در مسیر وب پشتیبانی نمی‌شود. تغییر فرم بعد
از پیش‌نمایش، تأیید آن را غیرفعال می‌کند. هیچ سفارشی هنگام ساخت پیش‌نمایش
اجرا نمی‌شود و با مسدودبودن ریسک، دکمهٔ تأیید فعال نمی‌شود.

برای اجرای واقعی **روی دمو** باید خود کاربر آگاهانه کنسول را با
`TRADE_MCP_ALLOW_TRADING=true` راه‌اندازی کند و در MT5 نیز مجوز معاملهٔ
MCP فعال باشد؛ این تغییر خودکار انجام نشده است. هم هنگام پیش‌نمایش و هم
تأیید، نوع حساب تازه خوانده می‌شود؛ حساب real در API وب رد می‌شود. تأیید
شرایط فعلی را دوباره بررسی می‌کند و رد سفارش توسط بروکر به‌عنوان اجرای
موفق ثبت نمی‌شود. پیشنهاد منقضی را باید دوباره ساخت.

این کنترل‌ها به معنی تضمین زیان ثابت نیستند؛ گپ، لغزش، هزینه و محدودیت
ابزار MCP همچنان برقرارند. محیط پیش‌نمایش توسعه صرفاً mock است؛ تست
اجرای پنل در تست‌ها روی mock انجام شده، نه ارسال سفارش به حساب کاربر.

## پنجرهٔ سفارش و مارکت‌واچ منتخب

کلیک روی نماد مارکت‌واچ اکنون پنجرهٔ سفارش را باز می‌کند؛ دکمهٔ «چارت»
برای تغییر نمودار جداست. مشخصات حجم/گام و Bid/Ask و وضعیت فعال‌بودن اجرا
از ترمینال خوانده می‌شوند. فهرست پیش‌فرض فقط نمادهای visible/selected
است؛ گزینهٔ «همهٔ نمادها» فهرست مخفی را نیز نشان می‌دهد و دکمهٔ افزودن
به مارکت‌واچ از symbol_select همان ترمینال استفاده می‌کند. فعال/منتخب‌بودن
به معنی بازبودن جلسهٔ معاملاتی یا داشتن مجوز معامله نیست.

وقتی SL خالی و «SL خودکار» روشن باشد، از بودجهٔ مشترک، درصد ریسک و حجم
انتخابی محاسبه می‌شود؛ اگر حجم خالی باشد حداقل حجم بروکر استفاده می‌شود.
این حد از تحلیل تکنیکال نیست و تضمین زیان ثابت ندارد. اگر با حداقل فاصلهٔ
بروکر سازگار نباشد، رد می‌شود؛ SL بی‌صدا حذف یا ریسک افزایش داده نمی‌شود.
با خاموش‌بودن این گزینه SL دستی لازم است. در پیش‌نمایش منبع SL مشخص است.

pending یعنی پیشنهاد داخلی، نه سفارش ثبت‌شده در MT5. پس از تأیید صریح
و موفقیت واقعی پاسخ بروکر، پوزیشن/سفارش در همان ترمینال native ظاهر
می‌شود و وب حساب/پوزیشن‌ها را بی‌درنگ دوباره می‌خواند. به جدول پوزیشن و
نتیجهٔ بروکر توجه کنید؛ یک پیام پیش‌نمایش به معنی اجرا نیست. با
ALLOW_TRADING=false هیچ ثبت سفارشی صورت نمی‌گیرد. برای اجرای دمو باید
خود کاربر پس از بررسی سرور/نوع حساب، کنسول را با true راه‌اندازی کند؛
کلید متعلق به همان terminal و مجوز MCP trading نیز لازم است. حساب real
همچنان در تأیید وب رد می‌شود. اجرای native روی حساب کاربر در محیط توسعه
امتحان نشده است؛ تست اجرای سفارش فقط روی mock است.

دکمه‌های BUY/SELL همراه قیمت روی نوار هر نمودار هم وجود دارند؛ کلیک،
پنجرهٔ سفارش همان نماد و جهت را باز می‌کند، نه معاملهٔ یک‌کلیکی. پنجره
مجوز برنامه و mcp_trade_allowed ترمینال را جدا نشان می‌دهد. خطای
`trading is not permitted` از خود MT5 است: تنظیمات Tools → Options → AI Assistant → Trading را بررسی کنید؛
در تب MCP تصویر این نسخه فقط اتصال، آدرس و کلید دیده می‌شود؛ ALLOW_TRADING برنامه جای آن
را نمی‌گیرد و روشن‌بودن Algo Trading نیز به‌تنهایی اثبات مجوز MCP نیست.
بعد از اصلاح مجوز، پیشنهاد جدید بسازید؛ درخواست شکست‌خورده خودکار تکرار
نمی‌شود. این مجوز توسط برنامه دور زده یا از راه دور فعال نمی‌شود.

مرجع تنظیم Trading در تب AI Assistant:
https://www.metatrader5.com/en/terminal/help/mcp_and_ai/configuration#ai_assistant_settings

## اتصال با کلیپ‌بورد، بدون واردکردن دستی کلید

در Ubuntu/X11 می‌توان xclip را با `sudo apt install xclip` نصب کرد.
سپس `python scripts/mt5_mcp_connect.py --console` را با پایتون venv اجرا
کنید. برنامه منتظر می‌ماند: اکنون در MT5 تب MCP دکمهٔ Copy را بزنید و
در ترمینال فقط Enter بزنید، نه Paste. اگر اول Copy کنید و بعد فرمان
اجرای اسکریپت را از مرورگر کپی کنید، کلیپ‌بورد با فرمان جایگزین می‌شود؛
به همین دلیل Copy باید پس از ظاهرشدن پیام انتظار برنامه انجام شود.

برنامه از بلوک JSON/TOML فقط terminal را انتخاب می‌کند، نه metaeditor.
کلید چاپ یا در فایل ذخیره نمی‌شود و از متغیرهای قدیمی شل استفاده نمی‌شود.
doctor با همین URL/key اجرا می‌شود؛ فقط اگر موفق شد کنسول را با معاملهٔ
غیرفعال راه‌اندازی می‌کند. در صورت 401 با وجود خواندن تنظیمات ذخیره‌شده،
اختلاف کلید فعال سرور با تنظیمات هنوز پابرجاست؛ این ابزار 401 را دور نمی‌زند.

### فعال‌کردن صریح دمو در ابزار کلیپ‌بورد

`python scripts/mt5_mcp_connect.py --console --enable-demo-trading` پس از
موفقیت doctor، نوع حساب را با اتصال تازه بررسی می‌کند. تنها اگر demo
باشد کنسول را با ALLOW_TRADING=true اجرا می‌کند. حالت پیش‌فرض بدون این
پرچم همچنان فقط پیش‌نمایش است. بعد از شروع برنامه، Copy را در MT5 بزنید
و فقط Enter بزنید؛ تنظیمات را در ترمینال پیست نکنید. پیشنهادهای قبلی
مسدود/منقضی را باید دوباره ساخت. این پرچم سفارش خودکار ارسال نمی‌کند.

پس از پیش‌نمایش، مقادیر SL/TP و حجم به کادرهای فرم برمی‌گردند؛ برای حجم
صریح نیز زیان تخمینی و درصد بودجه نمایش داده می‌شود. دکمهٔ «پرکردن فرم
از تحلیل چارت» سناریوی قاعده‌محور قابل ورود و حجم معتبر را منتقل می‌کند؛
اگر سناریو یا بودجه معتبر نباشد، چیزی به‌عنوان ورود قطعی پر نمی‌کند.
پرکردن فرم، سفارش‌گذاری نیست و دوباره به پیش‌نمایش و تأیید نیاز دارد.
مدل زبانی با این دکمه فعال نمی‌شود و نیازمند تنظیم جداگانهٔ سرویس است.

### انتخاب پوزیشن و راهنمای تغییر/بستن

در پنجرهٔ سفارش راهنمای بازشوندهٔ فارسی اضافه شده است. تغییر عملیات
به «بستن کامل» یا «تغییر SL/TP» فهرست پوزیشن‌ها را تازه می‌خواند؛ انتخاب
پوزیشن، تیکت/نماد/جهت/حجم/SL/TP فعلی را پر می‌کند. در بستن، فیلدهای
جهت ورود، ریسک و SL/TP پنهان و حجم فقط‌خواندنی است. پیش‌نمایش بستن،
حجم واقعی close_volume را نشان می‌دهد؛ صفر بودن SL/TP فعلی به معنی
نداشتن آن در پوزیشن است، نه نیاز به جعل عدد جدید. دکمهٔ بازخوانی نیز
در دسترس است. پس از تأیید بستن، فهرست دوباره خوانده و وجود یا نبود
همان تیکت در پاسخ تازه گزارش می‌شود؛ اگر هنوز دیده شود، پیام بررسی
MT5 نمایش داده می‌شود و درخواست خودکار تکرار نمی‌شود.

این مسیر با بازکردن، پیش‌نمایش و تأیید بستن در mock از طریق HTTP و با
انتخاب پوزیشن و پرشدن فرم در مرورگر آزمایش شده است. اگر روی ترمینال
واقعی خطای بستن رخ دهد، نتیجهٔ JSON همان عملیات (بدون کلید) لازم است؛
تصویر پنجرهٔ سفارش به‌تنهایی علت خطای بروکر یا ابزار MCP را مشخص نمی‌کند.

### Interactive model setup

Run `scripts/mt5_mcp_connect.py --console --enable-demo-trading --setup-llm`
with the virtualenv Python. Enter your OpenAI-compatible provider base URL
(including `/v1` if required), its exact model ID, and the model API key at the
hidden prompt. This key is separate from the MT5 MCP key. A single generic chat
request tests the provider before MT5 clipboard setup; provider charges may apply.
Remote providers require HTTPS. Local HTTP model servers are supported.
Failure or an empty response aborts startup; no provider errors or keys are printed.
The configuration is process-only and must be entered again on restart, unless
configured separately through the existing environment mechanism. A successful
chat test does not guarantee a valid agent JSON response or permission to trade.
Agents still need to be created/run through the Agents tab, and sizing limits remain
in force. Do not send API keys in chat or paste the MT5 Copy block into Bash.

## Visible agent workspace and model-provider failures

The terminal's top panel now shows saved agent activity without requiring the user
to read OpenCode chat logs. `GET /api/workspace-status` is a read-only snapshot of
configuration, the last explicit model test, saved agent reports (including runs
from another MCP process sharing the journal), and recent proposals. Polling this
endpoint never calls a model, runs an agent, places an order or modifies proposal
status. The browser polls every five seconds and refreshes across desktop/mobile.

- The explicit model-test button sends one generic request; charges may apply and
  one of the five shared test-budget attempts is consumed.
- The workspace shows the dedicated web provider separately from the development
  OpenCode chat. Web requests never use OpenCode credentials or totals, and no
  silent fallback is attempted when the web provider is missing or fails.
- Provider failures are classified into fixed safe codes/messages: billing required,
  quota exhausted, rate limited, authentication failed, endpoint unavailable, or
  connection failed. Provider response bodies and credentials are never returned.
- A model failure remains `wait`, `used_llm=false`, with no silent rules fallback.
  Saved reports expose the safe model error so it is visible in the web panel.
- Technical rule-based analysis is separately labelled, not presented as a model
  decision or executed order. Market/configuration warnings remain visible.
- The workspace's agent-run button always requests one advisory run, even when the
  saved agent's mode is auto. It does not modify the saved mode or start a scheduler.
- Valid proposals show entry, volume, SL/TP, estimated stop loss/target profit before
  costs and margin when available. The agent consumes the executor's actual preview
  rather than assuming a nonexistent payload field. Blocked proposals do not proceed
  to auto confirmation; agent lot ceilings use the actual preview volume.
- Old pending proposals past their TTL are displayed as expired with confirmation
  disabled. This is a view, not automatic cancellation or rewriting of history.
- Confirmation is a separate deliberate click and browser confirmation. Existing
  executor TTL, price drift, validation and trading-permission gates remain in force.
- Structured saved reports are no longer sliced into invalid JSON at 8000 characters.

A successful desktop connection is not evidence of web-provider entitlement. HTTP
402, provider credit failures and quota failures cannot be fixed or bypassed by
this panel. No alternate model is probed or selected automatically, and no billing
action, quota workaround, allowlist change or risk increase is performed. The
development OpenCode chat remains a separate integration; its plan or entitlement
does not make the web provider available. The web path uses one direct
OpenAI-compatible endpoint and fails closed when it is missing.

Optional browser regression checks (mock console only): `scripts/browser_smoke.cjs`
and `scripts/workspace_smoke.cjs`. The latter checks saved-report visibility,
classified billing errors, absence of automatic model/agent/confirmation requests,
expired proposal controls, text rendering and mobile layout.

## Page-owned advisory analysis and explicit model selection

The chart is above the analysis workspace. The web model ID is separate from the
development OpenCode chat selection. The provider base URL, API key, billing label
and usage convention come only from the console environment; only the model ID can
be changed for this console run from the workspace panel (`/api/model-config`).
Saving a model changes this process only, persists nothing, sends no request and
invalidates the previous model test. Test the exact model before starting a cycle.
A catalogue entry, a desktop chat answer or an OpenCode session does not prove
web-provider entitlement.

The page cycle also requires an active test budget (300 seconds from the first
authorized attempt, at most five actual attempts shared with every other web model
route). A successful model test, the active budget and explicit consent are all
required before starting. The window is persisted in SQLite; refreshing the page
or restarting the console does not create another one. Provider cooldowns are
respected and no automatic retry is attempted.

Choose a saved agent, an interval (30–3600 seconds), explicitly accept model
request usage, then start the page cycle. It always runs in advisory mode, even
if the saved agent was automatic. It does not confirm proposals. A valid pending
signal for a selected symbol postpones another analysis. Hidden/closed pages
request a stop; a missing heartbeat expires the ownership lease after 20 seconds.
An already-running provider request may finish and save a proposal, but cannot
trade. Returning to the page does not restart the cycle automatically. Background
scheduler and page cycle are mutually exclusive. The market-open check is a
schedule estimate, not a broker session API; it can exclude non-Forex sessions.
Each provider attempt made by the cycle is recorded in the separate web usage
ledger; unknown token/cost values stay NULL.

Each completed agent run is stored separately in SQLite and shown as historical
analysis. Proposal history retains expired proposals without cancelling them.
Historical reports describe their run time; current proposal status is shown in
the proposal lists. Orders require a separate explicit confirmation while valid.
Confirmation rechecks current limits, margin and positions, then atomically claims
the proposal to prevent duplicate execution. Estimates are not guaranteed fills.

The web model base URL and key are operator environment settings, not launcher
model-selection flags. Development-chat flags such as `--opencode-model` configure
OpenCode for the chat and MCP tools; they do not configure or authorize the web
model. Do not buy credits or change billing merely to diagnose a model mismatch.

## Dedicated OpenCode profile and managed web console

`.opencode/agent/trade-analyst.md` provides a project-local primary agent profile
for OpenCode v2.2.7. Open the updated repository in OpenCode and select
`trade-analyst` after the client reloads its project configuration. It is not
installed into an unrelated project or an existing running chat automatically.
Its shell/edit permissions are denied; other tools require permission. The
profile explicitly prohibits trade confirmation and credentials inspection.
It supervises the application but is **not** a persistent session behind the
stateless web model API. Its model selection does not change the console model.

New MCP tools are `start_web_workspace`, `web_workspace_status`, and
`stop_owned_web_workspace`. The manager passes credentials only through its
child environment, forces trading disabled, verifies console identity before
reuse, and stops only a process it owns. It never adopts an unrelated console
or extracts credentials from old processes. The MCP process must first be
reloaded onto this code, already have a valid native-demo connection, and have
`TRADE_MCP_LLM_BASE_URL`/`_MODEL` for the dedicated web provider. OpenCode
credentials are removed from the child environment; the manager cannot
bootstrap missing model credentials and will not substitute the development
chat provider. Do not restart MT5 or expose keys just to refresh the console.
Configuration mismatches require operator review rather than killing an unknown
process.

All automated model/broker checks in the development sandbox use mocks or HTTP
fixtures. A successful local test suite is not proof of live provider access,
broker calculations, or successful order execution.

## Optional free-model failover (development OpenCode only)

The dedicated web provider has **no** automatic fallback: one configured model,
one endpoint, and a failure stops the request. The console's web routes never use
the OpenCode fallback policy. The legacy OpenCode adapter keeps its opt-in free
fallback for development-chat use, but that setting is not inherited by the web
console child (the manager removes the OpenCode environment variables).

For direct OpenCode adapter use, the free fallback is an opt-in checkbox in the
OpenCode path. It is off by default and invalidates the model test.
`TRADE_MCP_OPENCODE_FREE_FALLBACK=true` persists that policy through environment
configuration. The primary must be one of the 11 explicitly free IDs in the
operator-supplied catalogue; `big-pickle`, unknown IDs, and paid models are
rejected when this option is enabled. This is a static allowlist, not live
price/entitlement verification; recheck the provider catalogue if pricing or
availability changes.

At most three generation attempts occur in one OpenCode call. Alternate order is
GLM Flash Free (`low`), MiMo Flash Free (default), then DeepSeek Flash Free
(`low`), excluding the primary and taking only two alternates. The selected
primary and variant are preserved. Reports record every attempted model and the
model and variant that actually responded. A response is not necessarily a valid
trading decision: invalid JSON or invalid actions still fail closed without
rotation.

Only explicit `model_quota_exceeded`, `model_capacity_exceeded`, or
`model_unavailable` provider error markers permit automatic failover. Generic
“Free usage limit”, shared/unknown quota, rate limits, billing, authentication,
connection and malformed-response errors **stop**, rather than rotating models.
The currently reported generic free-usage error therefore still requires operator
review. The catalogue supplies no evidence of independent per-model quotas.
Nothing here bypasses provider limits, buys credit, enables trading, or promises
unlimited inference. A successful test may itself have used an alternate; inspect
its recorded attempts and actual response model.

## Offline analysis input estimate

The workspace includes an offline input-token estimate button. Select an existing
agent first. `/api/estimate-analysis-tokens` builds the same messages as advisory
inference using a shared builder and 300 bars for one selected agent symbol.
It reads market data but never invokes a model, creates proposals, records an
agent run, consumes a test-budget attempt, or writes a provider-usage row.

The result is **ceil(message content characters / 4)**, not tokenizer-measured
usage. Chat formatting overhead is excluded and tokenization differs across
models and languages. Output, reasoning, cache hits and real cost remain unknown.
No prompt text is returned. The response identifies the backend and sample time;
`native` identifies the source but does not prove quote freshness. Mock order
quotes are labelled simulated, never live MT5. Starting a native-demo console
requires the local MT5 connection workflow; no automatic mock fallback should
be interpreted as a native connection.

## Explicit free OpenCode session connection (v2.2.7)

Set `TRADE_MCP_WEB_MODEL_BACKEND=opencode-session` explicitly to use the
already authenticated local OpenCode server, with `TRADE_MCP_OPENCODE_URL`,
`TRADE_MCP_OPENCODE_MODEL` and optional `TRADE_MCP_OPENCODE_VARIANT`.
Authentication uses the existing secure desktop discovery/launcher workflow;
never paste the desktop password or MT5 keys into chat. Default backend remains
`direct`; there is no implicit fallback. Only explicitly free allowlisted model
IDs are accepted in the session mode. Catalogue prices/entitlements can change.

Each web call creates a NEW deny-all session with the explicit model, verifies
the server acknowledgement, then uses `/api/session/{id}/generate`. No existing
development session ID is accepted. The ephemeral session is deleted afterwards;
the web ledger retains returned usage metadata. OpenCode may include project or
agent instructions, so its returned token usage can exceed the offline estimate.
No tools are executed by the application's generate call.

Token categories are reported exactly as returned, with unknown accounting
convention (no combined total). Server-calculated catalogue cost is NOT a
provider invoice and is not stored as provider-reported cost. Missing usage or
cost remains unknown. Failure does not prove zero billed tokens. Shared free
quota remains shared; separate sessions do not create new quota.

The persisted five-request/300-second budget wraps this path too. It counts
application generation invocations, NOT internal upstream retries: OpenCode
v2.2.7's generate implementation uses its own ModelRetry policy. The application
cannot enforce a provider-level attempt cap or cancel an upstream request merely
by timing out. No app retry or model rotation is performed. Stop testing on quota
or rate-limit errors; never switch routes to evade provider restrictions.

After updating, the local operator must explicitly select this backend, reload
only the relevant MCP/owned console, verify native versus mock data, activate a
test budget and make one model test. A successful local mock suite does not prove
live provider entitlement. Keep trading disabled throughout.
