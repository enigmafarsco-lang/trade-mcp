"""Explicit read-only OpenRouter access check. Never generates or changes config."""
import json
import os
import urllib.error
import urllib.request

from .opencode import NoRedirect


def check_openrouter(key):
    if not key:
        return {'ok':False, 'error':'api_key_required', 'generation_requested':False}
    def get(path):
        req = urllib.request.Request('https://openrouter.ai/api/v1/'+path,
            headers={'Authorization':'Bearer '+key}, method='GET')
        with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as response:
            return json.loads(response.read(2_000_000))
    try:
        data = get('key').get('data') or {}
        quota = data.get('free_model_daily_requests') or {}
        # Never return key labels, account identity, billing or raw responses.
        safe_quota = {name:value if isinstance(value, int) and not isinstance(value, bool)
                      and value >= 0 else None for name in ('used','limit','remaining')
                      for value in [quota.get(name)]}
        models = get('models').get('data') or []
        free = []
        for model in models:
            pricing = model.get('pricing') or {}
            if not str(model.get('id', '')).endswith(':free'):
                continue
            # Reject missing or nonzero prices; don't infer entitlement from names.
            try:
                zero = all(float(pricing[k]) == 0 for k in ('prompt','completion'))
            except (KeyError, ValueError, TypeError):
                continue
            if zero:
                free.append(model['id'])
        return {'ok':True, 'generation_requested':False, 'free_daily_requests':safe_quota,
                'catalogue_free_model_ids':sorted(free),
                'warning':'Catalogue pricing is not entitlement or proof of generation success. No model selected.'}
    except urllib.error.HTTPError as exc:
        return {'ok':False, 'error':'access_check_http_error', 'http_status':exc.code,
                'generation_requested':False}
    except (OSError, ValueError, TypeError, AttributeError):
        return {'ok':False, 'error':'access_check_failed', 'generation_requested':False}


if __name__ == '__main__':
    # Key must be provisioned locally by the user, not in CLI arguments or chat.
    print(json.dumps(check_openrouter(os.environ.get('OPENROUTER_API_KEY', '')), indent=2))
