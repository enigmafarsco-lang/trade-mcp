"""Strict reader for the optional read-only MQL5 cost snapshot.

Deliberately not an execution data source yet: a bounded history may omit opening
legs and separately booked account charges. Never infers ownership or fills costs.
"""
from __future__ import annotations

import csv
import math
import time
from pathlib import Path
from typing import Any


class CostSnapshotError(ValueError):
    """Snapshot is unavailable, stale, mismatched or malformed."""


def read_cost_snapshot(
    path: str | Path, *, login: str, server: str, currency: str,
    now: float | None = None, max_age: float = 30,
) -> dict[str, Any]:
    """Read a bounded snapshot; all identity arguments must come from live account data."""
    try:
        return _read(Path(path), login, server, currency, time.time() if now is None else now,
                     max_age)
    except (OSError, ValueError, OverflowError, csv.Error) as exc:
        raise CostSnapshotError("Cost snapshot unavailable or invalid") from exc


def _read(path: Path, login: str, server: str, currency: str,
          now: float, max_age: float) -> dict[str, Any]:
    if not all((login, server, currency)) or not math.isfinite(max_age) or max_age <= 0:
        raise ValueError("Invalid identity or age")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Oversized snapshot")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.reader(stream, delimiter="\t", strict=True)
        meta = next(rows, [])
        if len(meta) != 9 or meta[:2] != ["meta", "1"]:
            raise ValueError("Invalid header")
        if meta[2:5] != [str(login), server, currency]:
            raise ValueError("Account mismatch")
        generated, start, end, count = map(int, meta[5:])
        if not math.isfinite(now) or not 0 <= now - generated <= max_age:
            raise ValueError("Stale snapshot")
        if not 0 < start < end or not 0 <= count <= 100000:
            raise ValueError("Invalid bounds")
        deals = []
        seen = set()
        for row in rows:
            if len(deals) >= count or len(row) != 10 or row[0] != "deal":
                raise ValueError("Invalid row count or shape")
            ticket, position, timestamp, entry, kind = map(int, row[1:6])
            amounts = list(map(float, row[6:]))
            if (ticket <= 0 or position < 0 or ticket in seen or
                    not start <= timestamp <= end or entry not in range(4) or kind < 0 or
                    not all(math.isfinite(value) for value in amounts)):
                raise ValueError("Invalid deal")
            seen.add(ticket)
            deals.append(dict(ticket=str(ticket), position_id=str(position), time=timestamp,
                              entry=entry, type=kind, **dict(zip(
                                  ("profit", "swap", "commission", "fee"), amounts,
                                  strict=True)), costs_complete=True))
        if len(deals) != count:
            raise ValueError("Incomplete snapshot")
    return dict(source="mql5_cost_snapshot_v1", generated_at=generated,
                date_from=start, date_to=end, deals=deals,
                execution_eligible=False)
