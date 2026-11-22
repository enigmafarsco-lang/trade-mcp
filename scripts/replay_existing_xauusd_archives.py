"""Find and replay already-archived XAUUSD MCP responses; NO MT5/network calls.

This is an exploratory inventory of existing files, not a preregistered final
backtest. Limits are deliberately small and overlapping samples are excluded.
"""
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from trade_mcp.bridge.native import records  # noqa: E402
from trade_mcp.mcpclient import McpHttpClient  # noqa: E402
from trade_mcp.tick_cash_replay import economics, replay_cash_scenario  # noqa: E402
from trade_mcp.tick_replay import replay_signals  # noqa: E402

ROOT = Path('logs/research-inputs')
ECONOMICS = ROOT / 'xauusd-cash-research-20261122T075312Z/symbol-economics.json'
OUTPUT = ROOT / 'xauusd-existing-archives-inventory-v1.json'
FIELDS = {'trade_contract_size', 'currency_profit', 'trade_calc_mode',
          'volume_min', 'volume_max', 'volume_step'}
MAX_FILES = 128
MAX_VISITED = 2000
MAX_SIZE = 10_000_000


def candidate_paths(root):
    """Bound traversal, never follow symlinked files/directories."""
    if not root.is_dir() or root.is_symlink():
        raise ValueError('archive_root_missing_or_symlink')
    result, visited = [], 0
    for current, dirs, files in os.walk(root, followlinks=False):
        visited += len(dirs) + len(files)
        if visited > MAX_VISITED:
            raise ValueError('inventory_entry_cap_exceeded')
        dirs[:] = sorted(d for d in dirs if not (Path(current)/d).is_symlink())
        for name in sorted(files):
            path = Path(current)/name
            if (name == 'raw-response.json' or name.endswith('-raw-response.json')) and not path.is_symlink():
                result.append(path)
                if len(result) > MAX_FILES:
                    raise ValueError('inventory_file_cap_exceeded')
    return result


def collect(paths):
    """Only accept explicit successful XAUUSD_i tick payloads; never guess origin."""
    candidates, reasons, seen = [], Counter(), set()
    for path in paths:
        try:
            if not 0 < path.stat().st_size <= MAX_SIZE:
                reasons['invalid_file_size'] += 1
                continue
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).digest()
            if digest in seen:
                reasons['duplicate_file'] += 1
                continue
            seen.add(digest)
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get('isError'):
                reasons['failed_or_invalid_response'] += 1
                continue
            data = McpHttpClient.structured_content(result)
            if (not isinstance(data, dict) or data.get('symbol') != 'XAUUSD_i'
                    or str(data.get('period', '')).lower() != 'tick' or data.get('ok') is not True):
                reasons['unverified_symbol_period_or_success'] += 1
                continue
            rows = records(data, 'ticks', 'quotes', 'history', 'data')
            replay_signals(rows)  # validates quotes, order, naive timestamps and <=5 min
            start = datetime.fromisoformat(rows[0]['time_ms'])
            end = datetime.fromisoformat(rows[-1]['time_ms'])
            candidates.append((start, end, rows))
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            reasons['invalid_or_unsupported_sample'] += 1
    candidates.sort(key=lambda item: (item[0], item[1]))
    selected = []
    for item in candidates:
        if selected and item[0] <= selected[-1][1]:
            reasons['overlap_excluded'] += 1
        else:
            selected.append(item)
    return selected, dict(reasons)


def evaluate(root, econ_path):
    report = {'schema': 'xauusd-existing-archives-inventory/v1',
              'history_calls_made': 0, 'new_network_calls': 0,
              'selection': 'existing response files only; sorted chronologically, exclude overlaps',
              'complete_tick_coverage_verified': False, 'utc_verified': False,
              'independent_hypothetical_windows_not_continuous': True,
              'economics_symbol_binding_independently_verified': False,
              'net_pnl': None, 'commission': None, 'swap': None,
              'profitability_proven': False, 'execution_authorized': False,
              'windows': []}
    try:
        paths = candidate_paths(root)
        selected, reasons = collect(paths)
    except (OSError, ValueError):
        report['status'] = 'blocked_archive_root_unavailable_or_inventory_limit'
        return report
    report['archive_files_inspected'] = len(paths)
    report['excluded_by_reason'] = reasons
    report['unique_usable_nonoverlapping_samples'] = len(selected)
    report['distinct_broker_label_dates'] = len({start.date() for start, _, _ in selected})
    if not selected:
        report['status'] = 'no_usable_archived_samples'
        return report
    try:
        raw_econ = econ_path.read_bytes()
    except OSError:
        report['status'] = 'blocked_saved_economics_unavailable'
        return report
    try:
        if not 0 < len(raw_econ) <= 100000:
            raise ValueError('invalid_economics')
        info = json.loads(raw_econ)
        if not isinstance(info, dict) or set(info) != FIELDS:
            raise ValueError('invalid_economics')
        economics(info)
    except (ValueError, TypeError, OverflowError):
        report['status'] = 'blocked_invalid_or_unsupported_saved_economics'
        return report
    try:
        for i, (_, _, rows) in enumerate(selected, 1):
            report['windows'].append({'window_number': i, 'scenario': replay_cash_scenario(rows, info)})
        report['status'] = 'existing_samples_replayed'
    except Exception:
        report['status'] = 'blocked_offline_replay_error'
        report['raw_error_withheld'] = True
    return report


def main():
    if OUTPUT.exists():
        print('Offline inventory output already exists; no overwrite.')
        return 1
    report = evaluate(ROOT, ECONOMICS)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(OUTPUT, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'existing_samples_replayed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
