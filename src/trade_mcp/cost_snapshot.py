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
        if not meta or meta[0] != "meta" or meta[1:2] not in (["1"], ["2"]):
            raise ValueError("Invalid header")
        version = int(meta[1])
        if len(meta) != (9 if version == 1 else 10):
            raise ValueError("Invalid header length")
        if meta[2:5] != [str(login), server, currency]:
            raise ValueError("Account mismatch")
        generated, start, end, count = map(int, meta[5:9])
        position_count = int(meta[9]) if version == 2 else 0
        if not 0 <= position_count <= 10000:
            raise ValueError("Invalid position count")
        if not math.isfinite(now) or not 0 <= now - generated <= max_age:
            raise ValueError("Stale snapshot")
        if not 0 < start < end or not 0 <= count <= 100000:
            raise ValueError("Invalid bounds")
        deals = []
        seen = set()
        positions = []
        position_ids, position_tickets = set(), set()
        for row in rows:
            if version == 2 and row and row[0] == "position":
                if len(deals) != count or len(positions) >= position_count or len(row) != 11:
                    raise ValueError("Invalid position row")
                ticket, identifier, kind, timestamp = map(int, row[1:5])
                volume, profit, swap, sl, tp = map(float, row[5:10])
                if (ticket <= 0 or identifier <= 0 or kind not in (0, 1) or timestamp <= 0 or
                        ticket in position_tickets or identifier in position_ids or volume <= 0 or
                        min(sl, tp) < 0 or not all(math.isfinite(v) for v in
                                                 (volume, profit, swap, sl, tp))):
                    raise ValueError("Invalid position values")
                position_tickets.add(ticket)
                position_ids.add(identifier)
                positions.append(dict(ticket=str(ticket), identifier=str(identifier),
                                      type=kind, time=timestamp, volume=volume, profit=profit,
                                      swap=swap, sl=sl, tp=tp, comment=_comment(row[10]),
                                      pnl_complete=True))
                continue
            if (positions or len(deals) >= count or len(row) != (10 if version == 1 else 12)
                    or row[0] != "deal"):
                raise ValueError("Invalid row count or shape")
            ticket, position, timestamp, entry, kind = map(int, row[1:6])
            amounts = list(map(float, row[6:10]))
            if (ticket <= 0 or position < 0 or ticket in seen or
                    not start <= timestamp <= end or entry not in range(4) or kind < 0 or
                    not all(math.isfinite(value) for value in amounts)):
                raise ValueError("Invalid deal")
            seen.add(ticket)
            deals.append(dict(ticket=str(ticket), position_id=str(position), time=timestamp,
                              entry=entry, type=kind, **dict(zip(
                                  ("profit", "swap", "commission", "fee"), amounts,
                                  strict=True)), costs_complete=True))
            if version == 2:
                volume = float(row[10])
                if not math.isfinite(volume) or volume < 0 or (kind in (0, 1) and volume <= 0):
                    raise ValueError("Invalid deal volume")
                deals[-1].update(volume=volume, comment=_comment(row[11]))
        if len(deals) != count or len(positions) != position_count:
            raise ValueError("Incomplete snapshot")
    return dict(source=f"mql5_cost_snapshot_v{version}", generated_at=generated,
                date_from=start, date_to=end, deals=deals, positions=positions,
                execution_eligible=False)


def _comment(value: str) -> str:
    if len(value) > 2048 or len(value) % 2 or any(c not in "0123456789abcdefABCDEF" for c in value):
        raise ValueError("Invalid encoded comment")
    return bytes.fromhex(value).decode("utf-8", errors="strict")
