"""Build a deliberately narrow, sanitized runtime report; never copy raw logs.

Input is a local JSON object containing only the documented status/count fields.
Unknown fields are rejected, not echoed. This command never invokes git or MT5.
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
DAYTRADE_COUNTS = {"daytrade_pytest_passed", "daytrade_pytest_skipped", "daytrade_pytest_failed",
                   "daytrade_pytest_errors", "daytrade_pytest_total", "daytrade_pytest_exit_code"}
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
          "lifecycle_opening_time_mismatch_positions"} | DAYTRADE_COUNTS


def sanitize(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {"commit", "checks", "counts"}:
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
    return {"schema": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "commit": commit, "checks": {k: checks.get(k, "not_run") for k in sorted(CHECKS)},
            "counts": {k: counts.get(k) for k in sorted(COUNTS)},
            "execution_authorized_by_report": False}


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
