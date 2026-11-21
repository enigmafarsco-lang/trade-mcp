"""SQLite journal: trade proposals awaiting confirmation, executed trades, notes."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_cash_guard (account TEXT PRIMARY KEY, state TEXT NOT NULL);
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
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    symbols     TEXT NOT NULL,
    max_lot     REAL NOT NULL,
    max_risk    REAL NOT NULL,
    mode        TEXT NOT NULL,
    hours       TEXT,
    style       TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL,
    last_run    REAL,
    last_result TEXT
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    report TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_usage (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id              TEXT NOT NULL,
    created_at              REAL NOT NULL,
    request_kind            TEXT NOT NULL,
    attempt_index           INTEGER NOT NULL DEFAULT 1,
    is_fallback             INTEGER NOT NULL DEFAULT 0,
    provider                TEXT,
    model                   TEXT,
    variant                 TEXT,
    billing_project         TEXT,
    ok                      INTEGER NOT NULL,
    error_code              TEXT,
    http_status             INTEGER,
    input_tokens            INTEGER,
    cached_input_tokens     INTEGER,
    output_tokens           INTEGER,
    reasoning_tokens        INTEGER,
    cost_amount             REAL,
    cost_currency           TEXT,
    usage_source            TEXT NOT NULL,
    cost_source             TEXT NOT NULL,
    accounting_convention   TEXT NOT NULL,
    latency_ms              INTEGER,
    retry_after_ms          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_model_usage_created ON model_usage(created_at);
CREATE TABLE IF NOT EXISTS model_budget (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    window_id           TEXT,
    state               TEXT NOT NULL DEFAULT 'not_activated',
    activated_at        REAL,
    first_attempt_at    REAL,
    deadline            REAL,
    max_attempts        INTEGER NOT NULL DEFAULT 5,
    used_attempts       INTEGER NOT NULL DEFAULT 0,
    last_denied_at      REAL,
    last_denied_reason  TEXT,
    cooldown_until      REAL,
    cooldown_reason     TEXT,
    updated_at          REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
"""


class Store:
    def __init__(self, path: str = "~/.local/share/trade-mcp/journal.sqlite3"):
        self.path = Path(path).expanduser()
        if self.path.parent and str(self.path.parent) not in {"", "."}:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit mode lets budget reservations use an explicit
        # BEGIN IMMEDIATE transaction without colliding with other writes.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO model_budget (id, state, max_attempts, used_attempts, updated_at) "
            "VALUES (1, 'not_activated', 5, 0, ?)",
            (time.time(),),
        )
        self._conn.commit()
        self._write_lock = threading.RLock()

    def cash_guard_update(self, account, change):
        """Atomic durable guard mutation; callers never hold network calls here."""
        with self._write_lock:
            self._conn.execute('BEGIN IMMEDIATE')
            try:
                row = self._conn.execute('SELECT state FROM demo_cash_guard WHERE account=?', (account,)).fetchone()
                state = json.loads(row['state']) if row else {'latched':False, 'streak':0, 'intents':{}}
                result = change(state)
                self._conn.execute('INSERT OR REPLACE INTO demo_cash_guard VALUES (?,?)',
                                   (account,json.dumps(state, allow_nan=False)))
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

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

    def claim_proposal(self, proposal_id: str) -> bool:
        """Atomically reserve an unexpired pending proposal across MCP/web processes."""
        cursor = self._conn.execute(
            "UPDATE proposals SET status='executing' WHERE id=? AND status='pending' AND expires_at>?",
            (proposal_id, time.time()),
        )
        self._conn.commit()
        return cursor.rowcount == 1

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

    # --------------------------------------------------------------- agents
    def create_agent(self, spec: dict[str, Any]) -> dict[str, Any]:
        aid = uuid.uuid4().hex[:10]
        self._conn.execute(
            "INSERT INTO agents (id, name, symbols, max_lot, max_risk, mode, hours, style, "
            "enabled, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                aid,
                str(spec.get("name") or "agent"),
                ",".join(spec.get("symbols") or []),
                float(spec.get("max_lot") or 0.01),
                float(spec.get("max_risk") or 0.5),
                str(spec.get("mode") or "advisory"),
                str(spec.get("hours") or ""),
                str(spec.get("style") or ""),
                1 if spec.get("enabled", True) else 0,
                time.time(),
            ),
        )
        self._conn.commit()
        return self.get_agent(aid) or {}

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            return None
        return self._agent_row(row)

    def list_agents(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
        return [self._agent_row(r) for r in rows]

    def update_agent(self, agent_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        current = self.get_agent(agent_id)
        if current is None:
            return None
        merged = {**current, **{k: v for k, v in changes.items() if v is not None}}
        self._conn.execute(
            "UPDATE agents SET name=?, symbols=?, max_lot=?, max_risk=?, mode=?, hours=?, "
            "style=?, enabled=? WHERE id=?",
            (
                merged["name"],
                ",".join(merged.get("symbols") or []),
                float(merged.get("max_lot") or 0.01),
                float(merged.get("max_risk") or 0.5),
                merged.get("mode") or "advisory",
                merged.get("hours") or "",
                merged.get("style") or "",
                1 if merged.get("enabled") else 0,
                agent_id,
            ),
        )
        self._conn.commit()
        return self.get_agent(agent_id)

    def delete_agent(self, agent_id: str) -> None:
        self._conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        self._conn.commit()

    def mark_agent_run(self, agent_id: str, result: dict[str, Any] | str) -> None:
        text = result[:8000] if isinstance(result, str) else json.dumps(result, default=str)
        now = time.time()
        self._conn.execute(
            "UPDATE agents SET last_run = ?, last_result = ? WHERE id = ?",
            (now, text, agent_id),
        )
        self._conn.execute("INSERT INTO agent_runs(agent_id,created_at,report) VALUES(?,?,?)",
                           (agent_id, now, text))
        self._conn.commit()

    def agent_run_history(self, limit=30):
        rows = self._conn.execute("SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?",
                                  (max(1, min(int(limit), 100)),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item['report'] = json.loads(item['report'])
            except (ValueError, TypeError):
                item['report'] = {}
            result.append(item)
        return result

    # ----------------------------------------------------------- model usage
    def record_model_usage(self, row: dict[str, Any]) -> int:
        """Store metadata for one actual provider attempt.

        Only the fixed columns below are accepted. Prompt/response text and
        credentials are intentionally not part of the schema.
        """
        usage_source = str(row.get("usage_source") or "unknown")
        if usage_source not in {"provider", "partial", "unknown"}:
            usage_source = "unknown"
        cost_source = str(row.get("cost_source") or "unknown")
        if cost_source not in {"provider", "unknown"}:
            cost_source = "unknown"
        convention = str(row.get("accounting_convention") or "unknown")
        if convention not in {"openai_inclusive", "additive", "unknown"}:
            convention = "unknown"

        def integer(name: str) -> int | None:
            value = row.get(name)
            if value is None or isinstance(value, bool):
                return None
            try:
                number = int(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return number if number >= 0 else None

        def number(name: str) -> float | None:
            value = row.get(name)
            if value is None or isinstance(value, bool):
                return None
            try:
                parsed = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if parsed != parsed or parsed in {float("inf"), float("-inf")} or parsed < 0:
                return None
            return parsed

        cursor = self._conn.execute(
            "INSERT INTO model_usage (request_id, created_at, request_kind, attempt_index, "
            "is_fallback, provider, model, variant, billing_project, ok, error_code, http_status, "
            "input_tokens, cached_input_tokens, output_tokens, reasoning_tokens, cost_amount, "
            "cost_currency, usage_source, cost_source, accounting_convention, latency_ms, "
            "retry_after_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(row.get("request_id") or uuid.uuid4().hex),
                float(row.get("created_at") or time.time()),
                str(row.get("request_kind") or "unknown"),
                max(1, integer("attempt_index") or 1),
                1 if row.get("is_fallback") else 0,
                str(row["provider"]) if row.get("provider") else None,
                str(row["model"]) if row.get("model") else None,
                str(row["variant"]) if row.get("variant") else None,
                str(row["billing_project"]) if row.get("billing_project") else None,
                1 if row.get("ok") else 0,
                str(row["error_code"]) if row.get("error_code") else None,
                integer("http_status"),
                integer("input_tokens"),
                integer("cached_input_tokens"),
                integer("output_tokens"),
                integer("reasoning_tokens"),
                number("cost_amount"),
                str(row["cost_currency"]).upper() if row.get("cost_currency") else None,
                usage_source,
                cost_source,
                convention,
                integer("latency_ms"),
                integer("retry_after_ms"),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid or 0)

    def list_model_usage(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        rows = self._conn.execute(
            "SELECT * FROM model_usage ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def model_usage_summary(self) -> dict[str, Any]:
        """Aggregate known values only; unknown rows stay visible as counts."""
        rows = [dict(row) for row in self._conn.execute(
            "SELECT * FROM model_usage ORDER BY id"
        ).fetchall()]
        fields = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens")
        summary_fields: dict[str, dict[str, int]] = {}
        for field in fields:
            known = [row[field] for row in rows if row.get(field) is not None]
            summary_fields[field] = {
                "known_total": int(sum(known)),
                "known_rows": len(known),
                "unknown_rows": len(rows) - len(known),
            }
        cost_buckets: dict[str | None, dict[str, Any]] = {}
        for row in rows:
            amount = row.get("cost_amount")
            if amount is None:
                continue
            currency = row.get("cost_currency")
            bucket = cost_buckets.setdefault(
                currency, {"currency": currency, "known_total": 0.0, "known_rows": 0}
            )
            bucket["known_total"] += float(amount)
            bucket["known_rows"] += 1
        conventions: dict[str, int] = {}
        providers: dict[str, int] = {}
        models: dict[str, int] = {}
        kinds: dict[str, int] = {}
        projects: dict[str, int] = {}
        for row in rows:
            for key, target in (
                ("accounting_convention", conventions),
                ("provider", providers),
                ("model", models),
                ("request_kind", kinds),
                ("billing_project", projects),
            ):
                value = str(row.get(key) or "unknown")
                target[value] = target.get(value, 0) + 1
        return {
            "requests": len(rows),
            "ok_rows": sum(1 for row in rows if row.get("ok")),
            "failed_rows": sum(1 for row in rows if not row.get("ok")),
            "usage_unknown_rows": sum(1 for row in rows if row.get("usage_source") == "unknown"),
            "usage_partial_rows": sum(1 for row in rows if row.get("usage_source") == "partial"),
            "cost_unknown_rows": sum(1 for row in rows if row.get("cost_amount") is None),
            "fields": summary_fields,
            "cost": sorted(
                cost_buckets.values(),
                key=lambda item: (item["currency"] is None, str(item["currency"] or "")),
            ),
            "accounting_conventions": conventions,
            "providers": providers,
            "models": models,
            "request_kinds": kinds,
            "billing_projects": projects,
            "last_request_at": max((row.get("created_at") or 0 for row in rows), default=None),
            "note": "Only dedicated web-provider attempts are counted here; unknown values are NULL, not zero.",
        }

    # ----------------------------------------------------------- model budget
    def _budget_row(self) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM model_budget WHERE id = 1").fetchone()
        if row is None:
            self._conn.execute(
                "INSERT OR IGNORE INTO model_budget (id, state, max_attempts, used_attempts, updated_at) "
                "VALUES (1, 'not_activated', 5, 0, ?)",
                (time.time(),),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM model_budget WHERE id = 1").fetchone()
        return dict(row or {})

    @staticmethod
    def _budget_state(row: dict[str, Any], now: float, max_attempts: int) -> str:
        state = str(row.get("state") or "not_activated")
        if state != "active":
            return state
        if int(row.get("used_attempts") or 0) >= max_attempts:
            return "exhausted"
        deadline = row.get("deadline")
        if deadline is not None and now >= float(deadline):
            return "expired"
        return "active"

    def _budget_status_from_row(
        self, row: dict[str, Any], now: float, max_attempts: int
    ) -> dict[str, Any]:
        state = self._budget_state(row, now, max_attempts)
        used = int(row.get("used_attempts") or 0)
        deadline = row.get("deadline")
        cooldown_until = row.get("cooldown_until")
        cooldown_active = bool(cooldown_until is not None and now < float(cooldown_until))
        reason = None
        if state != "active":
            reason = f"test_budget_{state}" if state != "not_activated" else "test_budget_not_activated"
        elif cooldown_active:
            reason = "provider_cooldown"
        return {
            "allowed": state == "active" and not cooldown_active,
            "state": state,
            "reason": reason,
            "window_id": row.get("window_id"),
            "activated_at": row.get("activated_at"),
            "first_attempt_at": row.get("first_attempt_at"),
            "deadline": deadline,
            "max_attempts": max_attempts,
            "used_attempts": used,
            "remaining_attempts": max(0, max_attempts - used),
            "seconds_remaining": max(0.0, float(deadline) - now) if deadline is not None else None,
            "cooldown_until": cooldown_until,
            "cooldown_reason": row.get("cooldown_reason"),
            "retry_after_ms": int(max(0.0, float(cooldown_until) - now) * 1000) if cooldown_active else None,
            "last_denied_at": row.get("last_denied_at"),
            "last_denied_reason": row.get("last_denied_reason"),
            "updated_at": row.get("updated_at"),
        }

    def model_budget_status(
        self, *, now: float | None = None, max_attempts: int = 5
    ) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        max_attempts = max(1, min(int(max_attempts), 5))
        with self._write_lock:
            row = self._budget_row()
            return self._budget_status_from_row(row, now, max_attempts)

    def _deny_budget(self, reason: str, now: float) -> None:
        self._conn.execute(
            "UPDATE model_budget SET last_denied_at=?, last_denied_reason=?, updated_at=? WHERE id=1",
            (now, reason, now),
        )

    def activate_model_budget(
        self, *, now: float | None = None, window_seconds: float = 300.0,
        max_attempts: int = 5,
    ) -> dict[str, Any]:
        """Explicit activation only. An already-active window is never reset."""
        now = time.time() if now is None else float(now)
        window_seconds = max(0.01, float(window_seconds))
        max_attempts = max(1, min(int(max_attempts), 5))
        with self._write_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._budget_row()
                current = self._budget_status_from_row(row, now, max_attempts)
                if current["state"] == "active":
                    self._conn.commit()
                    return {**current, "activated": False, "already_active": True}
                window_id = uuid.uuid4().hex
                self._conn.execute(
                    "UPDATE model_budget SET state='active', window_id=?, activated_at=?, "
                    "first_attempt_at=NULL, deadline=NULL, max_attempts=?, used_attempts=0, "
                    "last_denied_at=NULL, last_denied_reason=NULL, updated_at=? WHERE id=1",
                    (window_id, now, max_attempts, now),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        status = self.model_budget_status(now=now, max_attempts=max_attempts)
        return {**status, "activated": True, "already_active": False}

    def reserve_model_budget(
        self, *, now: float | None = None, window_seconds: float = 300.0,
        max_attempts: int = 5,
    ) -> dict[str, Any]:
        """Atomically authorise one actual provider attempt.

        The 300-second window starts at the first authorised attempt, not at
        activation. A reserved request may finish after the deadline.
        """
        now = time.time() if now is None else float(now)
        window_seconds = max(0.01, float(window_seconds))
        max_attempts = max(1, min(int(max_attempts), 5))
        with self._write_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._budget_row()
                status = self._budget_status_from_row(row, now, max_attempts)
                if not status["allowed"]:
                    self._deny_budget(status["reason"] or "test_budget_not_activated", now)
                    self._conn.commit()
                    return {**status, "reserved": False}
                used = int(row.get("used_attempts") or 0) + 1
                first_attempt = row.get("first_attempt_at")
                deadline = row.get("deadline")
                if first_attempt is None:
                    first_attempt = now
                    deadline = now + window_seconds
                self._conn.execute(
                    "UPDATE model_budget SET state=?, first_attempt_at=?, deadline=?, "
                    "used_attempts=?, updated_at=? WHERE id=1",
                    ("exhausted" if used >= max_attempts else "active",
                     first_attempt, deadline, used, now),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {
            "allowed": True,
            "reserved": True,
            "reason": None,
            "window_id": row.get("window_id"),
            "first_attempt_at": first_attempt,
            "deadline": deadline,
            "attempt_index": used,
            "max_attempts": max_attempts,
            "used_attempts": used,
            "remaining_attempts": max(0, max_attempts - used),
            "seconds_remaining": max(0.0, float(deadline) - now) if deadline is not None else None,
        }

    def note_model_budget_cooldown(
        self, retry_after_ms: int | None, reason: str = "provider_cooldown",
        *, now: float | None = None,
    ) -> None:
        if not isinstance(retry_after_ms, int) or isinstance(retry_after_ms, bool):
            return
        if retry_after_ms < 0:
            return
        now = time.time() if now is None else float(now)
        until = now + retry_after_ms / 1000.0
        with self._write_lock:
            self._conn.execute(
                "UPDATE model_budget SET cooldown_until=?, cooldown_reason=?, updated_at=? WHERE id=1",
                (until, reason, now),
            )
            self._conn.commit()

    def revoke_model_budget(self, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._write_lock:
            self._conn.execute(
                "UPDATE model_budget SET state='revoked', updated_at=? WHERE id=1", (now,)
            )
            self._conn.commit()
        return self.model_budget_status(now=now)

    @staticmethod
    def _agent_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["symbols"] = [s for s in str(data.get("symbols") or "").split(",") if s]
        data["enabled"] = bool(data.get("enabled"))
        return data
