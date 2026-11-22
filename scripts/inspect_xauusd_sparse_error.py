"""Classify the already saved v1 response offline; never prints provider text."""
import contextlib
import json
import os
from pathlib import Path

ROOT = Path('logs/research-inputs/xauusd-sparse-screen-20261109-10-v1')
RAW = ROOT / 'eval-1-raw-response.json'
OUTPUT = ROOT / 'offline-error-diagnostic.json'

# Literal phrase matches are hints only, not independently verified causes.
HINTS = {
    'history_unavailable_phrase': ('history unavailable', 'history not available', 'no history', 'no data'),
    'date_range_phrase': ('date range', 'time range', 'invalid date', 'invalid time', 'out of range'),
    'symbol_phrase': ('unknown symbol', 'invalid symbol', 'symbol not found'),
    'argument_phrase': ('invalid argument', 'invalid parameter', 'missing parameter', 'unknown parameter'),
    'permission_phrase': ('permission denied', 'access denied', 'not authorized', 'unauthorized'),
    'limit_phrase': ('limit exceeded', 'too many', 'maximum limit', 'rate limit'),
    'timeout_phrase': ('timed out', 'timeout'),
}


def classify(result):
    if not isinstance(result, dict):
        raise ValueError('Saved response is not an object')
    structured = result.get('structuredContent')
    parts = result.get('content')
    texts = []
    if isinstance(parts, list):
        texts = [p['text'] for p in parts if isinstance(p, dict)
                 and p.get('type') == 'text' and isinstance(p.get('text'), str)]
    parsed = None
    if structured is None and texts:
        with contextlib.suppress(ValueError, TypeError):
            parsed = json.loads('\n'.join(texts))
    data = structured if structured is not None else parsed
    # Search only error-related fields and textual MCP content; do not expose it.
    messages = list(texts)
    if isinstance(data, dict):
        for key in ('error', 'message', 'description', 'detail'):
            if isinstance(data.get(key), str):
                messages.append(data[key])
            elif isinstance(data.get(key), dict):
                for nested in ('message', 'description', 'detail'):
                    value = data[key].get(nested)
                    if isinstance(value, str):
                        messages.append(value)
    joined = '\n'.join(messages).lower()
    hints = [name for name, patterns in HINTS.items() if any(x in joined for x in patterns)]
    return {
        'schema': 'xauusd-sparse-offline-error-diagnostic/v1',
        'source': 'existing_saved_eval_1_response',
        'diagnosis_scope': 'structural_flags_and_literal_phrase_hints_only',
        'is_error_true': result.get('isError') is True,
        'structured_content_present': 'structuredContent' in result,
        'text_content_present': bool(texts),
        'structured_payload_type': type(data).__name__ if data is not None else 'unavailable',
        'payload_ok_false': isinstance(data, dict) and data.get('ok') is False,
        'payload_error_truthy': isinstance(data, dict) and bool(data.get('error')),
        'phrase_hints_unverified': hints,
        'root_cause_verified': False,
        'raw_provider_text_disclosed': False,
        'history_calls_made': 0,
        'execution_authorized': False,
    }


def main():
    if not RAW.is_file() or not 0 < RAW.stat().st_size <= 10_000_000:
        print('Existing saved response missing or size invalid; no network attempted.')
        return 1
    try:
        report = classify(json.loads(RAW.read_bytes()))
    except (ValueError, TypeError, UnicodeDecodeError):
        print('Existing response is invalid; raw content withheld; no network attempted.')
        return 1
    fd = os.open(OUTPUT, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
