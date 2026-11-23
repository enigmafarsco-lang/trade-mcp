"""Offline comparison of saved availability label to pre-registered window labels.

No network calls and no provider values disclosed. Lexical calendar comparison
of naive labels is not a verified common timezone or server semantics.
"""
import json
import os
from datetime import datetime
from pathlib import Path

ROOT = Path('logs/research-inputs/xauusd-sparse-screen-20261109-10-v1')
RAW = ROOT / 'eval-1-raw-response.json'
OUTPUT = ROOT / 'availability-label-comparison.json'
WINDOWS = (
    ('eval-1', '2026-11-09T09:00:00', '2026-11-09T09:05:00'),
    ('eval-2', '2026-11-09T13:00:00', '2026-11-09T13:05:00'),
    ('eval-3', '2026-11-09T19:00:00', '2026-11-09T19:05:00'),
    ('final-1', '2026-11-10T09:00:00', '2026-11-10T09:05:00'),
    ('final-2', '2026-11-10T13:00:00', '2026-11-10T13:05:00'),
    ('final-3', '2026-11-10T19:00:00', '2026-11-10T19:05:00'),
)


def compare(result):
    report = {'schema': 'xauusd-availability-label-comparison/v1',
              'status': 'not_comparable', 'history_calls_made': 0,
              'raw_availability_label_disclosed': False,
              'response_time_semantics_verified': False, 'utc_verified': False,
              'provider_root_cause_verified': False, 'complete_coverage_verified': False,
              'execution_authorized': False,
              'comparison_note': 'calendar-label ordering only; common time basis and semantics unverified',
              'window_relationships': {}}
    if not isinstance(result, dict) or result.get('isError'):
        report['blocker'] = 'invalid_or_mcp_error_response'
        return report
    data = result.get('structuredContent')
    if data is None:
        parts = result.get('content')
        texts = [p['text'] for p in parts if isinstance(p, dict) and p.get('type') == 'text'
                 and isinstance(p.get('text'), str)] if isinstance(parts, list) else []
        try:
            data = json.loads('\n'.join(texts))
        except (TypeError, ValueError):
            report['blocker'] = 'saved_payload_not_json'
            return report
    if (not isinstance(data, dict) or data.get('symbol') != 'XAUUSD_i'
            or data.get('period') != 'tick' or data.get('ok') is not False
            or data.get('history') != []):
        report['blocker'] = 'saved_payload_does_not_match_failed_request'
        return report
    stamp = data.get('data_available_from')
    if not isinstance(stamp, str):
        report['blocker'] = 'availability_label_missing_or_not_string'
        return report
    try:
        available = datetime.fromisoformat(stamp)
    except ValueError:
        report['blocker'] = 'unsupported_availability_label_format'
        return report
    if available.tzinfo is not None:
        report['blocker'] = 'timezone_bearing_availability_label_not_comparable_to_naive_request'
        return report
    for name, start, end in WINDOWS:
        a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
        if available <= a:
            relationship = 'availability_label_at_or_before_window_start'
        elif available >= b:
            relationship = 'availability_label_at_or_after_window_end'
        else:
            relationship = 'availability_label_inside_window'
        report['window_relationships'][name] = relationship
    report['status'] = 'calendar_labels_compared'
    return report


def main():
    if not RAW.is_file() or not 0 < RAW.stat().st_size <= 10_000_000:
        print('Saved response missing or invalid size; no request made.')
        return 1
    try:
        report = compare(json.loads(RAW.read_bytes()))
    except (ValueError, TypeError, UnicodeDecodeError):
        print('Saved response is not valid JSON; provider content withheld.')
        return 1
    fd = os.open(OUTPUT, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'calendar_labels_compared' else 1


if __name__ == '__main__':
    raise SystemExit(main())
