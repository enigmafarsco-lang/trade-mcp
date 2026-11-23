"""One local research job: existing ticks + one read-only symbol-metadata request.

Never fetches ticks, reads accounts, installs packages or calls order endpoints.
Raw economics and full scenario stay owner-only; no automatic retries or fallbacks.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))

from trade_mcp.bridge.native import find, records  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.mcpclient import McpHttpClient  # noqa: E402
from trade_mcp.tick_cash_replay import replay_cash_scenario  # noqa: E402
from trade_mcp.tick_replay import replay_signals  # noqa: E402

TOOL = 'get_marketwatch_symbols'
ALIASES = {'trade_contract_size': ('trade_contract_size', 'contract_size'),
           'currency_profit': ('currency_profit', 'profit_currency'),
           'trade_calc_mode': ('trade_calc_mode', 'calc_mode', 'calculation_mode'),
           'volume_min': ('volume_min', 'min_volume'), 'volume_max': ('volume_max', 'max_volume'),
           'volume_step': ('volume_step', 'volume_step_size')}


def write_private(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)


def read_metadata_once(client, symbol):
    tools = [t for t in client.list_tools() if t.get('name') == TOOL]
    if len(tools) != 1:
        raise ValueError('Expected native read-only symbol metadata tool unavailable')
    schema = tools[0].get('inputSchema', {})
    args = {'symbol': symbol, 'limit': 10, 'include_hidden': True}
    if not set(args) <= set(schema.get('properties', {})) or not set(schema.get('required', [])) <= set(args):
        raise ValueError('Metadata schema does not support exact bounded symbol request')
    result = client.call_tool(TOOL, args)
    if result.get('isError'):
        raise ValueError('Metadata tool returned an error')
    data = McpHttpClient.structured_content(result)
    symbols = records(data, 'symbols', 'market_watch')
    matches = [r for r in symbols if str(find(r, 'name', 'symbol', 'symbol_name')).upper() == symbol.upper()]
    if len(matches) != 1:
        raise ValueError('Exact symbol record missing or ambiguous')
    return {key: find(matches[0], *aliases) for key, aliases in ALIASES.items()}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('raw_ticks', type=Path)
    p.add_argument('output_dir', type=Path)
    p.add_argument('--symbol', default='XAUUSD_i')
    p.add_argument('--economics', type=Path, help='Existing owner-only symbol-economics.json; ZERO network calls')
    a = p.parse_args()
    a.output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {'schema': 'xauusd-cash-research-job/v1', 'execution_authorized': False,
              'raw_or_symbol_data_committed': False, 'metadata_tool_calls': 0}
    try:
        if not 0 < a.raw_ticks.stat().st_size <= 10_000_000:
            raise ValueError('Existing tick archive size invalid')
        raw = a.raw_ticks.read_bytes()
        result = json.loads(raw)
        if result.get('isError'):
            raise ValueError('Existing tick response is an error')
        rows = records(McpHttpClient.structured_content(result), 'ticks', 'quotes', 'history', 'data')
        replay_signals(rows)  # Validate existing ticks BEFORE any metadata request.
        report['input_sha256'] = hashlib.sha256(raw).hexdigest()
        if a.economics:
            if not 0 < a.economics.stat().st_size <= 100_000:
                raise ValueError('Existing economics file size invalid')
            metadata_bytes = a.economics.read_bytes()
            metadata = json.loads(metadata_bytes)
            if not isinstance(metadata, dict) or set(metadata) != set(ALIASES):
                raise ValueError('Use original symbol-economics.json, not a report or invented defaults')
            report['economics_source'] = 'existing_local_reported_symbol_snapshot'
            report['economics_sha256'] = hashlib.sha256(metadata_bytes).hexdigest()
            report['economics_symbol_binding_independently_verified'] = False
        else:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                load_dotenv()
                cfg = Config.from_env()
                if cfg.backend != 'native':
                    raise ValueError('Native backend required')
                client = McpHttpClient(cfg.mcp_url, cfg.mcp_token, timeout=min(30, cfg.timeout))
                client.connect()
                report['metadata_tool_calls'] = 'at_most_one_attempt'
                metadata = read_metadata_once(client, a.symbol)
                report['metadata_tool_calls'] = 1
        write_private(a.output_dir/'symbol-economics.json', metadata)
        # Report fixed field names only; never replace missing values by defaults.
        missing = [key for key in ALIASES if metadata.get(key) is None or metadata.get(key) == '']
        report['missing_economic_fields'] = missing
        if missing:
            report['status'] = 'blocked_missing_reported_symbol_economics'
        else:
            scenario = replay_cash_scenario(rows, metadata)
            write_private(a.output_dir/'cash-scenario.json', scenario)
            report['status'] = 'gross_scenario_executed_net_costs_unknown'
            report['scenario'] = scenario
    except Exception:
        report['status'] = 'blocked_invalid_input_metadata_or_unsupported_profit_model'
        report['raw_error_withheld'] = True
    write_private(a.output_dir/'summary.json', report)
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'gross_scenario_executed_net_costs_unknown' else 1


if __name__ == '__main__':
    raise SystemExit(main())
