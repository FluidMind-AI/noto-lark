#!/usr/bin/env python3
"""
The VP House Response Playbook — "the holy grail of answering emails."

Mines the pilot admins' SENT replies (Alejandro / Alexis / Sharmaine)
into a SHARED database of situation-typed response patterns: what kind
of question/situation it was, how the house handles it, the tone, and
the actual reply as an exemplar. This is the canonical reference every
VP draft starts from — the drafter retrieves matching entries FIRST and
layers the individual's personal style on top only IF needed.

Unlike the per-user mail stores (strictly isolated), this DB is
deliberately communal — but the SOURCE mailboxes are read via each
user's own store, and anything the classifier marks personal is
skipped and never mined (verdict recorded, content NOT stored).

Store: indexes/email_playbook.db (git-ignored with the rest of indexes/).

CLI:
  python tools/email_playbook.py mine <user> [--limit N]   # incremental
  python tools/email_playbook.py stats
  python tools/email_playbook.py show <id>
  python tools/email_playbook.py search "<query>"
"""

import json
import os
import re
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_home                                   # noqa: E402
import mail_store                                              # noqa: E402

# Whose sent mail is canon, and how much weight it carries when the
# drafter has competing exemplars.
def _authority():
    """Per-user exemplar weight from config (mail.users.<slug>.authority,
    default 1.0)."""
    from config import load_config
    users = (load_config().get("mail", {}) or {}).get("users", {}) or {}
    return {slug: float((u or {}).get("authority", 1.0) or 1.0)
            for slug, u in users.items()}


class _Authority(dict):
    def __missing__(self, k):
        self.update(_authority())
        return dict.__getitem__(self, k)
    def __contains__(self, k):
        if not dict.__contains__(self, k):
            self.update(_authority())
        return dict.__contains__(self, k)


AUTHORITY = _Authority()

# Seed taxonomy — the LLM may add new kebab-case types when none fits;
# they show up in stats for periodic normalization.
SEED_TYPES = (
    "scheduling", "follow-up-chase", "status-update", "negotiation",
    "delivering-bad-news", "pushback-handling", "intro-pitch",
    "document-request", "referral-handling", "pricing-terms",
    "escalation", "reassurance", "admin-logistics")

_BATCH = 4          # inbound→reply pairs per LLM call
_INBOUND_CAP = 1600  # chars of inbound context per pair
_REPLY_CAP = 2600    # chars of the reply (the exemplar)


def _db_path() -> str:
    return os.path.join(get_home(), "indexes", "email_playbook.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  situation_type  TEXT NOT NULL,
  situation       TEXT NOT NULL,       -- 1–2 sentence description
  approach        TEXT NOT NULL,       -- how the house handles it
  tone            TEXT,                -- tone/register notes
  exemplar        TEXT NOT NULL,       -- the actual house reply (trimmed)
  tags            TEXT,                -- JSON list
  source_user     TEXT NOT NULL,       -- alejandro | alexis | sharmaine
  authority       REAL NOT NULL,
  source_msg_id   TEXT NOT NULL,
  thread_subject  TEXT,
  sent_date       TEXT,
  status          TEXT NOT NULL DEFAULT 'active',
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pb_type ON entries(situation_type);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
  entry_id UNINDEXED, situation, approach, exemplar, tags
);

-- Mining cursor: every examined sent message gets a verdict so reruns
-- never re-mine (or re-expose) the same message.
CREATE TABLE IF NOT EXISTS mined (
  msg_id   TEXT PRIMARY KEY,
  user     TEXT NOT NULL,
  verdict  TEXT NOT NULL,   -- entry | personal | trivial | no-context | error
  entry_id INTEGER,
  at       TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=20000")
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Pair harvesting — a SENT reply + the inbound it answers
# ---------------------------------------------------------------------------

def _harvest_pairs(user: str, limit: int) -> List[Dict[str, Any]]:
    """SENT messages not yet mined, each with the latest inbound message
    that precedes it in its thread (the thing being replied to)."""
    pb = _connect()
    try:
        done = {r[0] for r in pb.execute(
            "SELECT msg_id FROM mined WHERE user=?", (user,))}
    finally:
        pb.close()
    mc = mail_store._connect(user)
    try:
        sent = [dict(r) for r in mc.execute(
            "SELECT msg_id, thread_id, date_ms, subject, body_plain,"
            " to_json FROM messages WHERE label='SENT'"
            " ORDER BY date_ms DESC")]
        pairs = []
        for s in sent:
            if s["msg_id"] in done:
                continue
            inbound = mc.execute(
                "SELECT from_email, from_name, body_plain, subject"
                " FROM messages WHERE thread_id=? AND label='INBOX'"
                " AND date_ms < ? ORDER BY date_ms DESC LIMIT 1",
                (s["thread_id"], s["date_ms"] or 0)).fetchone()
            pairs.append({
                "msg_id": s["msg_id"],
                "subject": s["subject"] or "",
                "date_ms": s["date_ms"],
                "reply": (s["body_plain"] or "")[:_REPLY_CAP],
                "inbound_from": (inbound["from_email"] if inbound else ""),
                "inbound": ((inbound["body_plain"] or "")[:_INBOUND_CAP]
                            if inbound else ""),
            })
            if len(pairs) >= limit:
                break
        return pairs
    finally:
        mc.close()


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

_PROMPT = """You are building the Vargas Partners (legal recruiting firm) \
HOUSE EMAIL PLAYBOOK: the canonical database of how VP answers different \
kinds of emails. Below are {n} email exchanges — an inbound message and the \
reply a VP principal sent.

For EACH exchange, output one JSON object:
- "idx": the exchange number
- "personal": true if this is clearly a personal (non-work) email — family, \
friends, private finances, personal travel, medical. When true, set every \
other field to "" and DO NOT summarize the content.
- "trivial": true if there is no reusable craft (bare "thanks", "got it", \
pure scheduling ping with no technique, automated/no-reply).
- "situation_type": kebab-case type. Prefer one of: {types}. Invent a new \
kebab-case type ONLY if none fits.
- "situation": 1–2 sentences describing the situation/question being handled \
(generic — no candidate/client names).
- "approach": 2–4 sentences on HOW the reply handles it — the moves, the \
ordering, what it commits to vs. deflects, what it never says. This is the \
teachable craft.
- "tone": one line on register (e.g. "warm but direct, no hedging").
- "tags": 2–5 short topical tags.

Weigh the REPLY as the craft; the inbound is context. Output STRICT JSON: \
an array of {n} objects, nothing else.

Types: {types}

EXCHANGES:
{body}"""


def _extract_batch(pairs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from noto_research import _claude
    body = ""
    for i, p in enumerate(pairs, 1):
        body += (f"\n--- EXCHANGE {i} ---\n"
                 f"SUBJECT: {p['subject']}\n"
                 f"INBOUND (from {p['inbound_from'] or 'unknown'}):\n"
                 f"{p['inbound'] or '(no inbound found — outbound-initiated)'}\n"
                 f"VP REPLY:\n{p['reply']}\n")
    prompt = _PROMPT.format(n=len(pairs), types=", ".join(SEED_TYPES),
                            body=body)
    raw = _claude(prompt, timeout=240, web=False) or ""
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        out = json.loads(m.group(0))
        return out if isinstance(out, list) else []
    except Exception:
        return []


def mine(user: str, limit: int = 40, quiet: bool = False) -> Dict[str, int]:
    if user not in AUTHORITY:
        raise ValueError(f"not a playbook source: {user!r}")
    pairs = _harvest_pairs(user, limit)
    counts = {"entry": 0, "personal": 0, "trivial": 0,
              "no-context": 0, "error": 0}
    pb = _connect()
    try:
        for i in range(0, len(pairs), _BATCH):
            batch = pairs[i:i + _BATCH]
            results = _extract_batch(batch)
            by_idx = {int(r.get("idx", 0)): r for r in results
                      if isinstance(r, dict)}
            for j, p in enumerate(batch, 1):
                r = by_idx.get(j)
                now = time.strftime("%Y-%m-%d %H:%M:%S")
                day = ""
                if p["date_ms"]:
                    ts = p["date_ms"]
                    day = time.strftime(
                        "%Y-%m-%d",
                        time.localtime(ts / 1000 if ts > 10**12 else ts))
                if not r:
                    verdict, entry_id = "error", None
                elif r.get("personal"):
                    verdict, entry_id = "personal", None
                elif r.get("trivial"):
                    verdict, entry_id = "trivial", None
                elif not (r.get("situation") and r.get("approach")):
                    verdict, entry_id = "error", None
                else:
                    cur = pb.execute(
                        "INSERT INTO entries (situation_type, situation,"
                        " approach, tone, exemplar, tags, source_user,"
                        " authority, source_msg_id, thread_subject,"
                        " sent_date, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (r.get("situation_type") or "uncategorized",
                         r["situation"], r["approach"], r.get("tone") or "",
                         p["reply"], json.dumps(r.get("tags") or []),
                         user, AUTHORITY[user], p["msg_id"],
                         p["subject"], day, now))
                    entry_id = cur.lastrowid
                    pb.execute(
                        "INSERT INTO entries_fts (entry_id, situation,"
                        " approach, exemplar, tags) VALUES (?,?,?,?,?)",
                        (entry_id, r["situation"], r["approach"],
                         p["reply"], " ".join(r.get("tags") or [])))
                    verdict = "entry"
                pb.execute(
                    "INSERT OR REPLACE INTO mined (msg_id, user, verdict,"
                    " entry_id, at) VALUES (?,?,?,?,?)",
                    (p["msg_id"], user, verdict, entry_id, now))
                counts[verdict] += 1
            pb.commit()
            if not quiet:
                print(f"[playbook] {user}: batch {i//_BATCH + 1} → {counts}")
    finally:
        pb.close()
    return counts


# ---------------------------------------------------------------------------
# Retrieval (drafting + review)
# ---------------------------------------------------------------------------

def search(query: str, k: int = 6) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        terms = [t for t in re.findall(r"[A-Za-z0-9-]+", query) if len(t) > 1]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms[:12])
        rows = conn.execute(
            "SELECT e.* FROM entries_fts f JOIN entries e ON e.id=f.entry_id"
            " WHERE entries_fts MATCH ? AND e.status='active'"
            " ORDER BY rank LIMIT ?", (match, k * 3)).fetchall()
        # authority-weighted re-rank within the FTS candidates
        return sorted((dict(r) for r in rows),
                      key=lambda r: -r["authority"])[:k]
    finally:
        conn.close()


def stats() -> Dict[str, Any]:
    conn = _connect()
    try:
        return {
            "entries": conn.execute(
                "SELECT COUNT(*) FROM entries").fetchone()[0],
            "by_type": dict(conn.execute(
                "SELECT situation_type, COUNT(*) FROM entries"
                " GROUP BY situation_type ORDER BY COUNT(*) DESC")),
            "by_user": dict(conn.execute(
                "SELECT source_user, COUNT(*) FROM entries"
                " GROUP BY source_user")),
            "verdicts": dict(conn.execute(
                "SELECT verdict, COUNT(*) FROM mined GROUP BY verdict")),
        }
    finally:
        conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "help"
    if cmd == "mine" and len(args) > 1:
        lim = int(args[args.index("--limit") + 1]) if "--limit" in args else 40
        print(json.dumps(mine(args[1], limit=lim)))
    elif cmd == "stats":
        print(json.dumps(stats(), indent=2))
    elif cmd == "show" and len(args) > 1:
        conn = _connect()
        r = conn.execute("SELECT * FROM entries WHERE id=?",
                         (int(args[1]),)).fetchone()
        print(json.dumps(dict(r), indent=2) if r else "not found")
    elif cmd == "search" and len(args) > 1:
        for r in search(" ".join(args[1:])):
            print(f"  #{r['id']} [{r['situation_type']}] "
                  f"({r['source_user']}) {r['situation'][:70]}")
    else:
        print(__doc__)
