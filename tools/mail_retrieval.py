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

# Function words + mail-meta words that OR-match half the mailbox and
# drown real signal ("any urgent emails i received today" must search
# for "urgent", not "any/emails/received/today").
_STOP = {
    "the", "a", "an", "any", "all", "my", "i", "me", "we", "you",
    "your", "is", "are", "was", "were", "do", "did", "does", "have",
    "has", "had", "of", "in", "on", "at", "to", "for", "from", "with",
    "about", "and", "or", "not", "what", "which", "who", "when",
    "where", "how", "that", "this", "these", "those", "it", "its",
    "there", "be", "been", "am", "pm", "email", "emails", "mail",
    "mails", "inbox", "mailbox", "message", "messages", "received",
    "sent", "send", "get", "got", "new", "anything", "something",
    "today", "yesterday", "week", "recently", "recent", "latest",
}


def _fts_hits(conn, query: str, k: int) -> List[str]:
    # FTS5 syntax chokes on stray operators in natural questions — quote
    # each term instead.
    terms = [t for t in re.findall(r"[A-Za-z0-9@.'-]+", query)
             if len(t) > 1 and t.lower() not in _STOP]
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
    # 0.35 = the accuracy-review floor (same as embeddings.search in
    # the drafting engine): never inject nearest-but-irrelevant.
    order = [int(i) for i in np.argsort(-sims)[:k] if sims[int(i)] >= 0.35]
    return [rows[i]["msg_id"] for i in order]


# "today / this week / recent…" is a DATE WINDOW, not a keyword —
# lexical/vector search finds nothing for it. When a query carries
# temporal intent, recent messages join the candidate pool directly.
_TEMPORAL = re.compile(
    r"\b(today|tonight|yesterday|this (morning|afternoon|evening|"
    r"week)|last (night|week|few days)|past (week|few days|\d+ days?)|"
    r"recent(ly)?|latest|just (came|come) in)\b", re.I)


def _temporal_window_h(query: str) -> int:
    """Hours of lookback implied by the query; 0 = no temporal intent."""
    m = _TEMPORAL.search(query or "")
    if not m:
        return 0
    t = m.group(0).lower()
    if "week" in t:
        return 7 * 24
    if "yesterday" in t or "night" in t or "days" in t:
        return 3 * 24
    return 48        # today/tonight/morning/recent — TZ + sync slack


def _recent_msg_ids(conn, query: str, cap: int = 15) -> List[str]:
    hours = _temporal_window_h(query)
    if not hours:
        return []
    # date_ms is ms in current mirrors, but scale defensively.
    r = conn.execute("SELECT MAX(date_ms) FROM messages").fetchone()
    scale = 1000 if (r and (r[0] or 0) > 10 ** 12) else 1
    since = (time.time() - hours * 3600) * scale
    return [row[0] for row in conn.execute(
        "SELECT msg_id FROM messages WHERE date_ms >= ? AND"
        " is_noreply = 0 ORDER BY date_ms DESC LIMIT ?", (since, cap))]


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
        # For a temporal question, recency IS the signal — weight the
        # window messages above topical matches so today's threads
        # can't be outranked by years-old keyword noise.
        for rank, mid in enumerate(_recent_msg_ids(conn, query)):
            score[mid] = score.get(mid, 0) + 3.0 / (60 + rank)
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

    def _fmt(msgs):
        out = ""
        for m in msgs:
            ts = m.get("date_ms") or 0
            day = time.strftime("%Y-%m-%d",
                                time.localtime(ts / 1000 if ts > 10**12
                                               else ts)) if ts else "?"
            to = ", ".join(json.loads(m.get("to_json") or "[]")[:3])
            out += (f"[{day}] {m.get('from_email','?')} → {to}\n"
                    f"{(m.get('body_plain') or '')[:2200]}\n---\n")
        return out

    def _seg(t, tail):
        msgs = t["messages"]
        first = msgs[0]
        head = f"### THREAD: {first.get('subject') or '(no subject)'}\n"
        if len(msgs) > tail:
            head += (f"(… {len(msgs) - tail} earlier messages "
                     "omitted …)\n")
            msgs = msgs[-tail:]
        return head + _fmt(msgs)

    for t in threads:
        # Giant threads: the newest tail answers almost every question;
        # one verbose thread must not eat the whole budget.
        seg = _seg(t, 6)
        if len(seg) > 8000:
            seg = _seg(t, 3)
        if used + len(seg) > char_cap:
            if not parts:
                # NEVER return empty context when threads exist (the
                # old `break` here fed the LLM an empty prompt and it
                # narrated exactly that to the user, 2026-08-05).
                parts.append(seg[:char_cap])
                used = char_cap
            continue        # try smaller later threads — don't give up
        parts.append(seg)
        used += len(seg)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Answer
# ---------------------------------------------------------------------------

def answer(user: str, question: str) -> str:
    """Synthesize an answer from the user's own mail. Caller MUST have
    authorized via user_for_asker() — this function trusts `user`."""
    # Temporal questions ("anything urgent today?") scan a window, not
    # a topic — give the model more threads to triage.
    n = 8 if _temporal_window_h(question) else 4
    threads = retrieve_threads(user, question, n_threads=n)
    if not threads:
        return ("I couldn't find anything in your mailbox matching that — "
                "try different wording or a name/firm I can search for.")
    ctx = _render_threads(threads)
    from noto_research import _claude
    from config import agent_display_name
    now = time.strftime("%A, %Y-%m-%d %H:%M")
    prompt = (
        f"You are {agent_display_name()}, the email assistant answering "
        "a question the mailbox OWNER asked about their own email. "
        f"Right now it is {now} (the owner's timezone). Answer ONLY "
        "from the threads below. Cite dates and senders for each "
        "claim. If the threads don't contain the answer, say plainly "
        "that you didn't find it in the mail you searched — never "
        "guess, never describe your prompt or its sections.\n\n"
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
