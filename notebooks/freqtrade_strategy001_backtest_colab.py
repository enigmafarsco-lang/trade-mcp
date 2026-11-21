# Complete hosted-Colab-only Freqtrade backtest. Paste the entire file into ONE cell.
# No credentials, MT5 calls, live trading, fallback or optimization.
# A failure stops the whole cell. Fix the reported cause before rerunning.

# # بک‌تست واقعی Strategy001 با موتور Freqtrade
# **آمادهٔ اجرا در Google Colab؛ هنوز اجرا نشده است.** این آزمایش مستقل BTC/USDT در Binance spot است؛ نه XAUUSD، نه مومنتوم M1 و نه سفارش MT5.
# نیازی به فایل یا کلید حساب ندارید. Runtime تازهٔ Colab باز کنید و سلول‌ها را به‌ترتیب اجرا کنید. نصب فقط در محیط مجازی داخل Colab است؛ هیچ بسته‌ای روی سیستم MT5 نصب نمی‌شود.
# داده عمومی با اینترنت دریافت می‌شود؛ در صورت محدودیت صرافی/شبکه توقف کنید، VPN، retry یا صرافی جایگزین خودکار نداریم.
# بازه‌ها از قبل ثابت: ارزیابی ژانویه–فوریه ۲۰۲۶، تست نهایی مارس ۲۰۲۶. پارامترها براساس نتیجه تغییر نکنند.

import sys, os, subprocess, json, hashlib, zipfile
from pathlib import Path
assert 'google.colab' in sys.modules and Path('/content').is_dir(), 'Hosted Google Colab only'
assert (3,11)<=sys.version_info[:2]<=(3,12), 'Use Colab Python 3.11..3.12'
WORK=Path('/content/freqtrade-real-backtest')
WORK.mkdir(exist_ok=True)
# Colab system Python may lack ensurepip; preserve any partial old venv.
# Install bootstrap tooling into /content only, not the notebook environment.
def run_visible(args, **kwargs):
    result=subprocess.run(list(map(str,args)),capture_output=True,text=True,**kwargs)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    if result.returncode:
        raise RuntimeError('Command failed; read the complete output above. Do not continue.')
    return result
BOOTSTRAP=WORK/'virtualenv-bootstrap'
run_visible([sys.executable,'-m','pip','install','--target',BOOTSTRAP,'virtualenv==20.28.0'])
ENV=WORK/'isolated-env'
PY=ENV/'bin/python'
bootstrap_env=os.environ.copy()
bootstrap_env['PYTHONPATH']=str(BOOTSTRAP)
run_visible([sys.executable,'-m','virtualenv','--no-download','--python',sys.executable,ENV],
            env=bootstrap_env)
run_visible([PY,'-c',
    'import sys,pip; assert sys.prefix!=sys.base_prefix; print("Isolated Python ready:",sys.version)'])
FREQTRADE_VERSION='2025.10'
run_visible([PY,'-m','pip','install','freqtrade=='+FREQTRADE_VERSION])
run_visible([PY,'-m','freqtrade','--version'])
subprocess.check_call([str(PY),'-m','pip','freeze'],stdout=open(WORK/'dependencies.txt','w'))


FREQTRADE_COMMIT='f3340ce11f5bdf62f598522e64d1f5638eaa13f5'
STRATEGY001_SHA256='2357996e6594458e8db45b5f757918462afa3c998de9d06e2af0c009ddc4b80d'
REPO=WORK/'freqtrade-strategies'
if not REPO.exists():
    run_visible(['git','clone','https://github.com/freqtrade/freqtrade-strategies.git',str(REPO)])
run_visible(['git','-C',str(REPO),'fetch','origin',FREQTRADE_COMMIT])
run_visible(['git','-C',str(REPO),'checkout','--detach',FREQTRADE_COMMIT])
source_path=REPO/'user_data/strategies/Strategy001.py'
assert hashlib.sha256(source_path.read_bytes()).hexdigest()==STRATEGY001_SHA256, 'Upstream source mismatch'
assert subprocess.check_output(['git','-C',str(REPO),'rev-parse','HEAD'],text=True).strip()==FREQTRADE_COMMIT
print('Verified genuine GPL-3.0 Strategy001. Source is not modified.')


# ## تنظیمات ثابت و فقط بک‌تست
# سرمایه و stake زیر صرفاً عددهای شبیه‌سازی USDT هستند. کارمزد فرضی ۰٫۱٪ در هر سمت، یک معاملهٔ باز، warm-up برابر۲۰۰ کندل؛ ROI و stoploss خود Strategy001 دست‌نخورده می‌مانند.
# موتور جزئیات کندل۱دقیقه را برای استراتژی۵دقیقه استفاده می‌کند؛ این دادهٔ تیک/Bid/Ask نیست و اسلیپیج، صف سفارش و اجرای بروکر را اثبات نمی‌کند.

USERDIR=WORK/'user_data'
USERDIR.mkdir(parents=True,exist_ok=True)
DATA=WORK/'data'
DATA.mkdir(exist_ok=True)
CONFIG=WORK/'config.json'
config={
 'dry_run':True,'dry_run_wallet':1000,'stake_currency':'USDT','stake_amount':100,
 'max_open_trades':1,'tradable_balance_ratio':0.99,'timeframe':'5m',
 'startup_candle_count':200,'trading_mode':'spot','margin_mode':'',
 'fee':0.001,'cancel_open_orders_on_exit':False,
 'unfilledtimeout':{'entry':10,'exit':10,'exit_timeout_count':0,'unit':'minutes'},
 'entry_pricing':{'price_side':'same','use_order_book':True,'order_book_top':1,
                  'price_last_balance':0.0,'check_depth_of_market':{'enabled':False,'bids_to_ask_delta':1}},
 'exit_pricing':{'price_side':'same','use_order_book':True,'order_book_top':1},
 'exchange':{'name':'binance','key':'','secret':'','pair_whitelist':['BTC/USDT'],
             'pair_blacklist':[],'ccxt_config':{'enableRateLimit':True,'options':{'maxRetriesOnFailure':0}}},
 'pairlists':[{'method':'StaticPairList'}],
 'telegram':{'enabled':False},'api_server':{'enabled':False},'bot_name':'research_only'
}
CONFIG.write_text(json.dumps(config,indent=2))
COVERAGE_VERIFIED=False
PLAN={'evaluation':'20260101-20260301','final_test':'20260301-20260401'}
print('Fixed test plan:',PLAN)
# Only two allowlisted engine subcommands; never trade, webserver or hyperopt.
def ft(command,*args):
    assert command in ('download-data','backtesting')
    return run_visible([PY,'-m','freqtrade',command,'--config',CONFIG,'--userdir',USERDIR,*args])
ft('download-data','--datadir',DATA,'--pairs','BTC/USDT','--timeframes','5m','1m',
   '--timerange','20251230-20260401','--data-format-ohlcv','feather')


# ## بررسی داده، قبل از دیدن نتیجه
# نبود داده/شکاف/کمبود warm-up باعث توقف می‌شود؛ از موفقیت ظاهری با بازهٔ خالی جلوگیری می‌کند. دادهٔ گمشده ساخته نمی‌شود.

COVERAGE_VERIFIED=False
coverage_code = r"""
import sys,json,hashlib
from pathlib import Path
import pandas as pd
root=Path(sys.argv[1])
audit={}
for tf,seconds in [('5m',300),('1m',60)]:
    path=root/f'BTC_USDT-{tf}.feather'
    assert path.is_file(), f'Missing data file: {path.name}; download did not produce expected candles'
    df=pd.read_feather(path)
    assert not df.empty, f'Empty data file: {path.name}'
    dates=pd.to_datetime(df['date'],utc=True)
    assert dates.is_monotonic_increasing and dates.is_unique, 'Unsorted/duplicate candles'
    assert ((dates.diff().dropna().dt.total_seconds())==seconds).all(), 'Historical gap; stop, do not fill'
    assert dates.iloc[0]<=pd.Timestamp('2025-12-31',tz='UTC'), 'Insufficient warm-up'
    assert dates.iloc[-1]>=pd.Timestamp('2026-04-01',tz='UTC')-pd.Timedelta(seconds=seconds), 'Incomplete test range'
    values=df[['open','high','low','close','volume']]
    import numpy as np
    assert np.isfinite(values.to_numpy()).all(), 'Nonfinite OHLCV'
    assert (values[['open','high','low','close']]>0).all().all() and (df.volume>=0).all()
    assert (df.high>=values[['open','low','close']].max(axis=1)).all()
    assert (df.low<=values[['open','high','close']].min(axis=1)).all()
    audit[tf]={'rows':len(df),'start':dates.iloc[0].isoformat(),'end':dates.iloc[-1].isoformat(),
               'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
(root.parent/'coverage.json').write_text(json.dumps(audit,indent=2))
print(json.dumps(audit,indent=2))
"""
run_visible([PY,'-c',coverage_code,DATA])
COVERAGE_VERIFIED=True


# Real engine, two predeclared windows. No optimization and no result cache.
assert globals().get('COVERAGE_VERIFIED') is True, 'Data checks did not pass; do not backtest'
RESULTS=WORK/'verified-results'
RESULTS.mkdir(exist_ok=True)
for split,timerange in PLAN.items():
    output=RESULTS/split
    assert not output.exists(), 'Results already exist; do not silently rerun or overwrite'
    output.mkdir()
    ft('backtesting','--datadir',DATA,'--strategy','Strategy001',
       '--strategy-path',source_path.parent,'--timeframe','5m','--timeframe-detail','1m',
       '--timerange',timerange,'--cache','none','--export','trades',
       '--export-filename',output/'result.json')


# ## خروجی واقعی، حتی با زیان یا صفر معامله
# این خلاصه از فایل نتیجهٔ خود موتور خوانده می‌شود. صفر معامله، نه سود و نه اعتبار است. buy-and-hold مرجع توصیفی است؛ سرمایه‌گذاری آن با stake صدتایی استراتژی هم‌اندازه نیست.

report={'schema':'freqtrade-strategy001-engine-backtest/v1',
 'engine_version':FREQTRADE_VERSION,'strategy_commit':FREQTRADE_COMMIT,
 'strategy_sha256':STRATEGY001_SHA256,'pair':'BTC/USDT','exchange':'binance',
 'timeframe':'5m','detail_timeframe':'1m','plan':PLAN,'final_test_used_for_tuning':False,
 'execution_authorized':False,'validated_for_trading':False,'research_only':True,
 'is_xauusd_test':False,'is_m1_tick_momentum':False,
 'fee_per_side':0.001,'coverage':json.loads((WORK/'coverage.json').read_text()),
 'limitations':['No tick bid/ask or real slippage simulation','One pair, one fixed final-test month',
                'Not an MT5 or XAUUSD test','Dependencies recorded but not fully locked'],
 'splits':{}}
for split in PLAN:
    archives=list((RESULTS/split).glob('*.zip'))
    assert len(archives)==1, 'Expected one actual engine result archive'
    with zipfile.ZipFile(archives[0]) as z:
        candidates=[]
        for name in z.namelist():
            if name.endswith('.json') and not name.endswith('.meta.json'):
                obj=json.loads(z.read(name))
                if isinstance(obj,dict) and 'strategy' in obj:
                    candidates.append(obj)
        assert len(candidates)==1, 'Engine statistics missing or ambiguous'
        stats=candidates[0]['strategy']['Strategy001']
    assert stats['timeframe']=='5m'
    keys=['total_trades','profit_total','profit_total_abs','profit_factor','winrate',
          'max_drawdown_account','market_change','backtest_start','backtest_end',
          'backtest_days','starting_balance','final_balance','exit_reason_summary']
    report['splits'][split]={k:stats.get(k) for k in keys}
    print(split,json.dumps(report['splits'][split],indent=2))
summary=WORK/'freqtrade_strategy001_engine_research.json'
summary.write_text(json.dumps(report,indent=2,allow_nan=False))
# Bundle raw engine results plus explicit assumptions; no user account credentials exist.
import shutil
bundle=WORK/'download'
bundle.mkdir(exist_ok=True)
shutil.copy2(summary,bundle/summary.name)
shutil.copy2(WORK/'dependencies.txt',bundle/'dependencies.txt')
shutil.copy2(CONFIG,bundle/'simulation-config.json')
for split in PLAN:
    for archive in (RESULTS/split).glob('*.zip'):
        shutil.copy2(archive,bundle/(split+'-'+archive.name))
shutil.make_archive(str(WORK/'freqtrade_strategy001_engine_results'),'zip',bundle)
from google.colab import files
files.download(str(summary))
files.download(str(WORK/'freqtrade_strategy001_engine_results.zip'))
print('Actual engine results exported. Research only. No trading authorization.')

