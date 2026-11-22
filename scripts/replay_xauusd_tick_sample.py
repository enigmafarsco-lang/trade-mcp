"""Read an existing owner-only MCP raw tick response; no MT5/network calls."""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))

from trade_mcp.bridge.native import records  # noqa: E402
from trade_mcp.mcpclient import McpHttpClient  # noqa: E402
from trade_mcp.tick_replay import replay_signals  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('raw_response', type=Path)
    p.add_argument('output', type=Path)
    a = p.parse_args()
    if a.output.exists():
        raise SystemExit('Output exists; no overwrite')
    try:
        if not 0 < a.raw_response.stat().st_size <= 10_000_000:
            raise ValueError('size')
        raw = a.raw_response.read_bytes()
        result = json.loads(raw)
        if result.get('isError'):
            raise ValueError('tool error')
        data = McpHttpClient.structured_content(result)
        rows = records(data, 'ticks', 'quotes', 'history', 'data')
        report = replay_signals(rows)
        report['input_sha256'] = hashlib.sha256(raw).hexdigest()
    except Exception:
        raise SystemExit('Replay blocked: invalid/unsupported raw sample; no raw error or prices printed') from None
    a.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(a.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
