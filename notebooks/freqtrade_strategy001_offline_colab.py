# FULL SINGLE-CELL OFFLINE RESEARCH WORKFLOW; HOSTED COLAB ONLY.
# Setup may access PyPI/GitHub. Engine phase denies Python socket network access.
# Real local bundle required BEFORE any installation; no Binance request anywhere.
import sys, os, json, hashlib, base64, urllib.request, subprocess
from pathlib import Path
from google.colab import files
assert Path('/content').is_dir(), 'Hosted Colab only'
HELPER_SOURCE='"""Offline input validation/network guard. Does not implement a trading simulator."""\nimport hashlib\nimport json\nimport math\nimport socket\nfrom contextlib import contextmanager\nfrom pathlib import Path\n\nSTART = 1767052800  # 2025-12-30 UTC, warmup\nEND = 1775001600  # 2026-04-01 UTC, exclusive\n\n\ndef validate_bundle(value):\n    """A provenance declaration is recorded, not authenticated by this validator."""\n    if not isinstance(value, dict) or set(value) != {\'schema\', \'provenance\', \'market\', \'candles\'}:\n        raise ValueError(\'Expected offline bundle schema, provenance, market and candles only\')\n    if value[\'schema\'] != \'freqtrade-offline-bundle/v1\':\n        raise ValueError(\'Unsupported bundle schema\')\n    provenance = value[\'provenance\']\n    if (not isinstance(provenance, dict)\n            or set(provenance) != {\'exchange\', \'source\', \'captured_at\', \'ccxt_version\'}\n            or provenance.get(\'exchange\') != \'binance\'\n            or any(not isinstance(v, str) or not v.strip() for v in provenance.values())):\n        raise ValueError(\'Real Binance source/capture/CCXT version declaration required\')\n    market = value[\'market\']\n    if (not isinstance(market, dict) or market.get(\'symbol\') != \'BTC/USDT\'\n            or market.get(\'base\') != \'BTC\' or market.get(\'quote\') != \'USDT\'\n            or market.get(\'spot\') is not True or market.get(\'active\') is not True\n            or not market.get(\'id\') or market.get(\'contract\') is not False):\n        raise ValueError(\'Require actual active BTC/USDT spot CCXT market snapshot\')\n    for name in (\'amount\', \'price\'):\n        number = market.get(\'precision\', {}).get(name)\n        if isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number) or number <= 0:\n            raise ValueError(\'CCXT tick-size price/amount precision required\')\n    for name in (\'amount\', \'cost\', \'price\'):\n        limits = market.get(\'limits\', {}).get(name)\n        if not isinstance(limits, dict) or not {\'min\', \'max\'} <= set(limits):\n            raise ValueError(\'Actual market limits required, no invented defaults\')\n        for number in limits.values():\n            if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float))\n                                       or not math.isfinite(number) or number < 0):\n                raise ValueError(\'Invalid market limits\')\n        if limits[\'min\'] is not None and limits[\'max\'] is not None and limits[\'min\'] > limits[\'max\']:\n            raise ValueError(\'Reversed market limits\')\n    candles = value[\'candles\']\n    if not isinstance(candles, dict) or set(candles) != {\'1m\', \'5m\'}:\n        raise ValueError(\'Both real UTC 1m and 5m candles required\')\n    for tf, step in ((\'1m\', 60), (\'5m\', 300)):\n        rows = candles[tf]\n        expected_count = (END-START)//step\n        if not isinstance(rows, list) or len(rows) != expected_count:\n            raise ValueError(f\'{tf}: require complete fixed 20251230-20260401 UTC range\')\n        for i, row in enumerate(rows):\n            if (not isinstance(row, list) or len(row) != 6\n                    or any(isinstance(n, bool) or not isinstance(n, (int, float))\n                           or not math.isfinite(n) for n in row)):\n                raise ValueError(\'Finite numeric OHLCV arrays required\')\n            t, o, h, lo, c, volume = row\n            if (t != START+i*step or min(o, h, lo, c) <= 0 or volume < 0\n                    or h < max(o, lo, c) or lo > min(o, h, c)):\n                raise ValueError(\'Gap, invalid timestamp/price/volume or candle bounds\')\n    for i, row in enumerate(candles[\'5m\']):\n        group = candles[\'1m\'][i*5:i*5+5]\n        aggregated = [group[0][1], max(r[2] for r in group), min(r[3] for r in group),\n                      group[-1][4], sum(r[5] for r in group)]\n        if any(not math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-8)\n               for a, b in zip(row[1:], aggregated, strict=True)):\n            raise ValueError(\'1m/5m data disagree; do not manufacture replacement candles\')\n    return value\n\n\ndef load_bundle(path):\n    path = Path(path)\n    if not 0 < path.stat().st_size <= 80_000_000:\n        raise ValueError(\'Bundle size must be 1..80000000 bytes\')\n    raw = path.read_bytes()\n    value = validate_bundle(json.loads(raw))\n    return value, hashlib.sha256(raw).hexdigest()\n\n\n@contextmanager\ndef deny_network():\n    """Fail closed on Python socket egress, including DNS; not an OS sandbox."""\n    names = (\'create_connection\', \'getaddrinfo\')\n    original = {name: getattr(socket, name) for name in names}\n    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex\n\n    def denied(*args, **kwargs):\n        raise RuntimeError(\'OFFLINE_NETWORK_DENIED: no exchange access, fallback or retry permitted\')\n\n    try:\n        for name in names:\n            setattr(socket, name, denied)\n        socket.socket.connect = denied\n        socket.socket.connect_ex = denied\n        yield\n    finally:\n        for name, func in original.items():\n            setattr(socket, name, func)\n        socket.socket.connect, socket.socket.connect_ex = connect, connect_ex\n'
WORKER_SOURCE='"""Genuine Freqtrade engine with caller-supplied real candles/market snapshot.\n\nRun only in the isolated hosted research environment with Freqtrade preinstalled.\nNo downloads, package installation, exchange requests or fabricated market defaults.\n"""\nimport argparse\nimport importlib.metadata\nimport json\nimport sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).resolve().parents[1]/\'src\'))\n\nfrom trade_mcp.freqtrade_offline import deny_network, load_bundle  # noqa: E402\n\nSTRATEGY001_SHA256 = "2357996e6594458e8db45b5f757918462afa3c998de9d06e2af0c009ddc4b80d"\n\n\ndef run(bundle_path, strategy_path, config_path, output):\n    import hashlib\n\n    if importlib.metadata.version(\'freqtrade\') != \'2025.10\':\n        raise ValueError(\'Pinned Freqtrade 2025.10 required\')\n    bundle, digest = load_bundle(bundle_path)\n    if importlib.metadata.version(\'ccxt\') != bundle[\'provenance\'][\'ccxt_version\']:\n        raise ValueError(\'Installed CCXT must match real market snapshot version\')\n    if hashlib.sha256(strategy_path.read_bytes()).hexdigest() != STRATEGY001_SHA256:\n        raise ValueError(\'Strategy001 source hash mismatch\')\n    config = json.loads(config_path.read_text())\n    if (config[\'exchange\'][\'name\'] != \'binance\' or config[\'exchange\'].get(\'key\')\n            or config[\'exchange\'].get(\'secret\') or config[\'dry_run\'] is not True\n            or config.get(\'telegram\', {}).get(\'enabled\') or config.get(\'api_server\', {}).get(\'enabled\')):\n        raise ValueError(\'Offline dry-run Binance spot configuration without credentials required\')\n    if output.exists():\n        raise ValueError(\'Output already exists; no silent rerun/overwrite\')\n    if (config.get(\'fee\') != 0.001 or config.get(\'stake_amount\') != 100\n            or config.get(\'dry_run_wallet\') != 1000 or config.get(\'max_open_trades\') != 1\n            or config.get(\'timeframe\') != \'5m\' or config.get(\'startup_candle_count\') != 200\n            or any(k in config for k in (\'minimal_roi\', \'stoploss\', \'strategy_list\'))):\n        raise ValueError(\'Use the fixed notebook configuration; no tuning/strategy overrides\')\n    output.mkdir(mode=0o700, parents=True)\n    with deny_network():\n        # These are the actual installed upstream classes, not a replacement simulator.\n        import pandas as pd\n        from freqtrade.configuration import Configuration\n        from freqtrade.enums import RunMode\n        from freqtrade.optimize.backtesting import Backtesting\n        from freqtrade.resolvers import ExchangeResolver\n\n        datadir = output/\'data\'\n        datadir.mkdir()\n        userdir = output/\'user_data\'\n        userdir.mkdir()\n        for tf, rows in bundle[\'candles\'].items():\n            df = pd.DataFrame(rows, columns=[\'date\', \'open\', \'high\', \'low\', \'close\', \'volume\'])\n            df[\'date\'] = pd.to_datetime(df[\'date\'], unit=\'s\', utc=True)\n            df.to_feather(datadir/f\'BTC_USDT-{tf}.feather\')\n        summary = {\'schema\': \'freqtrade-offline-engine-report/v1\', \'bundle_sha256\': digest,\n                   \'network_policy\': \'Python socket egress denied throughout engine phase\',\n                   \'provenance_authenticated\': False, \'provenance\': bundle[\'provenance\'],\n                   \'engine_version\': importlib.metadata.version(\'freqtrade\'),\n                   \'ccxt_version\': importlib.metadata.version(\'ccxt\'),\n                   \'strategy_sha256\': STRATEGY001_SHA256,\n                   \'fee_assumption_per_side\': 0.001,\n                   \'execution_authorized\': False, \'validated_for_trading\': False,\n                   \'research_only\': True, \'market_snapshot_historical_rules_verified\': False,\n                   \'warning\': \'Snapshot source is declared, not independently authenticated. \'\n                              \'Snapshot limits may differ from historical limits. Not XAUUSD/MT5.\',\n                   \'splits\': {}}\n        for split, timerange in [(\'evaluation\', \'20260101-20260301\'),\n                                 (\'final_test\', \'20260301-20260401\')]:\n            cfg = Configuration({\'config\': [str(config_path)], \'user_data_dir\': str(userdir),\n                                 \'datadir\': str(datadir), \'strategy\': \'Strategy001\',\n                                 \'strategy_path\': str(strategy_path.parent), \'timerange\': timerange,\n                                 \'timeframe\': \'5m\', \'timeframe_detail\': \'1m\', \'backtest_cache\': \'none\',\n                                 \'export\': \'trades\', \'exportfilename\': str(output/f\'{split}.json\')},\n                                RunMode.BACKTEST).get_config()\n            if (cfg[\'exchange\'][\'name\'] != \'binance\' or cfg[\'exchange\'].get(\'key\')\n                    or cfg[\'exchange\'].get(\'secret\') or cfg[\'dry_run\'] is not True):\n                raise ValueError(\'Effective configuration must remain credential-free offline research\')\n            cfg[\'exchange\'][\'pair_whitelist\'] = [\'BTC/USDT\']\n            cfg[\'exchange\'][\'enable_ws\'] = False\n            cfg[\'pairlists\'] = [{\'method\': \'StaticPairList\'}]\n            cfg[\'trading_mode\'] = \'spot\'\n            # Only eager network validation/loading is deferred. After snapshot injection,\n            # real exchange config validation still runs with real saved precision/limits.\n            exchange = ExchangeResolver.load_exchange(cfg, validate=False, load_leverage_tiers=False)\n            try:\n                if exchange._api.precisionMode != 4:\n                    raise ValueError(\'Require CCXT TICK_SIZE precision mode for this snapshot\')\n                market = bundle[\'market\']\n                exchange._api.set_markets([market])\n                exchange._api_async.set_markets([market])\n                exchange._markets = exchange._api.markets\n                exchange.validate_config(cfg)\n                engine = Backtesting(cfg, exchange=exchange)\n                engine.start()\n                stats = engine.results[\'strategy\'][\'Strategy001\']\n                keys = (\'total_trades\', \'profit_total\', \'profit_total_abs\', \'profit_factor\',\n                        \'winrate\', \'max_drawdown_account\', \'backtest_start\', \'backtest_end\')\n                summary[\'splits\'][split] = {k: stats.get(k) for k in keys}\n            finally:\n                exchange.close()\n        summary[\'final_test_used_for_tuning\'] = False\n        (output/\'freqtrade_offline_research.json\').write_text(\n            json.dumps(summary, indent=2, allow_nan=False))\n    print(\'Actual offline engine report written. No trading authorization.\')\n\n\ndef main():\n    p = argparse.ArgumentParser(description=__doc__)\n    p.add_argument(\'bundle\', type=Path)\n    p.add_argument(\'strategy\', type=Path)\n    p.add_argument(\'config\', type=Path)\n    p.add_argument(\'output\', type=Path)\n    a = p.parse_args()\n    run(a.bundle, a.strategy, a.config, a.output)\n\n\nif __name__ == \'__main__\':\n    main()\n'
CONFIG_VALUE={'dry_run': True, 'dry_run_wallet': 1000, 'stake_currency': 'USDT', 'stake_amount': 100, 'max_open_trades': 1, 'tradable_balance_ratio': 0.99, 'timeframe': '5m', 'startup_candle_count': 200, 'trading_mode': 'spot', 'margin_mode': '', 'fee': 0.001, 'cancel_open_orders_on_exit': False, 'unfilledtimeout': {'entry': 10, 'exit': 10, 'exit_timeout_count': 0, 'unit': 'minutes'}, 'entry_pricing': {'price_side': 'same', 'use_order_book': True, 'order_book_top': 1, 'price_last_balance': 0.0, 'check_depth_of_market': {'enabled': False, 'bids_to_ask_delta': 1}}, 'exit_pricing': {'price_side': 'same', 'use_order_book': True, 'order_book_top': 1}, 'exchange': {'name': 'binance', 'key': '', 'secret': '', 'pair_whitelist': ['BTC/USDT'], 'pair_blacklist': [], 'ccxt_config': {'enableRateLimit': True, 'options': {'maxRetriesOnFailure': 0}}}, 'pairlists': [{'method': 'StaticPairList'}], 'telegram': {'enabled': False, 'token': '', 'chat_id': ''}, 'bot_name': 'research_only', 'dataformat_ohlcv': 'feather', 'dataformat_trades': 'feather'}
validation={}
exec(compile(HELPER_SOURCE,'offline_validation.py','exec'),validation)
print('Upload an EXISTING REAL freqtrade-offline-bundle/v1 JSON. Not the Kronos report or XAUUSD file.')
uploaded=files.upload()
assert len(uploaded)==1, 'Exactly one real offline bundle required'
raw=next(iter(uploaded.values()))
assert 0<len(raw)<=80_000_000, 'Input size limit exceeded'
validation['validate_bundle'](json.loads(raw))
print('Input structure/coverage checked. Provenance declaration is not independently authenticated.')

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
ENV=WORK/'isolated-env'
PY=ENV/'bin/python'
FREQTRADE_VERSION='2025.10'
probe_code='import sys,importlib.metadata as m; assert sys.prefix!=sys.base_prefix; assert m.version("freqtrade")=="2025.10"'
ready=PY.is_file() and subprocess.run([str(PY),'-c',probe_code],capture_output=True).returncode==0
if ready:
    print('Reusing installed isolated Freqtrade 2025.10; no reinstall or session restart.')
else:
    BOOTSTRAP=WORK/'virtualenv-bootstrap'
    run_visible([sys.executable,'-m','pip','install','--target',BOOTSTRAP,'virtualenv==20.28.0'])
    bootstrap_env=os.environ.copy()
    bootstrap_env['PYTHONPATH']=str(BOOTSTRAP)
    run_visible([sys.executable,'-m','virtualenv','--no-download','--python',sys.executable,ENV],
                env=bootstrap_env)
    run_visible([PY,'-c',
        'import sys,pip; assert sys.prefix!=sys.base_prefix; print("Isolated Python ready:",sys.version)'])
    run_visible([PY,'-m','pip','install','freqtrade=='+FREQTRADE_VERSION])
run_visible([PY,'-m','freqtrade','--version'])
subprocess.check_call([str(PY),'-m','pip','freeze'],stdout=open(WORK/'dependencies.txt','w'))


PROJECT=WORK/'offline-project'
(PROJECT/'src/trade_mcp').mkdir(parents=True,exist_ok=True)
(PROJECT/'scripts').mkdir(exist_ok=True)
(PROJECT/'src/trade_mcp/__init__.py').write_text('')
(PROJECT/'src/trade_mcp/freqtrade_offline.py').write_text(HELPER_SOURCE)
worker=PROJECT/'scripts/freqtrade_offline_worker.py'
worker.write_text(WORKER_SOURCE)
bundle_path=WORK/'offline_input.json'
if bundle_path.exists():
    assert bundle_path.read_bytes()==raw, 'A different offline input already exists; do not overwrite'
else:
    fd=os.open(bundle_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(raw)
config_path=WORK/'offline-config.json'
config_path.write_text(json.dumps(CONFIG_VALUE,indent=2))
STRATEGY_COMMIT='f3340ce11f5bdf62f598522e64d1f5638eaa13f5'
STRATEGY_SHA='2357996e6594458e8db45b5f757918462afa3c998de9d06e2af0c009ddc4b80d'
source_path=WORK/'offline-strategy/Strategy001.py'
if not source_path.exists():
    url=('https://api.github.com/repos/freqtrade/freqtrade-strategies/contents/'
         'user_data/strategies/Strategy001.py?ref='+STRATEGY_COMMIT)
    with urllib.request.urlopen(url,timeout=30) as response:
        source=base64.b64decode(json.load(response)['content'])
    assert hashlib.sha256(source).hexdigest()==STRATEGY_SHA
    source_path.parent.mkdir(exist_ok=True)
    source_path.write_bytes(source)
assert hashlib.sha256(source_path.read_bytes()).hexdigest()==STRATEGY_SHA
OUTPUT=WORK/'offline-engine-results'
assert not OUTPUT.exists(), 'Existing results preserved; do not silently rerun'
print('Starting real Freqtrade engine with Python network egress denied. No exchange calls.')
run_visible([PY,worker,bundle_path,source_path,config_path,OUTPUT])
summary=OUTPUT/'freqtrade_offline_research.json'
assert summary.is_file(), 'No actual engine summary produced'
print(summary.read_text())
import shutil
archive=shutil.make_archive(str(WORK/'freqtrade_offline_engine_results'),'zip',OUTPUT)
files.download(str(summary))
files.download(archive)
print('Research only. Not XAUUSD/MT5 and not trading authorization.')
