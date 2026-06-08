"""Central monitoring database for events and messages."""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("swarm.monitoring")


def _estimate_tokens(content: str) -> int:
    """Rough token estimate (~4 chars/token) — matches the order of magnitude of
    the char-based heuristic Hermes' ContextCompressor uses to decide when to
    compact. Good enough for dashboard usage tracking; not a billing figure."""
    if not content:
        return 0
    return max(1, len(content) // 4)


class MonitoringDB:
    """Central SQLite database for all monitoring events and message history."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp   REAL    NOT NULL,
        agent_name  TEXT    NOT NULL,
        team_id     TEXT,
        event_type  TEXT    NOT NULL,
        from_agent  TEXT,
        to_agent    TEXT,
        task_id     TEXT,
        data        TEXT
    );

    CREATE TABLE IF NOT EXISTS messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp   REAL    NOT NULL,
        agent_name  TEXT    NOT NULL,
        team_id     TEXT,
        role        TEXT    NOT NULL,
        content     TEXT    NOT NULL,
        task_id     TEXT,
        tokens      INTEGER
    );

    CREATE TABLE IF NOT EXISTS digests (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp        REAL    NOT NULL,
        agent_name       TEXT    NOT NULL,
        team_id          TEXT,
        summary          TEXT    NOT NULL,
        covers_to_msg_id INTEGER,
        msg_count        INTEGER,
        tokens_in        INTEGER,
        model            TEXT
    );

    CREATE TABLE IF NOT EXISTS decisions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp   REAL    NOT NULL,
        agent_name  TEXT    NOT NULL,
        team_id     TEXT,
        decision    TEXT    NOT NULL
    );

    -- Correlation ledger for typed messages: every TASK/QUESTION opens a row,
    -- the matching RESULT (reply_to) answers it. Lets the system see what work
    -- is outstanding without anyone polling, and feeds the loop detector.
    CREATE TABLE IF NOT EXISTS delegations (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        msg_id       TEXT    NOT NULL,
        timestamp    REAL    NOT NULL,
        from_agent   TEXT    NOT NULL,
        to_agent     TEXT    NOT NULL,
        team_id      TEXT,
        kind         TEXT    NOT NULL,
        summary      TEXT,
        status       TEXT    NOT NULL DEFAULT 'open',
        answered_at  REAL,
        reply_to     TEXT
    );

    -- Audit trail of side-effecting actions (deploy, email, publish, payment …)
    -- keyed by an idempotency_key so two agents cannot double-do the same thing.
    CREATE TABLE IF NOT EXISTS actions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp       REAL    NOT NULL,
        agent_name      TEXT    NOT NULL,
        team_id         TEXT,
        action_type     TEXT    NOT NULL,
        target          TEXT,
        idempotency_key TEXT    NOT NULL,
        outcome         TEXT,
        verified        INTEGER NOT NULL DEFAULT 0
    );

    -- Rolled-up summaries of old decisions so long-term team memory isn't lost
    -- when entries scroll past the injected window.
    CREATE TABLE IF NOT EXISTS milestones (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp           REAL    NOT NULL,
        team_id             TEXT,
        summary             TEXT    NOT NULL,
        covers_to_decision  INTEGER
    );

    CREATE INDEX IF NOT EXISTS idx_events_agent     ON events(agent_name);
    CREATE INDEX IF NOT EXISTS idx_events_time      ON events(timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_events_type      ON events(event_type);
    CREATE INDEX IF NOT EXISTS idx_messages_agent   ON messages(agent_name);
    CREATE INDEX IF NOT EXISTS idx_messages_time    ON messages(timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_digests_agent    ON digests(agent_name, timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_decisions_team   ON decisions(team_id, timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_deleg_team       ON delegations(team_id, status, timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_deleg_msg        ON delegations(msg_id);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_key ON actions(team_id, idempotency_key);
    CREATE INDEX IF NOT EXISTS idx_milestones_team  ON milestones(team_id, timestamp DESC);
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()
        self._migrate_add_team_id()

    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)
        # All agent threads write this one DB concurrently; WAL + busy_timeout
        # prevent "database is locked" under load.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(self.SCHEMA)
            conn.commit()

    def _migrate_add_team_id(self):
        """Add team_id column if the DB was created before the schema update."""
        with self._conn() as conn:
            cursor = conn.execute("PRAGMA table_info(events)")
            cols = [c[1] for c in cursor.fetchall()]
            if "team_id" not in cols:
                log.info("[MonitoringDB] Migrating: adding team_id to events table")
                conn.execute("ALTER TABLE events ADD COLUMN team_id TEXT")
                conn.execute("UPDATE events SET team_id = 'default'")
                conn.commit()
            # Ensure team index exists
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_team ON events(team_id)")
            conn.commit()

            cursor = conn.execute("PRAGMA table_info(messages)")
            cols = [c[1] for c in cursor.fetchall()]
            if "team_id" not in cols:
                log.info("[MonitoringDB] Migrating: adding team_id to messages table")
                conn.execute("ALTER TABLE messages ADD COLUMN team_id TEXT")
                conn.execute("UPDATE messages SET team_id = 'default'")
                conn.commit()
            if "tokens" not in cols:
                # Hermes stores NULL token_count in its own state.db, so the
                # dashboard had no usage signal. We record a rough char-based
                # estimate per message (same heuristic Hermes' compressor uses
                # for its trigger) and backfill existing rows.
                log.info("[MonitoringDB] Migrating: adding tokens to messages table")
                conn.execute("ALTER TABLE messages ADD COLUMN tokens INTEGER")
                conn.execute("UPDATE messages SET tokens = MAX(1, LENGTH(content) / 4)")
                conn.commit()
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_team ON messages(team_id)")
            conn.commit()

    def log_event(
        self,
        agent_name: str,
        event_type: str,
        from_agent: str = None,
        to_agent: str = None,
        task_id: str = None,
        data: dict = None,
        team_id: str = None,
    ):
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO events
                       (timestamp, agent_name, team_id, event_type, from_agent, to_agent, task_id, data)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        time.time(),
                        agent_name,
                        team_id,
                        event_type,
                        from_agent,
                        to_agent,
                        task_id,
                        json.dumps(data) if data else None,
                    ),
                )
                conn.commit()
        except Exception as e:
            log.warning("[MonitorDB] Failed to log event: %s", e)

    def log_message(
        self,
        agent_name: str,
        role: str,
        content: str,
        task_id: str = None,
        team_id: str = None,
    ):
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO messages
                       (timestamp, agent_name, team_id, role, content, task_id, tokens)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (time.time(), agent_name, team_id, role, content, task_id,
                     _estimate_tokens(content)),
                )
                conn.commit()
        except Exception as e:
            log.warning("[MonitorDB] Failed to log message: %s", e)

    def log_decision(self, agent_name: str, decision: str, team_id: str = None) -> None:
        """Append a one-line decision to the shared, team-scoped decision log.

        This is the durable team memory that replaces the old agent_log.md /
        log_changes file trail: the last N entries are injected into every
        agent's prompt each turn (see prompts.compose_live_context), so a
        decision recorded here is visible team-wide without anyone reading a
        file. Entries are append-only and never edited."""
        # Collapse to a single line — the whole point of the decision log is a
        # scannable one-liner per entry; newlines would break the injected view.
        one_line = " ".join((decision or "").split()).strip()
        if not one_line:
            return
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO decisions (timestamp, agent_name, team_id, decision) "
                    "VALUES (?, ?, ?, ?)",
                    (time.time(), agent_name, team_id, one_line[:500]),
                )
                conn.commit()
        except Exception as e:
            log.warning("[MonitorDB] Failed to log decision: %s", e)

    def get_recent_decisions(self, team_id: str = None, limit: int = 20) -> List[dict]:
        """Return the most recent decisions (newest first) for prompt injection.

        Scoped by team_id when given. Each row: {timestamp, agent_name, decision}."""
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                if team_id:
                    rows = conn.execute(
                        "SELECT timestamp, agent_name, decision FROM decisions "
                        "WHERE team_id = ? ORDER BY timestamp DESC LIMIT ?",
                        (team_id, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT timestamp, agent_name, decision FROM decisions "
                        "ORDER BY timestamp DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            log.warning("[MonitorDB] Failed to read decisions: %s", e)
            return []

    def get_events(
        self,
        agent_name: str = None,
        team_id: str = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                sql = "SELECT * FROM events WHERE 1=1"
                params = []
                if agent_name:
                    sql += " AND agent_name = ?"
                    params.append(agent_name)
                if team_id:
                    sql += " AND team_id = ?"
                    params.append(team_id)
                sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
                params.extend([limit, offset])
                rows = conn.execute(sql, tuple(params)).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            log.warning("[MonitorDB] Failed to get events: %s", e)
            return []

    def get_messages(
        self,
        agent_name: str,
        team_id: str = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                sql = "SELECT * FROM messages WHERE agent_name = ?"
                params = [agent_name]
                if team_id:
                    sql += " AND team_id = ?"
                    params.append(team_id)
                sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
                params.extend([limit, offset])
                rows = conn.execute(sql, tuple(params)).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            log.warning("[MonitorDB] Failed to get messages: %s", e)
            return []

    # ---- digests (Layer 3 observability) -----------------------------------

    def get_new_activity(
        self, agent_name: str, after_id: int = 0, team_id: str = None
    ) -> Dict[str, int]:
        """Cheap aggregate of un-digested transcript: how many new messages,
        their estimated tokens, and the latest message id. Used by the digest
        trigger WITHOUT pulling any content, so an idle-agent check is one row."""
        out = {"count": 0, "tokens": 0, "max_id": after_id}
        try:
            with self._conn() as conn:
                sql = ("SELECT COUNT(*) c, COALESCE(SUM(tokens),0) t, "
                       "COALESCE(MAX(id), ?) m FROM messages "
                       "WHERE agent_name = ? AND id > ?")
                params = [after_id, agent_name, after_id]
                if team_id:
                    sql += " AND team_id = ?"
                    params.append(team_id)
                row = conn.execute(sql, tuple(params)).fetchone()
                if row:
                    out = {"count": row[0] or 0, "tokens": row[1] or 0,
                           "max_id": row[2] or after_id}
        except Exception as e:
            log.warning("[MonitorDB] get_new_activity failed: %s", e)
        return out

    def get_messages_since(
        self, agent_name: str, after_id: int = 0, team_id: str = None,
        limit: int = 400,
    ) -> List[dict]:
        """New transcript messages (id > after_id), oldest-first, for summarizing."""
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                sql = ("SELECT id, timestamp, role, content, tokens FROM messages "
                       "WHERE agent_name = ? AND id > ?")
                params = [agent_name, after_id]
                if team_id:
                    sql += " AND team_id = ?"
                    params.append(team_id)
                sql += " ORDER BY id ASC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, tuple(params)).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            log.warning("[MonitorDB] get_messages_since failed: %s", e)
            return []

    def get_last_digest(self, agent_name: str, team_id: str = None) -> Optional[dict]:
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                sql = "SELECT * FROM digests WHERE agent_name = ?"
                params = [agent_name]
                if team_id:
                    sql += " AND team_id = ?"
                    params.append(team_id)
                sql += " ORDER BY id DESC LIMIT 1"
                row = conn.execute(sql, tuple(params)).fetchone()
                return dict(row) if row else None
        except Exception as e:
            log.warning("[MonitorDB] get_last_digest failed: %s", e)
            return None

    def get_digests(
        self, agent_name: str, team_id: str = None, limit: int = 50,
    ) -> List[dict]:
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                sql = "SELECT * FROM digests WHERE agent_name = ?"
                params = [agent_name]
                if team_id:
                    sql += " AND team_id = ?"
                    params.append(team_id)
                sql += " ORDER BY id DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, tuple(params)).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            log.warning("[MonitorDB] get_digests failed: %s", e)
            return []

    def save_digest(
        self, agent_name: str, summary: str, covers_to_msg_id: int,
        msg_count: int, tokens_in: int, model: str, team_id: str = None,
    ) -> Optional[int]:
        """Persist one rolling digest. summary is a JSON string. Returns row id."""
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    """INSERT INTO digests
                       (timestamp, agent_name, team_id, summary, covers_to_msg_id,
                        msg_count, tokens_in, model)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (time.time(), agent_name, team_id, summary, covers_to_msg_id,
                     msg_count, tokens_in, model),
                )
                conn.commit()
                return cur.lastrowid
        except Exception as e:
            log.warning("[MonitorDB] save_digest failed: %s", e)
            return None

    def prune(self, max_events: int, max_messages: int,
              max_digests: int = 5000) -> Dict[str, int]:
        """Trim events/messages to the most recent N rows each (rolling retention).

        Without this the DB grows unbounded on a 24/7 run (state-change and
        message volume dominate). Returns rows deleted per table.
        """
        deleted = {"events": 0, "messages": 0, "digests": 0}
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "DELETE FROM events WHERE id NOT IN "
                    "(SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                    (max_events,),
                )
                deleted["events"] = cur.rowcount or 0
                cur = conn.execute(
                    "DELETE FROM messages WHERE id NOT IN "
                    "(SELECT id FROM messages ORDER BY id DESC LIMIT ?)",
                    (max_messages,),
                )
                deleted["messages"] = cur.rowcount or 0
                cur = conn.execute(
                    "DELETE FROM digests WHERE id NOT IN "
                    "(SELECT id FROM digests ORDER BY id DESC LIMIT ?)",
                    (max_digests,),
                )
                deleted["digests"] = cur.rowcount or 0
                conn.commit()
            if deleted["events"] or deleted["messages"] or deleted["digests"]:
                log.info(
                    "[MonitorDB] Pruned %d events, %d messages, %d digests",
                    deleted["events"], deleted["messages"], deleted["digests"],
                )
        except Exception as e:
            log.warning("[MonitorDB] Prune failed: %s", e)
        return deleted

    # ---- Delegation correlation (typed messages: TASK/QUESTION -> RESULT) ----
    def open_delegation(self, msg_id: str, from_agent: str, to_agent: str,
                        kind: str, summary: str = "", team_id: str = None) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO delegations (msg_id, timestamp, from_agent, to_agent, "
                    "team_id, kind, summary, status) VALUES (?,?,?,?,?,?,?, 'open')",
                    (msg_id, time.time(), from_agent, to_agent, team_id, kind,
                     (summary or "")[:200]),
                )
                conn.commit()
        except Exception as e:
            log.warning("[MonitorDB] open_delegation failed: %s", e)

    def answer_delegation(self, reply_to_msg_id: str, by_agent: str = None) -> bool:
        """Mark the open delegation with msg_id == reply_to as answered. Returns
        True if a matching open row was closed."""
        if not reply_to_msg_id:
            return False
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "UPDATE delegations SET status='answered', answered_at=? "
                    "WHERE msg_id=? AND status='open'",
                    (time.time(), reply_to_msg_id),
                )
                conn.commit()
                return (cur.rowcount or 0) > 0
        except Exception as e:
            log.warning("[MonitorDB] answer_delegation failed: %s", e)
            return False

    def get_open_delegations(self, to_agent: str = None, from_agent: str = None,
                             team_id: str = None, limit: int = 50) -> List[dict]:
        """Outstanding TASK/QUESTION delegations (status='open'), newest first."""
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                sql = "SELECT * FROM delegations WHERE status='open'"
                params: list = []
                if team_id:
                    sql += " AND team_id = ?"; params.append(team_id)
                if to_agent:
                    sql += " AND to_agent = ?"; params.append(to_agent)
                if from_agent:
                    sql += " AND from_agent = ?"; params.append(from_agent)
                sql += " ORDER BY timestamp DESC LIMIT ?"; params.append(limit)
                return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
        except Exception as e:
            log.warning("[MonitorDB] get_open_delegations failed: %s", e)
            return []

    # ---- Action audit log + idempotency ----
    def record_action(self, agent_name: str, action_type: str, idempotency_key: str,
                      target: str = "", outcome: str = "", verified: bool = False,
                      team_id: str = None) -> Dict[str, Any]:
        """Record a side-effecting action. Idempotent on (team_id, key): if the
        key already exists this records NOTHING and returns the prior row so the
        caller can skip the duplicate. Returns {duplicate: bool, existing|recorded}."""
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                prior = conn.execute(
                    "SELECT * FROM actions WHERE team_id IS ? AND idempotency_key = ?",
                    (team_id, idempotency_key),
                ).fetchone()
                if prior:
                    return {"duplicate": True, "existing": dict(prior)}
                conn.execute(
                    "INSERT INTO actions (timestamp, agent_name, team_id, action_type, "
                    "target, idempotency_key, outcome, verified) VALUES (?,?,?,?,?,?,?,?)",
                    (time.time(), agent_name, team_id, action_type, (target or "")[:300],
                     idempotency_key, (outcome or "")[:500], 1 if verified else 0),
                )
                conn.commit()
                return {"duplicate": False, "recorded": True}
        except Exception as e:
            log.warning("[MonitorDB] record_action failed: %s", e)
            return {"duplicate": False, "recorded": False, "error": str(e)}

    def get_events_since(self, event_type: str, since_ts: float,
                         limit: int = 1000) -> List[dict]:
        """Events of one type newer than since_ts, newest first. Used by the loop
        detector so a flood of other event types can't push message_sent rows out
        of a fixed-size get_events() page."""
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM events WHERE event_type = ? AND timestamp >= ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (event_type, since_ts, limit),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            log.warning("[MonitorDB] get_events_since failed: %s", e)
            return []

    def count_actions_since(self, team_id: str, since_ts: float) -> int:
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM actions WHERE team_id IS ? AND timestamp >= ?",
                    (team_id, since_ts),
                ).fetchone()
                return row[0] if row else 0
        except Exception:
            return 0

    def count_decisions_since(self, team_id: str, since_ts: float) -> int:
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM decisions WHERE team_id IS ? AND timestamp >= ?",
                    (team_id, since_ts),
                ).fetchone()
                return row[0] if row else 0
        except Exception:
            return 0

    # ---- Decision-log rollup (long-term memory) ----
    def get_decisions_after(self, team_id: str, after_id: int = 0,
                            limit: int = 1000) -> List[dict]:
        """Decisions with id > after_id, OLDEST first (for rollup batching)."""
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, timestamp, agent_name, decision FROM decisions "
                    "WHERE team_id IS ? AND id > ? ORDER BY id ASC LIMIT ?",
                    (team_id, after_id, limit),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            log.warning("[MonitorDB] get_decisions_after failed: %s", e)
            return []

    def save_milestone(self, team_id: str, summary: str, covers_to_decision: int) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO milestones (timestamp, team_id, summary, covers_to_decision) "
                    "VALUES (?,?,?,?)",
                    (time.time(), team_id, summary, covers_to_decision),
                )
                conn.commit()
        except Exception as e:
            log.warning("[MonitorDB] save_milestone failed: %s", e)

    def get_latest_milestone(self, team_id: str) -> Optional[dict]:
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                r = conn.execute(
                    "SELECT * FROM milestones WHERE team_id IS ? "
                    "ORDER BY timestamp DESC LIMIT 1", (team_id,),
                ).fetchone()
                return dict(r) if r else None
        except Exception as e:
            log.warning("[MonitorDB] get_latest_milestone failed: %s", e)
            return None

    def get_agent_stats(self, team_id: Optional[str] = None) -> Dict[str, dict]:
        stats = {}
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                sql = (
                    "SELECT agent_name, event_type, COUNT(*) as count FROM events"
                )
                params = []
                if team_id:
                    sql += " WHERE team_id = ?"
                    params.append(team_id)
                sql += " GROUP BY agent_name, event_type"
                rows = conn.execute(sql, tuple(params)).fetchall()
                for r in rows:
                    aname = r["agent_name"]
                    if aname not in stats:
                        stats[aname] = {
                            "events": {},
                            "last_active": None,
                            "total_messages": 0,
                            "total_tokens": 0,
                        }
                    stats[aname]["events"][r["event_type"]] = r["count"]

                sql_last = "SELECT agent_name, MAX(timestamp) as last_ts FROM events"
                params_last = []
                if team_id:
                    sql_last += " WHERE team_id = ?"
                    params_last.append(team_id)
                sql_last += " GROUP BY agent_name"
                rows = conn.execute(sql_last, tuple(params_last)).fetchall()
                for r in rows:
                    if r["agent_name"] in stats:
                        stats[r["agent_name"]]["last_active"] = r["last_ts"]

                sql_msg = (
                    "SELECT agent_name, COUNT(*) as count, "
                    "COALESCE(SUM(tokens), 0) as tokens FROM messages"
                )
                params_msg = []
                if team_id:
                    sql_msg += " WHERE team_id = ?"
                    params_msg.append(team_id)
                sql_msg += " GROUP BY agent_name"
                rows = conn.execute(sql_msg, tuple(params_msg)).fetchall()
                for r in rows:
                    if r["agent_name"] in stats:
                        stats[r["agent_name"]]["total_messages"] = r["count"]
                        stats[r["agent_name"]]["total_tokens"] = r["tokens"]
        except Exception as e:
            log.warning("[MonitorDB] Failed to get stats: %s", e)
        return stats


# Global singleton instance
from swarm_server.config import MONITORING_DB  # noqa: E402

monitor_db = MonitoringDB(MONITORING_DB)
