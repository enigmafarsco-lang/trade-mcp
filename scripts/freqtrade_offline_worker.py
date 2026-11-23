"""Genuine Freqtrade engine with caller-supplied real candles/market snapshot.

Run only in the isolated hosted research environment with Freqtrade preinstalled.
No downloads, package installation, exchange requests or fabricated market defaults.
"""
import argparse
import importlib.metadata
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))

from trade_mcp.freqtrade_offline import deny_network, load_bundle  # noqa: E402

STRATEGY001_SHA256 = "2357996e6594458e8db45b5f757918462afa3c998de9d06e2af0c009ddc4b80d"


def run(bundle_path, strategy_path, config_path, output):
    import hashlib

    if importlib.metadata.version('freqtrade') != '2025.10':
        raise ValueError('Pinned Freqtrade 2025.10 required')
    bundle, digest = load_bundle(bundle_path)
    if importlib.metadata.version('ccxt') != bundle['provenance']['ccxt_version']:
        raise ValueError('Installed CCXT must match real market snapshot version')
    if hashlib.sha256(strategy_path.read_bytes()).hexdigest() != STRATEGY001_SHA256:
        raise ValueError('Strategy001 source hash mismatch')
    config = json.loads(config_path.read_text())
    if (config['exchange']['name'] != 'binance' or config['exchange'].get('key')
            or config['exchange'].get('secret') or config['dry_run'] is not True
            or config.get('telegram', {}).get('enabled') or config.get('api_server', {}).get('enabled')):
        raise ValueError('Offline dry-run Binance spot configuration without credentials required')
    if output.exists():
        raise ValueError('Output already exists; no silent rerun/overwrite')
    if (config.get('fee') != 0.001 or config.get('stake_amount') != 100
            or config.get('dry_run_wallet') != 1000 or config.get('max_open_trades') != 1
            or config.get('timeframe') != '5m' or config.get('startup_candle_count') != 200
            or any(k in config for k in ('minimal_roi', 'stoploss', 'strategy_list'))):
        raise ValueError('Use the fixed notebook configuration; no tuning/strategy overrides')
    output.mkdir(mode=0o700, parents=True)
    with deny_network():
        # These are the actual installed upstream classes, not a replacement simulator.
        import pandas as pd
        from freqtrade.configuration import Configuration
        from freqtrade.enums import RunMode
        from freqtrade.optimize.backtesting import Backtesting
        from freqtrade.resolvers import ExchangeResolver

        datadir = output/'data'
        datadir.mkdir()
        userdir = output/'user_data'
        userdir.mkdir()
        for tf, rows in bundle['candles'].items():
            df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
            df['date'] = pd.to_datetime(df['date'], unit='s', utc=True)
            df.to_feather(datadir/f'BTC_USDT-{tf}.feather')
        summary = {'schema': 'freqtrade-offline-engine-report/v1', 'bundle_sha256': digest,
                   'network_policy': 'Python socket egress denied throughout engine phase',
                   'provenance_authenticated': False, 'provenance': bundle['provenance'],
                   'engine_version': importlib.metadata.version('freqtrade'),
                   'ccxt_version': importlib.metadata.version('ccxt'),
                   'strategy_sha256': STRATEGY001_SHA256,
                   'fee_assumption_per_side': 0.001,
                   'execution_authorized': False, 'validated_for_trading': False,
                   'research_only': True, 'market_snapshot_historical_rules_verified': False,
                   'warning': 'Snapshot source is declared, not independently authenticated. '
                              'Snapshot limits may differ from historical limits. Not XAUUSD/MT5.',
                   'splits': {}}
        for split, timerange in [('evaluation', '20260101-20260301'),
                                 ('final_test', '20260301-20260401')]:
            cfg = Configuration({'config': [str(config_path)], 'user_data_dir': str(userdir),
                                 'datadir': str(datadir), 'strategy': 'Strategy001',
                                 'strategy_path': str(strategy_path.parent), 'timerange': timerange,
                                 'timeframe': '5m', 'timeframe_detail': '1m', 'backtest_cache': 'none',
                                 'export': 'trades', 'exportfilename': str(output/f'{split}.json')},
                                RunMode.BACKTEST).get_config()
            if (cfg['exchange']['name'] != 'binance' or cfg['exchange'].get('key')
                    or cfg['exchange'].get('secret') or cfg['dry_run'] is not True):
                raise ValueError('Effective configuration must remain credential-free offline research')
            cfg['exchange']['pair_whitelist'] = ['BTC/USDT']
            cfg['exchange']['enable_ws'] = False
            cfg['pairlists'] = [{'method': 'StaticPairList'}]
            cfg['trading_mode'] = 'spot'
            # Only eager network validation/loading is deferred. After snapshot injection,
            # real exchange config validation still runs with real saved precision/limits.
            exchange = ExchangeResolver.load_exchange(cfg, validate=False, load_leverage_tiers=False)
            try:
                if exchange._api.precisionMode != 4:
                    raise ValueError('Require CCXT TICK_SIZE precision mode for this snapshot')
                market = bundle['market']
                exchange._api.set_markets([market])
                exchange._api_async.set_markets([market])
                exchange._markets = exchange._api.markets
                exchange.validate_config(cfg)
                engine = Backtesting(cfg, exchange=exchange)
                engine.start()
                stats = engine.results['strategy']['Strategy001']
                keys = ('total_trades', 'profit_total', 'profit_total_abs', 'profit_factor',
                        'winrate', 'max_drawdown_account', 'backtest_start', 'backtest_end')
                summary['splits'][split] = {k: stats.get(k) for k in keys}
            finally:
                exchange.close()
        summary['final_test_used_for_tuning'] = False
        (output/'freqtrade_offline_research.json').write_text(
            json.dumps(summary, indent=2, allow_nan=False))
    print('Actual offline engine report written. No trading authorization.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('bundle', type=Path)
    p.add_argument('strategy', type=Path)
    p.add_argument('config', type=Path)
    p.add_argument('output', type=Path)
    a = p.parse_args()
    run(a.bundle, a.strategy, a.config, a.output)


if __name__ == '__main__':
    main()
