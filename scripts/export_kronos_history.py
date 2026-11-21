"""Read-only native M1 export for Colab; never loads models or account/order data."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))

from trade_mcp.bridge.client import MT5  # noqa: E402
from trade_mcp.bridge.native import NativeBridge  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.research_data import closed_m1_segment  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--symbol', default='XAUUSD')
    parser.add_argument('--count', type=int, default=1000)
    args = parser.parse_args()
    if not 420 <= args.count <= 5000:
        parser.error('count must be 420..5000')
    load_dotenv()
    cfg = Config.from_env()
    if cfg.backend != 'native':
        raise SystemExit('Native broker history required; no mock substitution')
    mt5 = MT5(NativeBridge(cfg.mcp_url, cfg.mcp_token, timeout=cfg.timeout), args.symbol)
    try:
        rows = closed_m1_segment(mt5.candles(args.symbol, 'M1', args.count),
                                 now=time.time(), maximum=args.count)
    except Exception:
        raise SystemExit('History export failed; check UTC/contiguity/access locally. Raw error withheld.') from None
    finally:
        mt5.close()
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'symbol': args.symbol, 'timeframe': 'M1', 'timezone': 'UTC',
                   'selection': 'latest_eligible_contiguous_closed_segment', 'rows': rows},
                  stream, allow_nan=False)
    print('Redacted closed M1 research export complete; rows:', len(rows))


if __name__ == '__main__':
    main()
