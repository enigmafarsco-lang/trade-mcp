"""SQLite journal: trade proposals awaiting confirmation, executed trades, notes."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id          TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    status      TEXT NOT NULL,
    symbol      TEXT,
    payload     TEXT NOT NULL,
    result      TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT,
    created_at  REAL NOT NULL,
    ticket      INTEGER,
    symbol      TEXT,
    side        TEXT,
    volume      REAL,
    price       REAL,
    sl          REAL,
    tp          REAL,
    comment     TEXT,
    raw         TEXT
);
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    symbol     TEXT,
    kind       TEXT,
    text       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
"""


class Store:
    def __init__(self, path: str = "~/.local/share/trade-mcp/journal.sqlite3"):
        self.path = Path(path).expanduser()
        if self.path.parent and str(self.path.parent) not in {"", "."}:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):  # pragma: no cover
            self._conn.close()

    # ------------------------------------------------------------ proposals
    def create_proposal(self, payload: dict[str, Any], ttl: int = 180) -> dict[str, Any]:
        pid = uuid.uuid4().hex[:12]
        now = time.time()
        expires = now + max(10, int(ttl))
        self._conn.execute(
            "INSERT INTO proposals (id, created_at, expires_at, status, symbol, payload) "
            "VALUES (?,?,?,?,?,?)",
            (pid, now, expires, "pending", payload.get("symbol"), json.dumps(payload, default=str)),
        )
        self._conn.commit()
        return {"id": pid, "created_at": now, "expires_at": expires, "status": "pending"}

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        if data.get("result"):
            with contextlib.suppress(TypeError, ValueError):
                data["result"] = json.loads(data["result"])
        data["expired"] = time.time() > float(data["expires_at"])
        return data

    def mark_proposal(self, proposal_id: str, status: str, result: Any = None) -> None:
        self._conn.execute(
            "UPDATE proposals SET status = ?, result = ? WHERE id = ?",
            (status, json.dumps(result, default=str) if result is not None else None, proposal_id),
        )
        self._conn.commit()

    def list_proposals(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM proposals ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            out.append(item)
        return out

    # --------------------------------------------------------------- trades
    def record_trade(self, proposal_id: str | None, payload: dict[str, Any], result: Any) -> None:
        self._conn.execute(
            "INSERT INTO trades (proposal_id, created_at, ticket, symbol, side, volume, price,"
            " sl, tp, comment, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                proposal_id,
                time.time(),
                (result or {}).get("ticket") if isinstance(result, dict) else None,
                payload.get("symbol"),
                payload.get("side"),
                payload.get("volume"),
                (result or {}).get("price") if isinstance(result, dict) else None,
                payload.get("sl"),
                payload.get("tp"),
                payload.get("comment"),
                json.dumps(result, default=str),
            ),
        )
        self._conn.commit()

    def list_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- notes
    def add_note(self, text: str, symbol: str | None = None, kind: str = "note") -> dict[str, Any]:
        cur = self._conn.execute(
            "INSERT INTO notes (created_at, symbol, kind, text) VALUES (?,?,?,?)",
            (time.time(), symbol, kind, text),
        )
        self._conn.commit()
        return {"id": cur.lastrowid, "created_at": time.time(), "symbol": symbol, "kind": kind}

    def list_notes(self, limit: int = 20, symbol: str | None = None) -> list[dict[str, Any]]:
        if symbol:
            rows = self._conn.execute(
                "SELECT * FROM notes WHERE symbol = ? ORDER BY created_at DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
