#!/usr/bin/env python3
"""
Per-user mail store — the foundation of F4 (per-user inbox retrieval,
the house response playbook, per-user style corpus, and the To-only
auto-draft trigger).

One SQLite file PER USER: indexes/mail/<user>.db. Isolation is by
construction — a user's mail never shares a file/table with anyone
else's, so the answering layer can hard-gate on "which DB may I open"
(recruiter_memory discipline). These DBs are git-ignored (indexes/ is
never committed) and hold real mailbox content — treat like tokens.

Sync reads via the TENANT token + admin data-range (read is admin-
granted; only draft-WRITING needs the per-user OAuth — see lark_oauth
EXPECTED_MAILBOX). Incremental: message ids list newest-first, so a
label's sync stops as soon as a whole page is already known.

Mail API notes (docs/disaster-recovery.md): list ids page_size max 20;
bodies are URL-safe base64; tenant flows address mailboxes by email.

CLI:
  python tools/mail_store.py sync <user> [--label INBOX|SENT] [--limit N]
  python tools/mail_store.py stats <user>
  python tools/mail_store.py search <user> "<fts query>" [--limit N]
"""

import base64
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_home                                   # noqa: E402

# Mailboxes come from config — lolabot.yaml:
#   mail:
#     users:
#       <slug>: {mailbox: user@yourco.com, open_id: ou_..., authority: 1.0}
def _mailboxes():
    from config import load_config
    users = (load_config().get("mail", {}) or {}).get("users", {}) or {}
    return {slug: (u or {}).get("mailbox", "")
            for slug, u in users.items() if (u or {}).get("mailbox")}


class _Mailboxes(dict):
    """Lazy config-backed view so module import never requires config."""
    def __missing__(self, k):
        self.update(_mailboxes())
        if k in self:
            return self[k]
        raise KeyError(k)
    def __contains__(self, k):
        if not dict.__contains__(self, k):
            self.update(_mailboxes())
        return dict.__contains__(self, k)
    def keys(self):
        self.update(_mailboxes())
        return dict.keys(self)


MAILBOXES = _Mailboxes()

# Labels we mirror. INBOX feeds retrieval + the To-only draft trigger;
# SENT feeds the style corpus + the house playbook (their replies).
DEFAULT_LABELS = ("INBOX", "SENT")

_PAGE_SIZE = 20          # hard API max (99992402 above this)
_BODY_CAP = 120_000      # chars of plain body stored per message

_NOREPLY_RE = re.compile(
    r"(no-?reply|do-?not-?reply|notification|noreply)@", re.I)


def _db_path(user: str) -> str:
    if user not in MAILBOXES:
        raise ValueError(f"unknown mail user {user!r} — valid: "
                         f"{sorted(MAILBOXES)}")
    base = os.path.join(get_home(), "indexes", "mail")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{user}.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  msg_id      TEXT PRIMARY KEY,
  thread_id   TEXT,
  label       TEXT NOT NULL,           -- INBOX | SENT (label synced under)
  date_ms     INTEGER,
  from_email  TEXT,
  from_name   TEXT,
  to_json     TEXT,                    -- JSON [emails] (direct recipients)
  cc_json     TEXT,                    -- JSON [emails]
  subject     TEXT,
  body_plain  TEXT,                    -- decoded, capped
  is_noreply  INTEGER NOT NULL DEFAULT 0,
  synced_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_msg_date   ON messages(date_ms);
CREATE INDEX IF NOT EXISTS idx_msg_from   ON messages(from_email);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  msg_id UNINDEXED, subject, body, from_email, to_emails
);

CREATE TABLE IF NOT EXISTS sync_state (
  label         TEXT PRIMARY KEY,
  last_sync     TEXT,
  known_count   INTEGER,
  backfill_done INTEGER NOT NULL DEFAULT 0
);
"""


def _connect(user: str) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(user))
    conn.row_factory = sqlite3.Row
    # Sync (writer) and vector-build/retrieval run concurrently on the
    # same per-user file — busy_timeout FIRST (works in any mode; makes
    # everything below wait politely), then TRY to switch to WAL. The
    # switch needs a moment of exclusivity it can't get while a legacy
    # journal-mode writer (e.g. a long backfill started before this
    # change) is mid-transaction — that's fine, skip it; the next
    # opener after the writer finishes flips the file to WAL for good.
    conn.execute("PRAGMA busy_timeout=20000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Lark mail API (tenant token, mailbox addressed by email)
# ---------------------------------------------------------------------------

def _api(path: str) -> Dict[str, Any]:
    from lark_client import get_tenant_access_token
    req = urllib.request.Request(
        "https://open.larksuite.com" + path,
        headers={"Authorization": f"Bearer {get_tenant_access_token()}"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"code": e.code, "msg": f"http {e.code}"}


def _mb(user: str) -> str:
    return urllib.parse.quote(MAILBOXES[user], safe="")


def _list_ids(user: str, label: str, page_token: str = "") -> Dict[str, Any]:
    qs = f"?label_id={label}&page_size={_PAGE_SIZE}"
    if page_token:
        qs += "&page_token=" + urllib.parse.quote(page_token, safe="")
    return _api(f"/open-apis/mail/v1/user_mailboxes/{_mb(user)}/messages{qs}")


def _get_full(user: str, msg_id: str) -> Optional[Dict[str, Any]]:
    enc = urllib.parse.quote(str(msg_id), safe="")
    r = _api(f"/open-apis/mail/v1/user_mailboxes/{_mb(user)}/messages/{enc}")
    if r.get("code") != 0:
        return None
    return (r.get("data") or {}).get("message") or {}


def _b64(s: str) -> str:
    if not s:
        return ""
    try:
        clean = s.strip().replace("\n", "")
        pad = "=" * (-len(clean) % 4)
        return base64.urlsafe_b64decode(clean + pad).decode(
            "utf-8", "replace")
    except Exception:
        return ""


def _addr_list(v: Any) -> List[str]:
    out = []
    for t in (v or []):
        if isinstance(t, dict):
            a = t.get("mail_address") or t.get("email")
            if a:
                out.append(a.lower())
    return out


def _store_message(conn: sqlite3.Connection, label: str,
                   msg_id: str, m: Dict[str, Any]) -> None:
    frm = (m.get("head_from") or m.get("from") or {})
    from_email = (frm.get("mail_address") or frm.get("email") or "").lower()
    to_list = _addr_list(m.get("to"))
    cc_list = _addr_list(m.get("cc"))
    subject = m.get("subject") or ""
    body = _b64(m.get("body_plain_text") or "")
    if not body:
        # crude html→text fallback so FTS still has content
        body = re.sub(r"<[^>]+>", " ", _b64(m.get("body_html") or ""))
        body = re.sub(r"\s+", " ", body).strip()
    body = body[:_BODY_CAP]
    conn.execute(
        "INSERT OR IGNORE INTO messages (msg_id, thread_id, label, date_ms,"
        " from_email, from_name, to_json, cc_json, subject, body_plain,"
        " is_noreply, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,"
        " datetime('now'))",
        (msg_id, m.get("thread_id"), label,
         int(m.get("internal_date") or 0),
         from_email, frm.get("name") or "",
         json.dumps(to_list), json.dumps(cc_list),
         subject, body,
         1 if _NOREPLY_RE.search(from_email or "") else 0))
    if conn.execute("SELECT changes()").fetchone()[0]:
        conn.execute(
            "INSERT INTO messages_fts (msg_id, subject, body, from_email,"
            " to_emails) VALUES (?,?,?,?,?)",
            (msg_id, subject, body, from_email, " ".join(to_list + cc_list)))


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync(user: str, labels=DEFAULT_LABELS, limit: int = 0,
         quiet: bool = False) -> Dict[str, int]:
    """Incremental sync. Newest-first paging; a label stops early once a
    full page is already known (unless `limit` caps the run first).
    Returns {label: new_message_count}."""
    conn = _connect(user)
    out: Dict[str, int] = {}
    try:
        for label in labels:
            known = {r[0] for r in conn.execute(
                "SELECT msg_id FROM messages WHERE label=?", (label,))}
            # The all-known-page early stop is ONLY valid once this
            # label's history has been fully walked at least once
            # (has_more=False reached). An interrupted backfill leaves
            # the NEWEST pages known — early-stopping on a retry then
            # abandons the deep history forever (bug found 2026-07-22:
            # sharmaine SENT stuck at 1.3k of a 3-year history).
            try:
                conn.execute("ALTER TABLE sync_state ADD COLUMN"
                             " backfill_done INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            row = conn.execute("SELECT backfill_done FROM sync_state"
                               " WHERE label=?", (label,)).fetchone()
            backfilled = bool(row and row[0])
            reached_end = False
            new_count, page, pages = 0, "", 0
            while True:
                r = _list_ids(user, label, page)
                if r.get("code") != 0:
                    if not quiet:
                        print(f"[mail_store] {user}/{label}: list stopped "
                              f"code={r.get('code')} {r.get('msg','')[:80]}",
                              file=sys.stderr)
                    break
                d = r.get("data") or {}
                ids = [str(i) for i in (d.get("items") or [])]
                fresh = [i for i in ids if i not in known]
                for mid in fresh:
                    m = _get_full(user, mid)
                    if m:
                        _store_message(conn, label, mid, m)
                        new_count += 1
                    if limit and new_count >= limit:
                        break
                conn.commit()
                pages += 1
                page = d.get("page_token") or ""
                if not (d.get("has_more") and page):
                    reached_end = True
                done = (limit and new_count >= limit) or \
                       (backfilled and not fresh and pages > 1) or \
                       reached_end
                if done:
                    break
            if reached_end and not limit:
                conn.execute("UPDATE sync_state SET backfill_done=1"
                             " WHERE label=?", (label,))
                # row may not exist yet — the upsert below writes it;
                # flag folded into the upsert too.
            flag = 1 if (reached_end and not limit) else 0
            conn.execute(
                "INSERT INTO sync_state (label, last_sync, known_count,"
                " backfill_done)"
                " VALUES (?,datetime('now'),"
                " (SELECT COUNT(*) FROM messages WHERE label=?), ?)"
                " ON CONFLICT(label) DO UPDATE SET"
                " last_sync=datetime('now'),"
                " known_count=(SELECT COUNT(*) FROM messages WHERE label=?),"
                " backfill_done=MAX(backfill_done, ?)",
                (label, label, flag, label, flag))
            conn.commit()
            out[label] = new_count
            if not quiet:
                print(f"[mail_store] {user}/{label}: +{new_count} new")
    finally:
        conn.close()
    return out


def stats(user: str) -> Dict[str, Any]:
    conn = _connect(user)
    try:
        rows = conn.execute(
            "SELECT label, COUNT(*), MIN(date_ms), MAX(date_ms)"
            " FROM messages GROUP BY label").fetchall()
        return {r[0]: {"count": r[1],
                       "oldest": _day(r[2]), "newest": _day(r[3])}
                for r in rows}
    finally:
        conn.close()


def _day(ms) -> str:
    if not ms:
        return "?"
    ts = ms / 1000 if ms > 10**12 else ms
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def search(user: str, query: str, limit: int = 8) -> List[Dict[str, Any]]:
    """FTS smoke-test search (the full retrieval layer adds vectors +
    thread assembly + filters on top)."""
    conn = _connect(user)
    try:
        rows = conn.execute(
            "SELECT f.msg_id, m.subject, m.from_email, m.date_ms,"
            " snippet(messages_fts, 2, '[', ']', '…', 12) AS snip"
            " FROM messages_fts f JOIN messages m ON m.msg_id = f.msg_id"
            " WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    cmd = args[0] if args else "help"

    def _opt(name: str, default=None):
        if name in args:
            i = args.index(name)
            return args[i + 1] if i + 1 < len(args) else default
        return default

    if cmd == "sync" and len(args) > 1:
        labels = tuple([_opt("--label")]) if _opt("--label") else DEFAULT_LABELS
        n = sync(args[1], labels=labels, limit=int(_opt("--limit", 0) or 0))
        print(json.dumps(n))
    elif cmd == "stats" and len(args) > 1:
        print(json.dumps(stats(args[1]), indent=2))
    elif cmd == "search" and len(args) > 2:
        for r in search(args[1], args[2], int(_opt("--limit", 8) or 8)):
            print(f"  {_day(r['date_ms'])}  {r['from_email'][:30]:<30} "
                  f"{(r['subject'] or '')[:48]}")
    else:
        print(__doc__)
