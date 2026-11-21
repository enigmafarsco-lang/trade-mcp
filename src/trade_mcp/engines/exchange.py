"""Optional CCXT sandbox reader. Never an alternative MT5 order bypass."""
import importlib
import os
import re


def sandbox_client(exchange_id):
    if not re.fullmatch(r'[a-z][a-z0-9_]{1,40}', exchange_id):
        raise ValueError('Invalid exchange ID')
    ccxt = importlib.import_module('ccxt')
    if exchange_id not in ccxt.exchanges:
        raise ValueError('Unknown exchange')
    client = getattr(ccxt, exchange_id)({
        'apiKey': os.environ.get('TRADE_MCP_CCXT_API_KEY', ''),
        'secret': os.environ.get('TRADE_MCP_CCXT_SECRET', ''),
        'password': os.environ.get('TRADE_MCP_CCXT_PASSWORD', ''),
        'enableRateLimit': True, 'timeout': 15000,
        'options': {'maxRetriesOnFailure': 0},
    })
    client.set_sandbox_mode(True)  # Must be first call, before market/data APIs.
    return client


def quote(exchange_id, symbol):
    client = sandbox_client(exchange_id)
    row = client.fetch_ticker(symbol)
    return dict(ok=True, sandbox=True, exchange=exchange_id, symbol=symbol,
                bid=row.get('bid'), ask=row.get('ask'), last=row.get('last'),
                timestamp=row.get('timestamp'))


def balance(exchange_id):
    client = sandbox_client(exchange_id)
    row = client.fetch_balance()
    return dict(ok=True, sandbox=True, exchange=exchange_id,
                free=row.get('free'), used=row.get('used'), total=row.get('total'))


def order_status():
    return dict(ok=False, error='ccxt_order_execution_not_integrated',
                reason='MT5 demo permission does not authorize exchange orders; durable '
                       'intents, exchange risk rules and reconciliation are required',
                execution_authorized=False)
