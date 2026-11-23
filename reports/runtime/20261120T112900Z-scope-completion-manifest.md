# Archive manifest — authorized scope continuation `20261120T112900Z`

- **Branch:** `arena/f81b6b27-trade-mcp`
- **Base commit:** `76c7f01ccdfd1226e0cfc69abdee749282a434bd`
- **Prior reports/commits preserved:** all previous `reports/runtime/*` and local commits (`302ad14`, `97bea86`, `f659d55`, `cb57fbb`, `76c7f01`, etc.) unchanged.

Local raw archive (git-ignored, owner-only `700/600`): `logs/session-20261120T112900Z/`

| Category | Local raw (L) | Sanitized report (S) | Notes |
|---|---:|---:|---|
| Changed files list, pytest full, ruff lint | `changed_files.txt`, `pytest_full.txt`, `lint.txt` | S (JSON/MD: A) | No secrets |
| Cloud validation (activate → 1 explain attempt → revoke) | `cloud_validation.json` | S (JSON/MD: C) | Endpoint/model/attempts/result/usage only; no key, no raw body, no credentials |
| DEMO acceptance fresh preflight (blocked) | `demo_preflight_blocked.json` | S (JSON/MD: D) | No raw account identifiers/financial records; broker flat/guard clean stated |
| Code/tests/docs changes | git diff (local) | committed explicitly (see commit) | Intended files only |
| Credentials, `.env`, keys, tokens, DBs, journals raw, raw provider payloads, logs (other than sanitized summaries above) | — | **excluded** | never committed |

**Sanitized files in this report set:** `20261120T112900Z-scope-completion-session.json`, `.md`, `-manifest.md`.

`execution_authorized_by_report: false`.
