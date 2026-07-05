"""
Chat-corpus ingestion — pulls message history from every group chat
the bot has access to (minus exclusions) into a local store. The raw
messages live in indexes/chat_messages.db; the LLM extractor
(chat_nuggets.py) reads from here to identify Q&A nuggets.

Source of truth for the new "chat-derived knowledge" layer that sits
alongside the existing docs/entities/RAG backend. The goal: capture
the Q&A gold currently disappearing into Lark chat history so the
agent can reuse it instead of guessing.

Exclusions: chats listed in lark.chat_corpus_excluded_chats (Management
chat by default — admin-only discussions never feed the user-
shared knowledge base).

  python tools/chat_corpus.py sync-all                # nightly delta
  python tools/chat_corpus.py sync <chat_id> [--full] # one chat
  python tools/chat_corpus.py stats                   # per-chat counts
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id          TEXT PRIMARY KEY,
    chat_name        TEXT,
    chat_type        TEXT,            -- group | p2p | topic
    member_count     INTEGER,
    last_synced_at   TEXT,
    oldest_msg_at    TEXT,
    newest_msg_at    TEXT,
    discovered_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    msg_id           TEXT PRIMARY KEY,
    chat_id          TEXT NOT NULL,
    parent_id        TEXT,            -- reply target (for threading)
    root_id          TEXT,            -- thread root (if part of a thread)
    sender_open_id   TEXT,
    sender_name      TEXT,
    sender_authority TEXT,            -- super_admin | admin | authoritative | standard | bot
    msg_type         TEXT,            -- text | post | file | image | sticker | ...
    text             TEXT,            -- normalized plain text for text/post messages
    created_at_ms    INTEGER NOT NULL,
    fetched_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_chat ON chat_messages(chat_id, created_at_ms);
CREATE INDEX IF NOT EXISTS idx_msg_thread ON chat_messages(chat_id, root_id);
CREATE INDEX IF NOT EXISTS idx_msg_sender ON chat_messages(sender_open_id);
"""


def _home() -> str:
    from config import get_home
    return get_home()


def _db_path() -> str:
    return os.path.join(_home(), "indexes", "chat_messages.db")


def _connect() -> sqlite3.Connection:
    p = _db_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    db = sqlite3.connect(p, timeout=30.0)
    from sqlite_utils import harden
    harden(db)
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    db.commit()
    return db


def _excluded_chats() -> set:
    try:
        from config import load_config
        return set((load_config().get("lark", {})
                    .get("chat_corpus_excluded_chats", []) or []))
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Lark API helpers — list chats + walk messages
# ---------------------------------------------------------------------------

def _list_user_chats(client: Any) -> List[Dict[str, Any]]:
    """All chats the BOT has been added to. Uses tenant_access_token
    (the user_token doesn't carry im:chat scope; the bot already has
    im scopes because it receives message webhooks)."""
    import requests
    from lark_client import get_tenant_access_token
    from config import load_config
    base = load_config()["lark"].get(
        "base_url", "https://open.larksuite.com").rstrip("/")
    token = get_tenant_access_token()
    out: List[Dict[str, Any]] = []
    page_token = None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(f"{base}/open-apis/im/v1/chats",
                         headers={"Authorization": f"Bearer {token}"},
                         params=params, timeout=20)
        d = r.json()
        if d.get("code") != 0:
            print(f"[chat_corpus] list chats err: {d.get('msg')}",
                  file=sys.stderr, flush=True)
            break
        items = (d.get("data") or {}).get("items", []) or []
        out.extend(items)
        if not (d.get("data") or {}).get("has_more"):
            break
        page_token = (d.get("data") or {}).get("page_token")
        if not page_token:
            break
    return out


def _walk_chat_messages(client: Any, chat_id: str,
                        start_ms: Optional[int] = None,
                        verbose: bool = True) -> List[Dict[str, Any]]:
    """Page through a chat's messages from oldest forward (or
    `start_ms`+1 if continuing an incremental sync). Returns the raw
    Lark message dicts (caller normalizes + stores). Uses tenant
    token — bot has im:message scope from being added to the chat."""
    import requests
    from lark_client import get_tenant_access_token
    from config import load_config
    base = load_config()["lark"].get(
        "base_url", "https://open.larksuite.com").rstrip("/")
    token = get_tenant_access_token()
    out: List[Dict[str, Any]] = []
    page_token = None
    # Lark's list_messages takes container_id + container_id_type=chat
    # and supports start_time / end_time (seconds). We page until done.
    while True:
        params = {
            "container_id_type": "chat",
            "container_id":      chat_id,
            "sort_type":         "ByCreateTimeAsc",
            "page_size":         50,
        }
        if start_ms:
            params["start_time"] = str(int(start_ms // 1000) + 1)
        if page_token:
            params["page_token"] = page_token
        r = requests.get(f"{base}/open-apis/im/v1/messages",
                         headers={"Authorization": f"Bearer {token}"},
                         params=params, timeout=30)
        d = r.json()
        if d.get("code") != 0:
            if verbose:
                print(f"  [warn] list_messages {chat_id}: "
                      f"{d.get('msg','')[:80]}", flush=True)
            break
        items = (d.get("data") or {}).get("items", []) or []
        out.extend(items)
        if not (d.get("data") or {}).get("has_more"):
            break
        page_token = (d.get("data") or {}).get("page_token")
        if not page_token:
            break
        # tiny pause to be kind to rate limits
        time.sleep(0.1)
    return out


# ---------------------------------------------------------------------------
# Normalize a Lark message + resolve sender authority
# ---------------------------------------------------------------------------

def _extract_text(msg: Dict[str, Any]) -> str:
    """Pull plain text out of a Lark message body (text or post types).
    Returns '' for non-text types (files, images, stickers, etc.)."""
    body = msg.get("body") or {}
    content = body.get("content") or ""
    msg_type = msg.get("msg_type") or "text"
    if msg_type == "text":
        try:
            j = json.loads(content) if content else {}
            return (j.get("text") or "").strip()
        except Exception:
            return content.strip()
    if msg_type == "post":
        try:
            j = json.loads(content) if content else {}
            # post is {title, content: [[{tag:text,text:'...'},...],...]}
            buf = []
            buf.append(j.get("title") or "")
            for line in (j.get("content") or []):
                for seg in line:
                    if isinstance(seg, dict):
                        buf.append(seg.get("text") or "")
            return "\n".join(s for s in buf if s).strip()
        except Exception:
            return ""
    # files / images / stickers / sharing / cards / etc.: drop body for
    # now (extractor uses text-only Q&A; non-text is logged but blank).
    return ""


def _resolve_sender(msg: Dict[str, Any]) -> Tuple[str, str, str]:
    """Returns (open_id, name, authority). Looks up name + authority
    from operators.yaml via feedback_capture; falls back to msg sender
    fields if unknown."""
    from feedback_capture import authority_for, name_for, _bot_open_id
    s = (msg.get("sender") or {})
    oid = (s.get("id") or "") if s.get("id_type") == "open_id" \
        else (s.get("sender_id", {}).get("open_id") if isinstance(s.get("sender_id"), dict)
              else "")
    if not oid:
        # newer schema: msg.sender.id with id_type indicating open_id
        sender_id = s.get("id") or ""
        oid = sender_id if sender_id.startswith("ou_") else ""
    sender_type = s.get("sender_type") or ""
    if sender_type == "app" or oid == _bot_open_id():
        return oid or _bot_open_id(), "Noto", "bot"
    name = name_for(oid) or ""
    authority = authority_for(oid) or "standard"
    return oid, name, authority


# ---------------------------------------------------------------------------
# Sync — one chat OR all-accessible
# ---------------------------------------------------------------------------

def sync_chat(client: Any, chat_id: str,
              full: bool = False,
              verbose: bool = True) -> Dict[str, int]:
    """Pull messages for one chat. Incremental by default (only since
    last newest_msg_at); --full to re-walk from beginning."""
    if chat_id in _excluded_chats():
        if verbose:
            print(f"  [skip] {chat_id} (excluded)", flush=True)
        return {"chat_id": chat_id, "added": 0, "skipped": "excluded"}

    db = _connect()
    try:
        prior = db.execute("SELECT newest_msg_at FROM chats WHERE chat_id=?",
                            (chat_id,)).fetchone()
    finally:
        db.close()
    start_ms = None
    if not full and prior and prior["newest_msg_at"]:
        try:
            start_ms = int(datetime.fromisoformat(
                prior["newest_msg_at"]).timestamp() * 1000)
        except Exception:
            start_ms = None

    if verbose:
        print(f"  syncing {chat_id} (start_ms={start_ms})…", flush=True)
    msgs = _walk_chat_messages(client, chat_id, start_ms=start_ms,
                                verbose=verbose)
    if verbose:
        print(f"  fetched {len(msgs)} messages from {chat_id}",
              flush=True)

    added = 0
    oldest_ms = None
    newest_ms = None
    db = _connect()
    try:
        for m in msgs:
            mid = m.get("message_id")
            if not mid:
                continue
            try:
                created_ms = int(m.get("create_time") or "0")
            except Exception:
                created_ms = 0
            if created_ms == 0:
                continue
            if oldest_ms is None or created_ms < oldest_ms:
                oldest_ms = created_ms
            if newest_ms is None or created_ms > newest_ms:
                newest_ms = created_ms
            oid, name, authority = _resolve_sender(m)
            try:
                cur = db.execute(
                    "INSERT OR IGNORE INTO chat_messages "
                    "(msg_id, chat_id, parent_id, root_id, "
                    "sender_open_id, sender_name, sender_authority, "
                    "msg_type, text, created_at_ms, fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, chat_id, m.get("parent_id"), m.get("root_id"),
                     oid, name, authority,
                     m.get("msg_type"), _extract_text(m),
                     created_ms,
                     datetime.now(timezone.utc).isoformat(timespec="seconds")))
                if cur.rowcount:
                    added += 1
            except Exception as e:
                if verbose:
                    print(f"  [warn] insert {mid}: {str(e)[:60]}",
                          flush=True)
        # update chat row
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        oldest_iso = (datetime.fromtimestamp(oldest_ms / 1000)
                      .isoformat(timespec="seconds") if oldest_ms else None)
        newest_iso = (datetime.fromtimestamp(newest_ms / 1000)
                      .isoformat(timespec="seconds") if newest_ms else None)
        existing = db.execute("SELECT 1 FROM chats WHERE chat_id=?",
                               (chat_id,)).fetchone()
        if existing:
            db.execute(
                "UPDATE chats SET last_synced_at=?, "
                "oldest_msg_at=COALESCE(MIN(?, oldest_msg_at), ?), "
                "newest_msg_at=COALESCE(MAX(?, newest_msg_at), ?) "
                "WHERE chat_id=?",
                (now_iso, oldest_iso, oldest_iso,
                 newest_iso, newest_iso, chat_id))
        else:
            db.execute(
                "INSERT INTO chats (chat_id, last_synced_at, "
                "oldest_msg_at, newest_msg_at, discovered_at) "
                "VALUES (?,?,?,?,?)",
                (chat_id, now_iso, oldest_iso, newest_iso, now_iso))
        db.commit()
    finally:
        db.close()

    return {"chat_id": chat_id, "fetched": len(msgs), "added": added}


def sync_all(full: bool = False, verbose: bool = True) -> Dict[str, Any]:
    """Walk every accessible group chat (minus exclusions) and sync."""
    from lark_client import LarkClient
    from lark_oauth import get_user_token
    try:
        client = LarkClient(user_token=get_user_token())
    except Exception:
        client = LarkClient()
    chats = _list_user_chats(client)
    if verbose:
        print(f"[chat_corpus] discovered {len(chats)} chats; "
              f"{len(_excluded_chats())} excluded", flush=True)
    # filter: group chats only, exclude listed
    targets = [c for c in chats
               if (c.get("chat_mode") or c.get("chat_type") or "") in
               ("group", "topic")
               and (c.get("chat_id") or "") not in _excluded_chats()]
    # update chats table with discovered metadata
    db = _connect()
    try:
        for c in targets:
            cid = c.get("chat_id")
            if not cid:
                continue
            db.execute(
                "INSERT OR IGNORE INTO chats (chat_id, chat_name, "
                "chat_type, discovered_at) VALUES (?,?,?,?)",
                (cid, c.get("name"), c.get("chat_mode") or "group",
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
            db.execute(
                "UPDATE chats SET chat_name=?, chat_type=? "
                "WHERE chat_id=?",
                (c.get("name"), c.get("chat_mode") or "group", cid))
        db.commit()
    finally:
        db.close()
    out = {"chats_synced": 0, "messages_added": 0,
           "excluded": list(_excluded_chats())}
    for c in targets:
        cid = c.get("chat_id")
        if not cid:
            continue
        if verbose:
            print(f"  → {c.get('name','?')!r} ({cid})", flush=True)
        try:
            res = sync_chat(client, cid, full=full, verbose=verbose)
            out["chats_synced"] += 1
            out["messages_added"] += res.get("added", 0)
        except Exception as e:
            if verbose:
                print(f"  [warn] sync {cid}: {str(e)[:80]}",
                      flush=True)
    if verbose:
        print(f"[chat_corpus] sync_all done — chats={out['chats_synced']} "
              f"new_messages={out['messages_added']}", flush=True)
    return out


def stats() -> Dict[str, Any]:
    db = _connect()
    try:
        chats = db.execute(
            "SELECT chat_id, chat_name, chat_type, oldest_msg_at, "
            "newest_msg_at, last_synced_at, "
            "(SELECT COUNT(*) FROM chat_messages WHERE chat_id=c.chat_id) "
            "AS n_messages FROM chats c ORDER BY n_messages DESC"
        ).fetchall()
        total = db.execute(
            "SELECT COUNT(*) FROM chat_messages").fetchone()[0]
    finally:
        db.close()
    return {
        "total_messages": total,
        "chats": [dict(r) for r in chats],
        "excluded_chats": sorted(_excluded_chats()),
    }


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 0
    cmd = argv[0]
    if cmd == "sync-all":
        full = "--full" in argv
        sync_all(full=full, verbose=True)
        return 0
    if cmd == "sync" and len(argv) >= 2:
        chat_id = argv[1]
        full = "--full" in argv
        from lark_client import LarkClient
        from lark_oauth import get_user_token
        client = LarkClient(user_token=get_user_token())
        res = sync_chat(client, chat_id, full=full, verbose=True)
        print(json.dumps(res, indent=2))
        return 0
    if cmd == "stats":
        st = stats()
        print(f"Total messages: {st['total_messages']}")
        print(f"Excluded chats: {st['excluded_chats']}")
        print(f"\nPer-chat:")
        for c in st["chats"]:
            print(f"  {(c.get('chat_name') or '?')[:45]:45} "
                  f"[{c['chat_type'] or '?':6}] "
                  f"{c['n_messages']:>5} msgs  "
                  f"{(c.get('oldest_msg_at') or '?')[:10]} → "
                  f"{(c.get('newest_msg_at') or '?')[:10]}")
        return 0
    print("commands: sync-all [--full] | sync <chat_id> [--full] | stats",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
