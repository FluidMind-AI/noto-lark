"""
Hardened SQLite settings — every bot-read DB needs these or it throws
"database is locked" the instant another process touches it.

The bot reads many SQLite indexes on a hot path (doc_index for FTS,
embeddings for vector RAG, feedback/usage stores, tombstones, etc.).
The nightly resync (and any background extraction jobs) are concurrent
writers. Without WAL + a healthy busy_timeout, a bot read can fail
immediately under contention — that's what produced the "I hit an
error answering that" cards on 2026-05-30.

Call harden(conn) on every sqlite3.Connection the bot may touch.
"""

import sqlite3


def harden(conn: sqlite3.Connection, timeout_ms: int = 30000) -> None:
    """Apply WAL + a 30s busy_timeout (overridable). Idempotent — safe to
    call multiple times on the same connection."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
