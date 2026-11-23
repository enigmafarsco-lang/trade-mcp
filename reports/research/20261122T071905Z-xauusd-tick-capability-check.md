# XAUUSD historical tick capability check (read-only)

- Branch: `arena/f81b6b27-trade-mcp`
- HEAD at check: `f73cc84`
- Executed command (exact): `.venv/bin/python scripts/xauusd_research_check.py "logs/research-inputs/xauusd-capabilities-20261122T071556Z.json"`
- Trading: OFF. Cloud budget: revoked. No installs/Wine-MT5 restarts/orders/paid calls/Binance requests.

## Discovery (executed)
- Script result (`logs/research-inputs/xauusd-capabilities-20261122T071556Z.json`, owner-only, gitignored): `status=candidate_advertised`.
- Candidate: `get_chart_ticks_history` (read-only per tool description: "read-only and never places, modifies or cancels orders"). Schema params: `datetime_from`(string, required), `datetime_to`(string, required), `symbol`(string, required), `limit`(integer, optional). `additionalProperties=false`.
- Discovery calls made by script: `initialize`, `tools/list`. Historical bid/ask not verified by discovery; backtest not executed.

## Bounded historical tick sample attempt (executed once)
- Target: `XAUUSD_i` (resolved from marketwatch symbols; XAUUSD exact name not present in marketwatch list). Window: `2026-11-20T19:23:00Z` to `2026-11-20T19:28:00Z` (5 minutes, last known closed session), `limit=10000`.
- Result: **request failed**. A local execution-script argument-construction bug included `tool` in call arguments; the MCP call was made once and failed. Per instruction, **no retry/fallback** performed.
- Historical bid/ask verified: **no**. Actual data sample obtained: **no**. Raw response/body: not preserved as valid sample; error withheld from sanitized report.

## Verified blocker
- No verified historical XAUUSD Bid/Ask tick sample obtained. Capability candidate is advertised and read-only by schema/description, but bounded sample verification was not completed due to the single failed request (no-retry constraint). No data fabricated.

## Sanitized evidence
- This report only. Raw discovery/sample artifacts remain owner-only under gitignored `logs/research-inputs/` (0600).
