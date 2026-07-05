#!/usr/bin/env python3
"""
Per-user usage store for the operator dashboard.

Two tables, deliberately narrow:

  messages — one row per inbound im.message.receive_v1 the bot processed.
             Metadata only (workflow, attribution, ts) — NEVER the
             message body or any user-typed text. Privacy by construction.

  tokens   — one row per upstream Claude call (research, drafting,
             feedback classification). Captures input/output/cache token
             counts and the cost the CLI reports, scoped to the
             user/workflow that triggered the call.

Plus a denormalised `users` table so the dashboard can show top users
without scanning the whole messages table on every render.

All writes are best-effort: a usage-tracking failure must never break a
user-facing reply. Callers wrap calls in their own try/except.
"""

import os
import sqlite3
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_home  # noqa: E402


def _db_path() -> str:
    return os.path.join(get_home(), "indexes", "usage.db")


# ---------------------------------------------------------------------------
# Workflow vocabulary (single source of truth — keep small).
# Anything the bot does that costs tokens or holds a user's attention
# should fall into one of these. The dashboard groups by these labels.
# ---------------------------------------------------------------------------

WORKFLOWS = {
    "q_and_a":          "Q&A / research",
    "general":          "General",
    "doc_edit":         "Document — edit",
    "deliverable_doc":  "Lark doc (deliverables)",
    "feedback":         "Feedback capture / classify",
    "command":          "/command (help/feedback)",
    "system":           "Background / system",  # nightly resync, indexers
    "unknown":          "Unknown",
}


def normalise_workflow(wf: Optional[str]) -> str:
    if not wf:
        return "unknown"
    w = str(wf).strip().lower()
    return w if w in WORKFLOWS else "unknown"


# ---------------------------------------------------------------------------
# Schema + open/close
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    user_open_id  TEXT    NOT NULL,
    user_name     TEXT,
    chat_id       TEXT,
    chat_type     TEXT,            -- 'p2p' | 'group'
    addressed     INTEGER NOT NULL DEFAULT 0,
    workflow      TEXT    NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_msg_user_ts  ON messages(user_open_id, ts);
CREATE INDEX IF NOT EXISTS idx_msg_ts       ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_msg_workflow ON messages(workflow, ts);

CREATE TABLE IF NOT EXISTS tokens (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                     REAL    NOT NULL,
    user_open_id           TEXT,           -- nullable: background calls
    workflow               TEXT    NOT NULL DEFAULT 'unknown',
    model                  TEXT,
    input_tokens           INTEGER NOT NULL DEFAULT 0,
    output_tokens          INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens  INTEGER NOT NULL DEFAULT 0,
    total_cost_usd         REAL    NOT NULL DEFAULT 0.0,
    duration_ms            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tok_user_ts  ON tokens(user_open_id, ts);
CREATE INDEX IF NOT EXISTS idx_tok_ts       ON tokens(ts);
CREATE INDEX IF NOT EXISTS idx_tok_workflow ON tokens(workflow, ts);

CREATE TABLE IF NOT EXISTS users (
    open_id      TEXT PRIMARY KEY,
    display_name TEXT,
    first_seen   REAL,
    last_seen    REAL,
    msg_count    INTEGER NOT NULL DEFAULT 0
);
"""


class UsageStore:
    """Thread-safe SQLite wrapper. Singleton-friendly — call once per
    process; uses a lock for write serialisation. Reads can run concurrently."""

    _instance: Optional["UsageStore"] = None
    _instance_lock = threading.Lock()

    @classmethod
    def get(cls) -> "UsageStore":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = UsageStore()
            return cls._instance

    def __init__(self, path: Optional[str] = None):
        self.path = path or _db_path()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(_SCHEMA)
            # Additive migration: action_type = the USE CASE of the
            # message (agent skill / command kind) — finer than workflow.
            try:
                db.execute("ALTER TABLE messages ADD COLUMN action_type "
                           "TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    # -- writes -----------------------------------------------------------

    def log_message(self, user_open_id: str, user_name: Optional[str],
                    chat_id: Optional[str], chat_type: Optional[str],
                    addressed: bool, workflow: Optional[str],
                    action_type: str = "") -> None:
        if not user_open_id:
            return
        ts = time.time()
        wf = normalise_workflow(workflow)
        try:
            with self._lock, self._connect() as db:
                db.execute(
                    "INSERT INTO messages "
                    "(ts, user_open_id, user_name, chat_id, chat_type, "
                    " addressed, workflow, action_type) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ts, user_open_id, user_name or "", chat_id or "",
                     chat_type or "", 1 if addressed else 0, wf,
                     (action_type or "")[:60]),
                )
                # Upsert the user row
                db.execute(
                    "INSERT INTO users (open_id, display_name, first_seen, "
                    " last_seen, msg_count) VALUES (?, ?, ?, ?, 1) "
                    "ON CONFLICT(open_id) DO UPDATE SET "
                    "  display_name = COALESCE(NULLIF(excluded.display_name,''),"
                    "                          users.display_name), "
                    "  last_seen    = excluded.last_seen, "
                    "  msg_count    = users.msg_count + 1",
                    (user_open_id, user_name or "", ts, ts),
                )
                db.commit()
        except Exception as e:
            print(f"[usage_store] WARN log_message failed: {e}",
                  file=sys.stderr, flush=True)

    def set_action(self, user_open_id: str, chat_id: str,
                   action_type: str, window_s: float = 600.0) -> None:
        """Attach the use-case label to the user's LATEST message in this
        chat (within window_s). Used for agent-routed messages, whose
        skill is only known after the planner runs. Best-effort."""
        if not (user_open_id and action_type):
            return
        try:
            with self._lock, self._connect() as db:
                db.execute(
                    "UPDATE messages SET action_type=? WHERE id = ("
                    "  SELECT id FROM messages WHERE user_open_id=? "
                    "  AND chat_id=? AND ts >= ? "
                    "  ORDER BY ts DESC LIMIT 1)",
                    ((action_type or "")[:60], user_open_id,
                     chat_id or "", time.time() - window_s))
                db.commit()
        except Exception as e:
            print(f"[usage_store] WARN set_action failed: {e}",
                  file=sys.stderr, flush=True)

    def actions_breakdown(self, window_days: int = 30
                          ) -> "List[Dict[str, Any]]":
        """Use-case distribution (non-empty action_type only)."""
        cutoff = time.time() - window_days * 86400
        try:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT action_type, COUNT(*) n, "
                    "COUNT(DISTINCT user_open_id) users "
                    "FROM messages WHERE ts >= ? AND action_type != '' "
                    "GROUP BY action_type ORDER BY n DESC",
                    (cutoff,)).fetchall()
            return [{"action_type": r[0], "messages": r[1],
                     "users": r[2]} for r in rows]
        except Exception:
            return []

    def actions_for_user(self, open_id: str, window_days: int = 30
                         ) -> "List[Dict[str, Any]]":
        """One user's use-case counts."""
        cutoff = time.time() - window_days * 86400
        try:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT action_type, COUNT(*) n FROM messages "
                    "WHERE user_open_id=? AND ts >= ? AND action_type != '' "
                    "GROUP BY action_type ORDER BY n DESC",
                    (open_id, cutoff)).fetchall()
            return [{"action_type": r[0], "n": r[1]} for r in rows]
        except Exception:
            return []

    def actions_by_user(self, window_days: int = 30
                        ) -> "List[Dict[str, Any]]":
        """Per-user use-case counts — the coaching matrix."""
        cutoff = time.time() - window_days * 86400
        try:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT user_open_id, action_type, COUNT(*) n "
                    "FROM messages WHERE ts >= ? AND action_type != '' "
                    "GROUP BY user_open_id, action_type",
                    (cutoff,)).fetchall()
            return [{"open_id": r[0], "action_type": r[1], "n": r[2]}
                    for r in rows]
        except Exception:
            return []

    def log_tokens(self, user_open_id: Optional[str],
                   workflow: Optional[str], model: Optional[str],
                   input_tokens: int, output_tokens: int,
                   cache_read_tokens: int = 0, cache_creation_tokens: int = 0,
                   total_cost_usd: float = 0.0, duration_ms: int = 0) -> None:
        # Drop empty-usage records — keeps storage clean.
        if (input_tokens or output_tokens or cache_read_tokens
                or cache_creation_tokens) == 0:
            return
        ts = time.time()
        wf = normalise_workflow(workflow)
        try:
            with self._lock, self._connect() as db:
                db.execute(
                    "INSERT INTO tokens "
                    "(ts, user_open_id, workflow, model, "
                    " input_tokens, output_tokens, "
                    " cache_read_tokens, cache_creation_tokens, "
                    " total_cost_usd, duration_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ts, user_open_id or "", wf, model or "",
                     int(input_tokens or 0), int(output_tokens or 0),
                     int(cache_read_tokens or 0),
                     int(cache_creation_tokens or 0),
                     float(total_cost_usd or 0.0),
                     int(duration_ms or 0)),
                )
                db.commit()
        except Exception as e:
            print(f"[usage_store] WARN log_tokens failed: {e}",
                  file=sys.stderr, flush=True)

    # -- reads (used by tools/dashboard.py) -------------------------------

    def users_summary(self, window_days: int = 7) -> List[Dict[str, Any]]:
        """Top users by recent message count. One row per user with
        message totals, distinct workflows used, tokens spent, last seen."""
        cutoff = time.time() - window_days * 86400
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """
                SELECT
                    u.open_id,
                    u.display_name,
                    u.msg_count                                     AS msgs_all,
                    COALESCE(m7.n, 0)                               AS msgs_window,
                    COALESCE(m30.n, 0)                              AS msgs_30d,
                    COALESCE(w.distinct_workflows, 0)               AS workflows_used,
                    u.last_seen,
                    COALESCE(t.input_tokens, 0)                     AS input_tokens,
                    COALESCE(t.output_tokens, 0)                    AS output_tokens,
                    COALESCE(t.cache_read_tokens, 0)                AS cache_read,
                    COALESCE(t.cache_creation_tokens, 0)            AS cache_creation,
                    COALESCE(t.total_cost_usd, 0)                   AS cost_usd
                FROM users u
                LEFT JOIN (
                    SELECT user_open_id, COUNT(*) AS n FROM messages
                    WHERE ts >= ? GROUP BY user_open_id
                ) m7  ON m7.user_open_id  = u.open_id
                LEFT JOIN (
                    SELECT user_open_id, COUNT(*) AS n FROM messages
                    WHERE ts >= ? GROUP BY user_open_id
                ) m30 ON m30.user_open_id = u.open_id
                LEFT JOIN (
                    SELECT user_open_id, COUNT(DISTINCT workflow)
                           AS distinct_workflows
                    FROM messages WHERE ts >= ? GROUP BY user_open_id
                ) w  ON w.user_open_id    = u.open_id
                LEFT JOIN (
                    SELECT user_open_id,
                           SUM(input_tokens)          AS input_tokens,
                           SUM(output_tokens)         AS output_tokens,
                           SUM(cache_read_tokens)     AS cache_read_tokens,
                           SUM(cache_creation_tokens) AS cache_creation_tokens,
                           SUM(total_cost_usd)        AS total_cost_usd
                    FROM tokens WHERE ts >= ? GROUP BY user_open_id
                ) t  ON t.user_open_id    = u.open_id
                ORDER BY msgs_window DESC, u.last_seen DESC
                """,
                (cutoff, time.time() - 30 * 86400, cutoff, cutoff),
            ).fetchall()
        return [dict(r) for r in rows]

    def headline_kpis(self, window_days: int = 7) -> Dict[str, Any]:
        cutoff = time.time() - window_days * 86400
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            msg = db.execute(
                "SELECT COUNT(*) AS n, COUNT(DISTINCT user_open_id) AS u "
                "FROM messages WHERE ts >= ?", (cutoff,)).fetchone()
            tok = db.execute(
                "SELECT COALESCE(SUM(input_tokens+output_tokens+"
                "  cache_read_tokens+cache_creation_tokens), 0) AS total, "
                " COALESCE(SUM(total_cost_usd), 0) AS cost "
                "FROM tokens WHERE ts >= ?", (cutoff,)).fetchone()
            top_wf = db.execute(
                "SELECT workflow, COUNT(*) AS n FROM messages "
                "WHERE ts >= ? GROUP BY workflow ORDER BY n DESC LIMIT 1",
                (cutoff,)).fetchone()
        return {
            "messages":       int(msg["n"]) if msg else 0,
            "active_users":   int(msg["u"]) if msg else 0,
            "tokens_total":   int(tok["total"]) if tok else 0,
            "cost_usd":       float(tok["cost"]) if tok else 0.0,
            "top_workflow":   (top_wf["workflow"] if top_wf else None),
            "top_workflow_n": (int(top_wf["n"]) if top_wf else 0),
        }

    def workflows_breakdown(self, window_days: int = 7,
                            user_open_id: Optional[str] = None,
                            ) -> List[Dict[str, Any]]:
        cutoff = time.time() - window_days * 86400
        params: List[Any] = [cutoff]
        where = "WHERE ts >= ?"
        if user_open_id:
            where += " AND user_open_id = ?"
            params.append(user_open_id)
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            msg_rows = db.execute(
                f"SELECT workflow, COUNT(*) AS msgs, "
                f"  COUNT(DISTINCT user_open_id) AS users "
                f"FROM messages {where} GROUP BY workflow "
                f"ORDER BY msgs DESC",
                params).fetchall()
            tok_rows = db.execute(
                f"SELECT workflow, "
                f"  SUM(input_tokens+output_tokens+cache_read_tokens+"
                f"      cache_creation_tokens) AS total_tokens, "
                f"  SUM(total_cost_usd) AS cost "
                f"FROM tokens {where} GROUP BY workflow",
                params).fetchall()
        tokens_by_wf = {r["workflow"]: dict(r) for r in tok_rows}
        out = []
        for r in msg_rows:
            wf = r["workflow"]
            tk = tokens_by_wf.get(wf, {})
            out.append({
                "workflow":     wf,
                "label":        WORKFLOWS.get(wf, wf),
                "messages":     int(r["msgs"]),
                "users":        int(r["users"]),
                "tokens":       int(tk.get("total_tokens") or 0),
                "cost_usd":     float(tk.get("cost") or 0.0),
            })
        return out

    def get_user(self, open_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM users WHERE open_id = ?",
                (open_id,),
            ).fetchone()
            return dict(row) if row else None

    def messages_by_day(self, days: int = 14,
                        user_open_id: Optional[str] = None,
                        ) -> List[Tuple[str, int]]:
        """Daily message counts for a sparkline. Returns [(YYYY-MM-DD, n), ...]
        in chronological order, gap-filled with zeros."""
        cutoff = time.time() - days * 86400
        params: List[Any] = [cutoff]
        where = "WHERE ts >= ?"
        if user_open_id:
            where += " AND user_open_id = ?"
            params.append(user_open_id)
        with self._connect() as db:
            rows = db.execute(
                f"SELECT date(ts, 'unixepoch', 'localtime') AS d, COUNT(*) "
                f"FROM messages {where} GROUP BY d ORDER BY d",
                params,
            ).fetchall()
        by_day = {d: n for (d, n) in rows}
        # Gap-fill so the sparkline shows zeros, not jumps.
        from datetime import date, timedelta
        out = []
        today = date.today()
        for i in range(days, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            out.append((d, by_day.get(d, 0)))
        return out


# ---------------------------------------------------------------------------
# CLI for self-test + one-off queries
# ---------------------------------------------------------------------------

def _main(argv: List[str]) -> int:
    cmd = argv[0] if argv else "selftest"
    s = UsageStore.get()
    if cmd == "selftest":
        s.log_message("ou_test1", "Test User", "oc_test", "p2p", True,
                      "q_and_a")
        s.log_tokens("ou_test1", "q_and_a", "claude-opus", 100, 200, 50, 0,
                     0.012, 1500)
        print("logged 1 message + 1 token row")
        print("kpis:", s.headline_kpis())
        print("users:", s.users_summary())
        return 0
    if cmd == "kpis":
        import json as _json
        print(_json.dumps(s.headline_kpis(), indent=2))
        return 0
    if cmd == "users":
        for r in s.users_summary():
            print(r)
        return 0
    if cmd == "workflows":
        for r in s.workflows_breakdown():
            print(r)
        return 0
    print(f"unknown: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
