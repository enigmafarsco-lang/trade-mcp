"""Verify live loopback role previews, never generation, and save sanitized JSON."""
import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from trade_mcp.opencode import NoRedirect


def verify(base_url, agent_id):
    parsed = urlparse(base_url)
    if (parsed.scheme != 'http' or parsed.hostname not in {'localhost','127.0.0.1','::1'}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {'','/'}):
        raise ValueError('Loopback HTTP console URL without credentials required')
    opener = urllib.request.build_opener(NoRedirect)
    def request(path, payload=None):
        req = urllib.request.Request(base_url.rstrip('/')+path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Content-Type':'application/json'}, method='GET' if payload is None else 'POST')
        try:
            with opener.open(req, timeout=60) as response:
                return response.status, json.loads(response.read(1_000_000))
        except urllib.error.HTTPError as exc:
            # Only whitelisted fields below are ever persisted.
            try:
                return exc.code, json.loads(exc.read(1_000_000))
            except ValueError:
                return exc.code, {}
    def boundary(data):
        return {'backend':data.get('backend'), 'trading_enabled':data.get('trading_enabled'),
                'budget_state':(data.get('model_budget') or {}).get('state'),
                'used_attempts':(data.get('model_budget') or {}).get('used_attempts'),
                'ledger_requests':(data.get('model_usage') or {}).get('requests'),
                'scheduler_running':(data.get('scheduler') or {}).get('running'),
                'cycle_active':(data.get('page_cycle') or {}).get('active'),
                'cycle_busy':(data.get('page_cycle') or {}).get('busy'),
                'recent_proposal_ids':[p.get('id') for p in data.get('proposals', [])]}
    status, before = request('/api/workspace-status')
    start = boundary(before)
    if (status != 200 or start['backend'] != 'native' or start['trading_enabled'] is not False
            or start['budget_state'] != 'revoked' or start['scheduler_running'] is not False
            or start['cycle_active'] is not False or start['cycle_busy'] is not False
            or not any(a.get('id') == agent_id for a in before.get('agents', []))):
        return {'ok':False,'error':'safe_console_preconditions_failed','before':start}
    rows = []
    for role in ('market','bull','bear'):
        status, result = request('/api/research/prepare', {'agent_id':agent_id, 'role':role})
        messages = result.get('messages')
        valid = (status == 200 and result.get('ok') is True and result.get('role') == role
                 and result.get('generation_requested') is False and result.get('model_response') is False
                 and result.get('executable') is False and 'usage' in result and result['usage'] is None
                 and isinstance(messages, list) and len(messages) > 0)
        rows.append({'role':role,'http_status':status,'ok':valid,
                     'message_count':len(messages) if isinstance(messages, list) else None,
                     'characters':result.get('characters') if isinstance(result.get('characters'), int) else None})
        if not valid:
            break
    status, after = request('/api/workspace-status')
    end = boundary(after)
    unchanged = status == 200 and start == end
    return {'ok':all(row['ok'] for row in rows) and len(rows) == 3 and unchanged,
            'roles':rows, 'before':start, 'after':end, 'boundaries_unchanged':unchanged,
            'limitation':'Recent proposal IDs only, not a full database count. Concurrent activity can affect checks.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--console-url', default='http://127.0.0.1:8790')
    parser.add_argument('--agent-id', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    relative = 'logs/role-preview-verification.json'
    if subprocess.run(['git','check-ignore','--quiet',relative], cwd=root, check=False).returncode:
        parser.error('Repository logs directory must be Git-ignored')
    started = datetime.now(timezone.utc)
    try:
        report = verify(args.console_url, args.agent_id)
    except (OSError, ValueError, TypeError, AttributeError):
        report = {'ok':False, 'error':'console_verification_failed'}
    report.update(started_at=started.isoformat(), finished_at=datetime.now(timezone.utc).isoformat())
    directory = root/'logs'
    directory.mkdir(exist_ok=True)
    path = directory/('role-preview-'+started.strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    with path.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    print(json.dumps({'ok':report['ok'],'report_path':str(path)}, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
