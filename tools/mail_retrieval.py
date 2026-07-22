#!/usr/bin/env python3
"""
Per-user inbox retrieval (F4-4a) — hybrid search + thread assembly +
LLM synthesis over ONE user's mail_store DB.

Architecture (deliberately NOT GraphRAG): FTS5 (exact names/firms/
numbers) + local vectors (paraphrase) merged by reciprocal-rank fusion,
grouped into THREADS (inbox answers live in conversations, not lone
messages), synthesized by the same claude -p path the company research
engine uses. Entity tagging can link hits to the candidate/firm graph
later — that's the graph-RAG benefit without a knowledge-graph build.

ISOLATION (hard rule): every function takes a `user` slug and opens
ONLY that user's DB file. The bot-facing gate is `user_for_asker()` —
it returns a slug ONLY for a p2p DM from the mailbox owner; anything
else gets None and the caller must refuse. Vectors live INSIDE the
per-user DB file (no shared vector store), so isolation stays
by-construction.

CLI (operator debugging):
  python tools/mail_retrieval.py build-vectors <user> [--limit N]
  python tools/mail_retrieval.py retrieve <user> "<query>"   # subjects only
  python tools/mail_retrieval.py ask <user> "<question>"
"""

import json
import os
import re
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# fastembed defaults its model cache to the system TEMP dir, which macOS
# clears on reboot (the ONNX file vanished after 2026-07-15's restart).
# Pin it somewhere persistent BEFORE embeddings loads the model.
os.environ.setdefault(
    "FASTEMBED_CACHE_PATH",
    os.path.join(os.path.expanduser("~/noto-home"), "indexes",
                 "fastembed-cache"))
import mail_store                                              # noqa: E402
from embeddings import embed_passages, embed_query             # noqa: E402

import numpy as np                                             # noqa: E402

# The ONLY bot-facing authorization map: Lark open_id → mail slug.
# recruiter_memory discipline: p2p + exact owner, or nothing.
def _owner_map():
    from config import load_config
    users = (load_config().get("mail", {}) or {}).get("users", {}) or {}
    return {(u or {}).get("open_id", ""): slug
            for slug, u in users.items() if (u or {}).get("open_id")}

_VEC_SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_vecs (
  msg_id     TEXT PRIMARY KEY,
  dim        INTEGER NOT NULL,
  vec        BLOB NOT NULL,
  updated_at REAL NOT NULL
);
"""


def user_for_asker(open_id: str, chat_type: str) -> Optional[str]:
    """The bot's gate. A mail slug comes back ONLY for a 1:1 DM from the
    mailbox owner — group chats always get None, even for the owner."""
    if (chat_type or "").lower() != "p2p":
        return None
    return _owner_map().get(open_id or "")


def _connect(user: str) -> sqlite3.Connection:
    conn = mail_store._connect(user)
    conn.executescript(_VEC_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Vectors — one embedding per message (subject + body head)
# ---------------------------------------------------------------------------

def _embed_text(subject: str, body: str) -> str:
    return ((subject or "") + "\n" + (body or "")[:1800]).strip()


def build_vectors(user: str, limit: int = 0, batch: int = 64) -> int:
    conn = _connect(user)
    try:
        rows = conn.execute(
            "SELECT m.msg_id, m.subject, m.body_plain FROM messages m"
            " LEFT JOIN mail_vecs v ON v.msg_id = m.msg_id"
            " WHERE v.msg_id IS NULL AND m.is_noreply = 0").fetchall()
        if limit:
            rows = rows[:limit]
        done = 0
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            vecs = embed_passages(
                [_embed_text(r["subject"], r["body_plain"]) for r in chunk])
            now = time.time()
            conn.executemany(
                "INSERT OR REPLACE INTO mail_vecs (msg_id, dim, vec,"
                " updated_at) VALUES (?,?,?,?)",
                [(r["msg_id"], int(vecs.shape[1]),
                  np.asarray(v, dtype=np.float32).tobytes(), now)
                 for r, v in zip(chunk, vecs)])
            conn.commit()
            done += len(chunk)
        return done
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Hybrid retrieval → threads
# ---------------------------------------------------------------------------

def _fts_hits(conn, query: str, k: int) -> List[str]:
    # FTS5 syntax chokes on stray operators in natural questions — quote
    # each term instead.
    terms = [t for t in re.findall(r"[A-Za-z0-9@.'-]+", query) if len(t) > 1]
    if not terms:
        return []
    match = " OR ".join(f'"{t}"' for t in terms[:12])
    try:
        return [r[0] for r in conn.execute(
            "SELECT msg_id FROM messages_fts WHERE messages_fts MATCH ?"
            " ORDER BY rank LIMIT ?", (match, k))]
    except sqlite3.OperationalError:
        return []


def _vec_hits(conn, query: str, k: int) -> List[str]:
    rows = conn.execute("SELECT msg_id, vec FROM mail_vecs").fetchall()
    if not rows:
        return []
    mat = np.frombuffer(b"".join(r["vec"] for r in rows),
                        dtype=np.float32).reshape(len(rows), -1)
    q = embed_query(query)
    sims = mat @ np.asarray(q, dtype=np.float32)
    order = np.argsort(-sims)[:k]
    return [rows[int(i)]["msg_id"] for i in order]


def retrieve_threads(user: str, query: str,
                     n_threads: int = 4) -> List[Dict[str, Any]]:
    """Hybrid RRF over messages → top threads with full context."""
    conn = _connect(user)
    try:
        fts = _fts_hits(conn, query, 40)
        vec = _vec_hits(conn, query, 40)
        score: Dict[str, float] = {}
        for rank, mid in enumerate(fts):
            score[mid] = score.get(mid, 0) + 1.0 / (60 + rank)
        for rank, mid in enumerate(vec):
            score[mid] = score.get(mid, 0) + 1.0 / (60 + rank)
        if not score:
            return []
        # message score → thread score (best hit + per-extra-hit bonus)
        tscore: Dict[str, float] = {}
        for mid, s in score.items():
            r = conn.execute("SELECT thread_id FROM messages WHERE msg_id=?",
                             (mid,)).fetchone()
            tid = (r and r[0]) or mid
            tscore[tid] = max(tscore.get(tid, 0), s) + 0.002
        top = sorted(tscore, key=tscore.get, reverse=True)[:n_threads]
        out = []
        for tid in top:
            msgs = [dict(r) for r in conn.execute(
                "SELECT date_ms, from_email, from_name, to_json, subject,"
                " body_plain FROM messages WHERE thread_id=?"
                " ORDER BY date_ms", (tid,))]
            if msgs:
                out.append({"thread_id": tid, "score": tscore[tid],
                            "messages": msgs})
        return out
    finally:
        conn.close()


def _render_threads(threads: List[Dict[str, Any]],
                    char_cap: int = 24000) -> str:
    parts, used = [], 0
    for t in threads:
        first = t["messages"][0]
        head = f"### THREAD: {first.get('subject') or '(no subject)'}\n"
        body = ""
        for m in t["messages"]:
            ts = m.get("date_ms") or 0
            day = time.strftime("%Y-%m-%d",
                                time.localtime(ts / 1000 if ts > 10**12
                                               else ts)) if ts else "?"
            to = ", ".join(json.loads(m.get("to_json") or "[]")[:3])
            body += (f"[{day}] {m.get('from_email','?')} → {to}\n"
                     f"{(m.get('body_plain') or '')[:2200]}\n---\n")
        seg = head + body
        if used + len(seg) > char_cap:
            break
        parts.append(seg)
        used += len(seg)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Answer
# ---------------------------------------------------------------------------

def answer(user: str, question: str) -> str:
    """Synthesize an answer from the user's own mail. Caller MUST have
    authorized via user_for_asker() — this function trusts `user`."""
    threads = retrieve_threads(user, question, n_threads=4)
    if not threads:
        return ("I couldn't find anything in your mailbox matching that — "
                "try different wording or a name/firm I can search for.")
    ctx = _render_threads(threads)
    from noto_research import _claude
    prompt = (
        "You are Noto answering a question the mailbox OWNER asked about "
        "their own email. Answer ONLY from the threads below. Cite dates "
        "and senders for each claim. If the threads don't contain the "
        "answer, say so plainly — never guess or invent.\n\n"
        f"OWNER'S QUESTION: {question}\n\nTHEIR EMAIL THREADS:\n{ctx}")
    return (_claude(prompt, timeout=180, web=False) or "").strip() or \
        "Synthesis failed — try again."


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "help"
    if cmd == "build-vectors" and len(args) > 1:
        lim = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
        print(f"embedded {build_vectors(args[1], limit=lim)} messages")
    elif cmd == "retrieve" and len(args) > 2:
        for t in retrieve_threads(args[1], args[2]):
            first = t["messages"][0]
            print(f"  {t['score']:.4f}  ({len(t['messages'])} msgs)  "
                  f"{(first.get('subject') or '')[:64]}")
    elif cmd == "ask" and len(args) > 2:
        print(answer(args[1], " ".join(args[2:])))
    else:
        print(__doc__)
