"""Build a deliberately narrow, sanitized runtime report; never copy raw logs.

Input is a local JSON object containing only documented statuses, counts and
optional fixed reason categories. Unknown fields are rejected, not echoed.
This command never invokes git or MT5.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

STATES = {"pass", "fail", "blocked", "not_run"}
CHECKS = {"compile", "snapshot_fresh", "account_match", "financial_fields",
          "unique_tickets", "position_lifecycle", "floating_net_pnl",
          "guardian", "analysis", "proposal", "order", "three_loss_latch",
          "text_tool_discovery", "technical_builtin", "strategy_rules",
          "broker_engine", "web_engine", "optional_dependency_errors",
          "guard_cost_source", "guardian_offline_tests", "historical_lifecycle_audit",
          "risk_guardrails", "comprehensive_analysis", "paper_ledger", "paper_runner_offline",
          "daytrade_offline_tests"}
# Live-input checks identify the *kind* of evidence, not trading readiness. A
# pass for an advisory function does not establish quote freshness, market
# session, profitability, UI delivery or permission to execute an order.
DAYTRADE_LIVE_CHECKS = {
    "daytrade_mt5_terminal", "daytrade_mt5_symbol", "daytrade_mt5_session",
    "daytrade_mt5_quote", "daytrade_mt5_quote_update", "daytrade_mt5_quote_freshness",
    "daytrade_mt5_m1", "daytrade_mt5_m1_freshness", "daytrade_mcp_discovery",
    "daytrade_live_entry_buy", "daytrade_live_entry_sell", "daytrade_live_scan_patterns",
    "daytrade_live_scan_range", "daytrade_live_exit_plan", "daytrade_live_sizing",
    "daytrade_live_alert_snapshot", "daytrade_live_alert_rules", "daytrade_live_risk_limits",
    "daytrade_live_historical_replay", "daytrade_live_time_machine",
    "daytrade_fixture_news", "daytrade_fixture_stock_facts", "daytrade_fixture_stock_disclosures",
    "daytrade_expanded_offline_tests", "daytrade_probe_offline_tests", "daytrade_upstream_tests", "daytrade_opencode_connection",
    "daytrade_broker_input_http", "daytrade_live_console_http",
    "daytrade_live_browser_monitor", "daytrade_prior_m1_forward",
}
CHECKS |= DAYTRADE_LIVE_CHECKS
DAYTRADE_COUNTS = {"daytrade_pytest_passed", "daytrade_pytest_skipped", "daytrade_pytest_failed",
                   "daytrade_pytest_errors", "daytrade_pytest_total", "daytrade_pytest_exit_code"}
EXPANDED_COUNTS = {"daytrade_expanded_pytest_passed", "daytrade_expanded_pytest_skipped",
                   "daytrade_expanded_pytest_failed", "daytrade_expanded_pytest_errors",
                   "daytrade_expanded_pytest_total", "daytrade_expanded_pytest_exit_code"}
PROBE_COUNTS = {"daytrade_probe_pytest_passed", "daytrade_probe_pytest_skipped",
                "daytrade_probe_pytest_failed", "daytrade_probe_pytest_errors",
                "daytrade_probe_pytest_total", "daytrade_probe_pytest_exit_code"}
UPSTREAM_COUNTS = {"daytrade_upstream_passed", "daytrade_upstream_skipped", "daytrade_upstream_failed",
                   "daytrade_upstream_errors", "daytrade_upstream_total", "daytrade_upstream_exit_code"}
LIVE_COUNTS = {"daytrade_mt5_quote_samples", "daytrade_mt5_m1_bars",
               "daytrade_mt5_segments", "daytrade_mt5_bootstrap_paths", "daytrade_http_cases"}
# Fixed categories only: no free-text errors, account data, prices or paths.
REASONS = {
    "authentication_failed", "connection_unavailable", "terminal_disconnected", "tool_unavailable",
    "schema_mismatch", "tool_error", "symbol_unavailable", "session_unverified", "market_closed",
    "timestamp_unverified", "clock_mismatch", "stale_data", "unchanged_quotes", "empty_data",
    "invalid_data", "insufficient_history", "missing_broker_spec", "missing_costs", "ui_unavailable",
    "dependency_unavailable", "unsafe_operation_excluded", "not_exercisable", "assertion_failed",
    "unexpected_error", "client_unavailable", "feature_not_mt5", "other",
}
COUNTS = {"deals", "positions", "cloud_requests", "orders_submitted",
          "unallocated_records", "balance_operations", "credit_operations",
          "commission_operations", "charge_operations", "other_account_operations",
          "excluded_positive_balance_records", "blocking_unallocated_records",
          "identity_compared_trades", "identity_position_mismatches",
          "identity_side_mismatches", "identity_entry_mismatches",
          "lifecycle_positions_evaluated", "lifecycle_closed_volume_consistent",
          "lifecycle_open_volume_consistent", "lifecycle_incomplete_positions",
          "lifecycle_reversal_positions", "lifecycle_direction_mismatch_positions",
          "lifecycle_volume_mismatch_positions", "lifecycle_active_without_history",
          "lifecycle_opening_time_mismatch_positions"} | DAYTRADE_COUNTS | EXPANDED_COUNTS | PROBE_COUNTS | UPSTREAM_COUNTS | LIVE_COUNTS


def sanitize(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {"commit", "checks", "counts", "reasons"}:
        raise ValueError("Invalid report fields")
    commit = value.get("commit", "")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("A full loaded commit is required")
    checks = value.get("checks", {})
    counts = value.get("counts", {})
    if not isinstance(checks, dict) or set(checks) - CHECKS:
        raise ValueError("Invalid checks")
    if not isinstance(counts, dict) or set(counts) - COUNTS:
        raise ValueError("Invalid counts")
    if any(not isinstance(v, str) or v not in STATES for v in checks.values()):
        raise ValueError("Invalid check state")
    if any(type(v) is not int or not 0 <= v <= 10**9 for v in counts.values()):
        raise ValueError("Invalid count")
    reasons = value.get("reasons", {})
    if (not isinstance(reasons, dict) or set(reasons) - DAYTRADE_LIVE_CHECKS
            or any(not isinstance(reason, str) or reason not in REASONS for reason in reasons.values())
            or any(checks.get(key) not in {"fail", "blocked"} for key in reasons)):
        raise ValueError("Invalid reason category or check state")
    state = checks.get("daytrade_offline_tests", "not_run")
    supplied = set(counts) & DAYTRADE_COUNTS
    if supplied and state == "not_run":
        raise ValueError("Day-trader test counts require a measured check state")
    # A successful pytest run can still contain skips. Require complete,
    # internally consistent counts rather than treating unknowns as zero.
    if state == "pass" and (
        supplied != DAYTRADE_COUNTS or counts["daytrade_pytest_exit_code"] != 0
        or counts["daytrade_pytest_passed"] == 0 or counts["daytrade_pytest_failed"] != 0
        or counts["daytrade_pytest_errors"] != 0
        or counts["daytrade_pytest_passed"] + counts["daytrade_pytest_skipped"]
           != counts["daytrade_pytest_total"]
    ):
        raise ValueError("Incomplete or inconsistent day-trader test evidence")
    expanded_state = checks.get("daytrade_expanded_offline_tests", "not_run")
    expanded_supplied = set(counts) & EXPANDED_COUNTS
    if expanded_supplied and expanded_state == "not_run":
        raise ValueError("Expanded test counts require a measured check state")
    if expanded_state == "pass" and (
        expanded_supplied != EXPANDED_COUNTS or counts["daytrade_expanded_pytest_exit_code"] != 0
        or counts["daytrade_expanded_pytest_passed"] == 0 or counts["daytrade_expanded_pytest_failed"] != 0
        or counts["daytrade_expanded_pytest_errors"] != 0
        or counts["daytrade_expanded_pytest_passed"] + counts["daytrade_expanded_pytest_skipped"]
           != counts["daytrade_expanded_pytest_total"]
    ):
        raise ValueError("Incomplete or inconsistent expanded test evidence")

    probe_state = checks.get("daytrade_probe_offline_tests", "not_run")
    probe_supplied = set(counts) & PROBE_COUNTS
    if probe_supplied and probe_state == "not_run":
        raise ValueError("Probe test counts require a measured check state")
    if probe_state == "pass" and (
        probe_supplied != PROBE_COUNTS or counts["daytrade_probe_pytest_exit_code"] != 0
        or counts["daytrade_probe_pytest_passed"] == 0 or counts["daytrade_probe_pytest_failed"] != 0
        or counts["daytrade_probe_pytest_errors"] != 0
        or counts["daytrade_probe_pytest_passed"] + counts["daytrade_probe_pytest_skipped"]
           != counts["daytrade_probe_pytest_total"]
    ):
        raise ValueError("Incomplete or inconsistent probe test evidence")

    upstream_state = checks.get("daytrade_upstream_tests", "not_run")
    upstream_supplied = set(counts) & UPSTREAM_COUNTS
    if upstream_supplied and upstream_state == "not_run":
        raise ValueError("Upstream test counts require a measured check state")
    if upstream_state == "pass" and (
        upstream_supplied != UPSTREAM_COUNTS or counts["daytrade_upstream_exit_code"] != 0
        or counts["daytrade_upstream_passed"] == 0 or counts["daytrade_upstream_failed"] != 0
        or counts["daytrade_upstream_errors"] != 0
        or counts["daytrade_upstream_passed"] + counts["daytrade_upstream_skipped"]
           != counts["daytrade_upstream_total"]
    ):
        raise ValueError("Incomplete or inconsistent upstream test evidence")

    count_checks = {
        "daytrade_mt5_quote_samples": ("daytrade_mt5_quote",),
        "daytrade_mt5_m1_bars": ("daytrade_mt5_m1",),
        "daytrade_mt5_segments": ("daytrade_live_historical_replay", "daytrade_live_time_machine"),
        "daytrade_mt5_bootstrap_paths": ("daytrade_live_time_machine",),
        "daytrade_http_cases": ("daytrade_broker_input_http",),
    }
    for name, measured_checks in count_checks.items():
        if name in counts and not any(checks.get(check, "not_run") != "not_run" for check in measured_checks):
            raise ValueError(f"Count {name} requires a measured check state")

    def require(check: str, dependencies: tuple[str, ...] = (),
                minimums: tuple[tuple[str, int], ...] = ()) -> None:
        if checks.get(check) == "pass" and (
            any(checks.get(dep) != "pass" for dep in dependencies)
            or any(counts.get(name, -1) < minimum for name, minimum in minimums)
        ):
            raise ValueError(f"Incomplete evidence for {check}")

    require("daytrade_mt5_symbol", ("daytrade_mt5_terminal",))
    require("daytrade_mt5_session", ("daytrade_mt5_terminal", "daytrade_mt5_symbol"))
    require("daytrade_mt5_quote", ("daytrade_mt5_terminal", "daytrade_mt5_symbol"),
            (("daytrade_mt5_quote_samples", 1),))
    require("daytrade_mt5_quote_update", ("daytrade_mt5_quote",), (("daytrade_mt5_quote_samples", 2),))
    require("daytrade_mt5_quote_freshness", ("daytrade_mt5_quote",))
    require("daytrade_mt5_m1", ("daytrade_mt5_terminal", "daytrade_mt5_symbol"),
            (("daytrade_mt5_m1_bars", 2),))
    require("daytrade_mt5_m1_freshness", ("daytrade_mt5_m1",))
    for name in ("daytrade_live_entry_buy", "daytrade_live_entry_sell", "daytrade_live_scan_patterns",
                 "daytrade_live_scan_range", "daytrade_live_historical_replay", "daytrade_live_time_machine"):
        require(name, ("daytrade_mt5_m1", "daytrade_mcp_discovery"))
    for name in ("daytrade_live_exit_plan", "daytrade_live_sizing", "daytrade_live_alert_snapshot",
                 "daytrade_live_alert_rules", "daytrade_live_risk_limits"):
        require(name, ("daytrade_mt5_quote", "daytrade_mcp_discovery"))
    require("daytrade_live_historical_replay", minimums=(("daytrade_mt5_segments", 1),))
    require("daytrade_live_time_machine", minimums=(("daytrade_mt5_segments", 20),
                                                   ("daytrade_mt5_bootstrap_paths", 1)))
    require("daytrade_broker_input_http", ("daytrade_mcp_discovery",), (("daytrade_http_cases", 1),))
    require("daytrade_opencode_connection", ("daytrade_mcp_discovery",))
    require("daytrade_live_console_http", ("daytrade_mt5_terminal", "daytrade_mt5_symbol"))
    require("daytrade_live_browser_monitor", ("daytrade_mt5_quote", "daytrade_live_console_http"))
    require("daytrade_prior_m1_forward", ("daytrade_mt5_quote_freshness", "daytrade_mt5_session"))
    for check in ("daytrade_broker_input_http", "daytrade_live_console_http"):
        if (checks.get(check) == "pass"
                and not any(checks.get(name) == "pass" for name in ("daytrade_mt5_quote", "daytrade_mt5_m1"))):
            raise ValueError("Incomplete broker input evidence for HTTP check")
    result = {"schema": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
              "commit": commit, "checks": {k: checks.get(k, "not_run") for k in sorted(CHECKS)},
              "counts": {k: counts.get(k) for k in sorted(COUNTS)},
              "execution_authorized_by_report": False}
    if reasons:
        result["reasons"] = {key: reasons[key] for key in sorted(reasons)}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.input.stat().st_size > 16384:
        raise SystemExit("Input too large")
    try:
        result = sanitize(json.loads(args.input.read_text(encoding="utf-8")))
    except (ValueError, TypeError):
        raise SystemExit("Invalid report; input contents withheld") from None
    if args.output.exists():
        raise SystemExit("Refusing to overwrite a report")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
