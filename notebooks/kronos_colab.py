# Complete Colab-only export. Run sections in order in Google Colab.


# %%
# COLAB ONLY
import os, sys, subprocess, json, hashlib, time, math
IN_COLAB = 'google.colab' in sys.modules or os.path.exists('/content')
assert IN_COLAB, 'This notebook must run only inside Google Colab runtime.'
print('Runtime Python:', sys.version.split()[0], '| executable:', sys.executable)
if not (3, 10) <= sys.version_info[:2] <= (3, 12):
    raise RuntimeError(
        'This pinned notebook supports Python 3.10-3.12 only. '
        'Python 3.13+ is not validated for these pinned dependencies. '
        'Select a Python 3.11/3.12 hosted runtime if available. '
        'Do not replace the system Python or silently upgrade dependency pins.')
KRONOS_COMMIT = '67b630e67f6a18c9e9be918d9b4337c960db1e9a'
WORK='/content/kronos-work'
REPO=f'{WORK}/Kronos'
os.makedirs(WORK, exist_ok=True)
# Install deps in Colab runtime only
# Install the pinned checkout's actual requirements after verification below.
if not os.path.exists(REPO):
    subprocess.check_call(['git','clone','https://github.com/shiyu-coder/Kronos.git',REPO])
subprocess.check_call(['git','-C',REPO,'fetch','--all'])
subprocess.check_call(['git','-C',REPO,'checkout',KRONOS_COMMIT])
head=subprocess.check_output(['git','-C',REPO,'rev-parse','HEAD'],text=True).strip()
dirty=subprocess.check_output(['git','-C',REPO,'status','--porcelain'],text=True).strip()
assert head==KRONOS_COMMIT and not dirty, 'Pinned clean Kronos checkout required'
# Record loaded distributions before pip; changing them requires a session restart.
from importlib import metadata as _metadata
_KRONOS_RUNTIME_READY = False
_watched = ('torch', 'numpy', 'pandas', 'matplotlib', 'einops', 'huggingface_hub', 'safetensors')
def _installed_version(name):
    try:
        return _metadata.version(name)
    except _metadata.PackageNotFoundError:
        return None
_before_versions = {name: _installed_version(name) for name in _watched}
_loaded_before_install = {name for name in _watched if name in sys.modules}
# Keep pip diagnostics visible; CalledProcessError alone hides the useful cause.
install = subprocess.run([sys.executable,'-m','pip','install','-r',os.path.join(REPO,'requirements.txt')])
if install.returncode:
    raise RuntimeError('Dependency installation failed. Read the pip ERROR lines above; do not run inference cells.')
_changed_loaded = [name for name in _loaded_before_install
                   if _before_versions[name] != _installed_version(name)]
if _changed_loaded:
    raise RuntimeError(
        'Installed versions of loaded packages changed: ' + ', '.join(sorted(_changed_loaded)) +
        '. Use Runtime > Restart session, then rerun setup and validation. '
        'Do not delete the runtime or reinstall torch manually.')
if REPO not in sys.path:
    sys.path.insert(0, REPO)
print('Kronos checkout OK:', head)


# %%
# COLAB ONLY - validate runtime BEFORE uploading data/loading Kronos
_KRONOS_RUNTIME_READY = False
try:
    import torch
    # Exercise a real operation, not just version metadata.
    assert torch.tensor([1.0, 2.0]).sum().item() == 3.0
    from model import Kronos, KronosPredictor, KronosTokenizer
except Exception as exc:
    raise RuntimeError(
        'PyTorch/Kronos runtime validation failed (' + type(exc).__name__ + '). '
        'If the output mentions TORCH_LIBRARY/prims or a partially initialized module, '
        'use Runtime > Restart session, then run setup and this validation cell again. '
        'Do not use importlib.reload(torch), delete sys.modules, or rerun inference. '
        'If it still fails in a fresh session, retain the full error above for diagnosis.'
    ) from exc
_KRONOS_RUNTIME_READY = True
print('Runtime validation OK | torch', torch.__version__, '| CUDA', torch.cuda.is_available())
print('Kronos import OK. You may now upload OHLCV data and run inference.')


# %%
# COLAB ONLY - upload redacted OHLCV JSON
assert globals().get('_KRONOS_RUNTIME_READY') is True, 'Run setup and runtime validation successfully first; restart session after a partial import failure.'
from google.colab import files
import pandas as pd, numpy as np
uploaded = files.upload()  # user selects a redacted JSON file
assert uploaded, 'No file uploaded'
name = next(iter(uploaded))
req = json.loads(uploaded[name].decode('utf-8'))
symbol = str(req.get('symbol','XAUUSD'))
rows = req['rows']
# Basic redaction/sanity checks
forbidden = {'login','password','token','secret','account','journal','credential'}
blob = json.dumps(req).lower()
assert not any(k in blob for k in forbidden), 'Input appears to contain sensitive fields; redact first.'
assert len(rows) >= 180, 'Need >=180 closed contiguous M1 bars'
# validate contiguity
times=[int(r[0]) for r in rows]
assert all(b-a==60 for a,b in zip(times,times[1:])), 'Rows must be contiguous M1 bars'
print('Loaded', len(rows), 'rows for', symbol)


# %%
# COLAB ONLY - actual upstream inference
assert globals().get('_KRONOS_RUNTIME_READY') is True, 'Run setup and runtime validation successfully first; restart session after a partial import failure.'
from model import Kronos, KronosPredictor, KronosTokenizer
# User may change model IDs to provisioned/available compatible checkpoints in Colab.
TOKENIZER_ID = os.environ.get('KRONOS_TOKENIZER_ID','NeoQuasar/Kronos-Tokenizer-base')
MODEL_ID = os.environ.get('KRONOS_MODEL_ID','NeoQuasar/Kronos-small')
tokenizer = KronosTokenizer.from_pretrained(TOKENIZER_ID)
model = Kronos.from_pretrained(MODEL_ID)
predictor = KronosPredictor(model, tokenizer)

def kronos_predict(rows, predictor):
    import pandas as pd
    df = pd.DataFrame(rows, columns=['time','open','high','low','close','volume'])
    historical = pd.Series(pd.to_datetime(df.pop('time'), unit='s', utc=True).dt.tz_localize(None))
    future = pd.Series(pd.date_range(historical.iloc[-1]+pd.Timedelta(minutes=1), periods=60, freq='min'))
    result = predictor.predict(df=df, x_timestamp=historical, y_timestamp=future, pred_len=60, sample_count=1, verbose=False)
    if len(result) != 60:
        raise ValueError('Kronos must return 60 future candles')
    out=[]
    volume_adjustments=[]
    ohlc_adjustments=[]
    for i, (_, row) in enumerate(result.iterrows()):
        o,h,l,c,v = [float(row[k]) for k in ['open','high','low','close','volume']]
        # Diagnose actual model-output defects separately; never silently repair them.
        problems = []
        if not all(math.isfinite(x) for x in (o,h,l,c,v)):
            problems.append('non_finite_model_output')
        else:
            if min(o,h,l,c) <= 0:
                problems.append('non_positive_price')
            # Declared envelope projection: preserve raw values, never change open/close.
            if h < max(o,c,l) or l > min(o,c,h):
                raw = {'open':o, 'high':h, 'low':l, 'close':c}
                h, l = max(h,o,c), min(l,o,c)
                ohlc_adjustments.append({'minute':i+1, 'raw':raw,
                                         'used':{'open':o,'high':h,'low':l,'close':c}})
            if v < 0:
                # Explicit physical-domain projection, NOT a raw model prediction.
                volume_adjustments.append({'minute': i+1, 'raw_volume': v, 'used_volume': 0.0})
                v = 0.0
        if problems:
            raise ValueError(
                'Kronos inference completed but generated an invalid candle at horizon minute '
                + str(i+1) + ': ' + ', '.join(problems) +
                '. No artifact was produced. This is not an HF_TOKEN or download error. '
                'Non-positive or non-finite prices cannot be projected; no repeated sampling.')
        out.append([int(rows[-1][0])+60*(i+1), o,h,l,c,v])
    return {'candles': out, 'source': 'official_KronosPredictor.predict',
            'upstream_commit_expected': KRONOS_COMMIT, 'execution_authorized': False, 'validated_for_trading': False,
            'postprocessing': {'volume_policy':'negative_to_zero_with_raw_audit',
                               'ohlc_policy':'envelope_preserve_open_close_with_raw_audit',
                               'prices_modified':bool(ohlc_adjustments),
                               'ohlc_adjustments':ohlc_adjustments,
                               'volume_adjustments':volume_adjustments}}

# held-out evaluation on final 60 bars (never given to predictor for that origin)
training, holdout = rows[:-60], rows[-60:]
forecast = kronos_predict(training, predictor)
metrics=[]
for horizon in (15,30,60):
    actual = float(holdout[horizon-1][4])
    predicted = float(forecast['candles'][horizon-1][4])
    baseline = float(training[-1][4])
    metrics.append({'minutes_ahead':horizon,'absolute_error':abs(predicted-actual),'no_change_absolute_error':abs(baseline-actual)})
evaluation = {'source':forecast['source'],'holdout_candles':60,'metrics':metrics,'independent_origins':1,'validated_for_trading':False,'warning':'One holdout origin is not profitability or generalization proof', 'postprocessing':forecast['postprocessing']}
print(json.dumps(evaluation, indent=2))


# %%
# COLAB ONLY - build trade-mcp compatible artifact
for m in evaluation['metrics']:
    if not (math.isfinite(m['absolute_error']) and math.isfinite(m['no_change_absolute_error'])):
        raise ValueError('Invalid metric')
    if m['absolute_error'] >= m['no_change_absolute_error']:
        raise ValueError('Failed baseline screen; do not fabricate a passing artifact')
live_forecast = kronos_predict(rows, predictor)  # uses all provided closed bars as context
artifact = {
  'symbol': symbol,
  'timeframe': 'M1',
  'forecast_origin': int(rows[-1][0]) + 60,
  'source': 'official_KronosPredictor.predict',
  'upstream_commit_expected': KRONOS_COMMIT,
  'candles': live_forecast['candles'],
  'holdout_evaluation': evaluation,
  'postprocessing': live_forecast['postprocessing'],
  'execution_authorized': False,
  'validated_for_trading': False,
}
out_path = f'/content/kronos_forecast_{symbol}_M1.json'
with open(out_path,'w',encoding='utf-8') as f: json.dump(artifact, f, ensure_ascii=False)
print('Wrote', out_path)


# %%
# COLAB ONLY
from google.colab import files
files.download(out_path)
print('Downloaded. Transfer securely to local MT5 machine; keep owner-only; do not commit secrets/data.')
