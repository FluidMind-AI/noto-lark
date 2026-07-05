"""
Chat-corpus Phase 2: LLM extracts Q&A nuggets from ingested chat
messages.

Reads chat_messages.db (populated by chat_corpus.py), walks each chat
in fixed-size windows, asks the LLM "is there genuine substantive
Q&A here that another team member would benefit from?" — and writes
extracted nuggets to indexes/chat_nuggets.db.

Status routing per operator spec:
  • authoritative single answerer (super_admin / admin /
        authoritative tier) with no existing conflict
        → status='active' (live in retrieval)
  • non-authoritative answerer    → status='pending' (admin review)
  • conflict detected with prior active nugget on same topic
        → BOTH go to status='pending' (review)
  • everything else               → status='pending'

Each active nugget is also embedded into vectors.db (source_kind='nugget')
so the agent's semantic search surfaces it naturally.

  python tools/chat_nuggets.py extract-pending [--limit N]
  python tools/chat_nuggets.py extract-chat <chat_id>
  python tools/chat_nuggets.py embed-active
  python tools/chat_nuggets.py stats
  python tools/chat_nuggets.py list [--status pending|active|...]
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


_AUTHORITATIVE = {"super_admin", "admin", "authoritative"}
_WINDOW_MSGS = 40                    # messages per LLM extraction call
_MAX_MSG_TEXT_CHARS = 500            # truncate long messages in the prompt


_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_nuggets (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    question          TEXT NOT NULL,
    answer            TEXT NOT NULL,
    topic             TEXT,
    asker_open_id     TEXT,
    asker_name        TEXT,
    answerer_open_id  TEXT NOT NULL,
    answerer_name     TEXT,
    authority         TEXT NOT NULL,    -- super_admin | admin | authoritative | standard
    chat_id           TEXT NOT NULL,
    chat_name         TEXT,
    source_msg_ids    TEXT NOT NULL,    -- JSON array of msg_ids
    confidence        REAL NOT NULL DEFAULT 0.5,
    status            TEXT NOT NULL DEFAULT 'pending',
                                        -- active | pending | rejected | superseded
    conflict_with     INTEGER,          -- nugget id if flagged
    created_at        TEXT NOT NULL,
    reviewed_at       TEXT,
    reviewed_by       TEXT,
    reviewed_note     TEXT,
    embedded_at       TEXT              -- set after vectors.db sync
);
CREATE INDEX IF NOT EXISTS idx_nug_status   ON chat_nuggets(status);
CREATE INDEX IF NOT EXISTS idx_nug_chat     ON chat_nuggets(chat_id);
CREATE INDEX IF NOT EXISTS idx_nug_topic    ON chat_nuggets(topic);
CREATE INDEX IF NOT EXISTS idx_nug_authority ON chat_nuggets(authority);

CREATE TABLE IF NOT EXISTS extraction_progress (
    chat_id            TEXT PRIMARY KEY,
    last_extracted_msg_ms  INTEGER NOT NULL DEFAULT 0,
    last_run_at        TEXT
);
"""


def _home() -> str:
    from config import get_home
    return get_home()


def _db_path() -> str:
    return os.path.join(_home(), "indexes", "chat_nuggets.db")


def _msgs_db_path() -> str:
    return os.path.join(_home(), "indexes", "chat_messages.db")


def _connect() -> sqlite3.Connection:
    p = _db_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    db = sqlite3.connect(p, timeout=30.0)
    from sqlite_utils import harden
    harden(db)
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    # Additive migrations (idempotent; duplicate-column errors ignored).
    # context_note: Noto's corpus cross-check written at approval time —
    # situates the chat answer against the document corpus so retrieval
    # never serves a nugget as a standalone verdict.
    # durability: contextualize()'s verdict — durable | mixed | ephemeral.
    # durable_reframe: the lasting lesson separated from dated specifics.
    # contributors: JSON list of names — everyone whose messages shaped
    # the answer, for nuggets synthesized from a multi-person discussion.
    for ddl in ("ALTER TABLE chat_nuggets ADD COLUMN context_note "
                "TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE chat_nuggets ADD COLUMN durability "
                "TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE chat_nuggets ADD COLUMN durable_reframe "
                "TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE chat_nuggets ADD COLUMN contributors "
                "TEXT NOT NULL DEFAULT '[]'"):
        try:
            db.execute(ddl)
        except sqlite3.OperationalError:
            pass
    db.commit()
    return db


def _msgs_connect() -> sqlite3.Connection:
    db = sqlite3.connect(_msgs_db_path(), timeout=30.0)
    from sqlite_utils import harden
    harden(db)
    db.row_factory = sqlite3.Row
    return db


# ---------------------------------------------------------------------------
# Window builder — read messages in chunks per chat
# ---------------------------------------------------------------------------

def _next_window(chat_id: str, after_ms: int = 0,
                 size: int = _WINDOW_MSGS) -> List[Dict[str, Any]]:
    db = _msgs_connect()
    try:
        rows = db.execute(
            "SELECT msg_id, parent_id, sender_open_id, sender_name, "
            "sender_authority, msg_type, text, created_at_ms FROM "
            "chat_messages WHERE chat_id=? AND created_at_ms > ? "
            "AND text != '' AND text IS NOT NULL "
            "ORDER BY created_at_ms ASC LIMIT ?",
            (chat_id, after_ms, size)).fetchall()
    finally:
        db.close()
    return [dict(r) for r in rows]


def _chat_name(chat_id: str) -> str:
    db = _msgs_connect()
    try:
        r = db.execute(
            "SELECT chat_name FROM chats WHERE chat_id=?",
            (chat_id,)).fetchone()
        return (r["chat_name"] if r else "") or ""
    finally:
        db.close()


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

_EXTRACT_PROMPT = """\
You're reading a chunk of chat history from a team at your \
organization. Extract any GENUINE Q&A pairs that would help a future \
team member facing the same situation.

BE CONSERVATIVE. Most chat is logistics ("did you send X?"), coordination \
("yes I'll handle it"), or social ("congrats!"). DON'T extract those. \
Only extract SUBSTANTIVE Q&A about HOW to do the work: \
handling tricky client or vendor situations, structuring a report, \
internal process knowledge, tool know-how, \
specific policy/process insights, etc.

PREFER DURABLE KNOWLEDGE. Purely time-sensitive facts — a specific \
project's parameters, a deadline that's currently looming, this month's \
timeline — are NOT lessons; projects come and go. Skip them unless \
they carry a lesson that outlives the moment (a vendor's recurring \
behavior, a team dynamic, how to handle a situation). When an \
answer mixes both, make the durable part the core of the answer and \
qualify the dated specifics ("as of <when>, …").

For each Q&A pair you find, output one JSON object on its own line. \
If no real Q&A in this chunk, output an empty array [].

READ THE DISCUSSION AS A WHOLE. An "answer" is often not one message — \
several people chime in, correct each other, and converge. In that case \
synthesize the conclusion of the discussion into the answer, set \
answerer_msg_id to the message that best anchors the conclusion, and \
list EVERY message that contributed substance in contributor_msg_ids so \
all participants are credited.

Schema per pair:
{{"question": "<paraphrased person-agnostic — what another team member \
would re-ask>",
  "answer": "<the substantive answer or the discussion's synthesized \
conclusion — faithful>",
  "asker_msg_id": "<the message_id of the question>",
  "answerer_msg_id": "<the message_id that best anchors the conclusion>",
  "contributor_msg_ids": ["<every message_id whose content shaped the \
answer — may be just the answerer's, may be several people's>"],
  "topic": "<short tag — e.g. 'onboarding', 'expense_policy', \
'vendor_selection', 'tool_intel:crm_rollout'>",
  "confidence": <float 0-1>}}

CHAT CHUNK (chronological):
{messages}

Output a JSON array (even if 0 or 1 items). No prose, no fences.
"""


def _format_window(msgs: List[Dict[str, Any]]) -> str:
    lines = []
    for m in msgs:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        if len(text) > _MAX_MSG_TEXT_CHARS:
            text = text[:_MAX_MSG_TEXT_CHARS] + "…"
        sender = m.get("sender_name") or "?"
        auth = m.get("sender_authority") or "standard"
        marker = ""
        if auth in _AUTHORITATIVE:
            marker = " ⭐"   # signals authoritative answerers to the LLM
        elif auth == "bot":
            marker = " 🤖"
        lines.append(f"[{m['msg_id']}] @{sender}{marker}: {text}")
    return "\n".join(lines)


def extract_window(msgs: List[Dict[str, Any]],
                   verbose: bool = False) -> List[Dict[str, Any]]:
    """Run the extraction LLM on one window. Returns list of nugget dicts."""
    if not msgs:
        return []
    from noto_research import _claude
    prompt = _EXTRACT_PROMPT.format(messages=_format_window(msgs))
    raw = _claude(prompt, timeout=120, web=False) or ""
    # find a JSON array in the output
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, dict)
            and x.get("question") and x.get("answer")]


# ---------------------------------------------------------------------------
# Persist + conflict detection
# ---------------------------------------------------------------------------

def _persist_nugget(nug: Dict[str, Any], chat_id: str,
                     msg_index: Dict[str, Dict[str, Any]],
                     verbose: bool) -> Optional[int]:
    """Validate + persist one extracted nugget. Returns the inserted id
    or None if dropped. Handles authority resolution + conflict
    detection."""
    asker_msg = msg_index.get(nug.get("asker_msg_id") or "")
    answerer_msg = msg_index.get(nug.get("answerer_msg_id") or "")
    if not answerer_msg:
        return None
    answerer_oid = answerer_msg.get("sender_open_id") or ""
    answerer_name = answerer_msg.get("sender_name") or ""
    authority = answerer_msg.get("sender_authority") or "standard"
    if authority == "bot":
        return None      # bot replies aren't team knowledge

    asker_oid = asker_msg.get("sender_open_id") if asker_msg else ""
    asker_name = asker_msg.get("sender_name") if asker_msg else ""

    contrib_ids = [x for x in (nug.get("contributor_msg_ids") or [])
                   if isinstance(x, str) and x in msg_index]
    source_ids = [nug.get("asker_msg_id"), nug.get("answerer_msg_id"),
                  *contrib_ids]
    seen_src: set = set()
    source_ids = [x for x in source_ids
                  if x and not (x in seen_src or seen_src.add(x))]

    # contributors: distinct sender names across contributing messages
    # (bot excluded) — credits everyone in a multi-person discussion.
    contributors: List[str] = []
    for mid in source_ids:
        m = msg_index.get(mid) or {}
        nm = (m.get("sender_name") or "").strip()
        if (m.get("sender_authority") != "bot" and nm
                and nm not in contributors):
            contributors.append(nm)

    # Conflict detection: any existing ACTIVE nugget on same topic with
    # the same canonical question (case-folded, whitespace-collapsed)
    # but a DIFFERENT answer → both flip to pending.
    db = _connect()
    try:
        qkey = _qkey(nug["question"])
        conflict = db.execute(
            "SELECT id, answer, authority FROM chat_nuggets "
            "WHERE status='active' AND topic=? "
            "AND lower(replace(replace(question,' ',''),'?','')) = ?",
            (nug.get("topic") or "", qkey)).fetchone()
        new_status = ("active"
                      if authority in _AUTHORITATIVE
                      else "pending")
        conflict_id = None
        if conflict and _answers_differ(conflict["answer"], nug["answer"]):
            # flip both
            new_status = "pending"
            conflict_id = conflict["id"]
            db.execute("UPDATE chat_nuggets SET status='pending', "
                       "conflict_with=? WHERE id=?",
                       (None, conflict["id"]))   # mark prior as conflict
            if verbose:
                print(f"  ⚠ conflict with existing #{conflict['id']} on "
                      f"topic {nug.get('topic')!r} — both → pending",
                      flush=True)
        cur = db.execute(
            "INSERT INTO chat_nuggets (question, answer, topic, "
            "asker_open_id, asker_name, answerer_open_id, "
            "answerer_name, authority, chat_id, chat_name, "
            "source_msg_ids, contributors, confidence, status, "
            "conflict_with, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (nug["question"].strip(),
             nug["answer"].strip(),
             (nug.get("topic") or "").strip().lower()[:80],
             asker_oid, asker_name, answerer_oid, answerer_name,
             authority, chat_id, _chat_name(chat_id),
             json.dumps(source_ids), json.dumps(contributors),
             float(nug.get("confidence", 0.5) or 0.5),
             new_status, conflict_id,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
        db.commit()
        return cur.lastrowid
    finally:
        db.close()


def _qkey(q: str) -> str:
    return re.sub(r"\s+", "", (q or "").lower()).replace("?", "")


def _answers_differ(a: str, b: str) -> bool:
    """Cheap conflict heuristic: lowercased word-set Jaccard < 0.5
    means meaningfully different. Phase 2 v1; semantic similarity in a
    later pass."""
    sa = set(re.findall(r"\w+", (a or "").lower()))
    sb = set(re.findall(r"\w+", (b or "").lower()))
    if not sa and not sb:
        return False
    inter = len(sa & sb)
    union = len(sa | sb) or 1
    return (inter / union) < 0.5


# ---------------------------------------------------------------------------
# Extract — one chat OR all
# ---------------------------------------------------------------------------

def extract_chat(chat_id: str, verbose: bool = True) -> Dict[str, int]:
    """Walk new messages in this chat in windows, extract nuggets,
    persist. Resumable via extraction_progress table."""
    db = _connect()
    try:
        prog = db.execute(
            "SELECT last_extracted_msg_ms FROM extraction_progress "
            "WHERE chat_id=?", (chat_id,)).fetchone()
        last_ms = (prog["last_extracted_msg_ms"] if prog else 0) or 0
    finally:
        db.close()

    extracted = windows = 0
    if verbose:
        print(f"[chat_nuggets] {chat_id} (from ms={last_ms})",
              flush=True)
    while True:
        msgs = _next_window(chat_id, after_ms=last_ms,
                            size=_WINDOW_MSGS)
        if not msgs:
            break
        msg_index = {m["msg_id"]: m for m in msgs}
        nuggets = extract_window(msgs, verbose=verbose)
        for n in nuggets:
            nid = _persist_nugget(n, chat_id, msg_index,
                                   verbose=verbose)
            if nid:
                extracted += 1
                if verbose:
                    print(f"  ✓ #{nid} [{n.get('topic','?')}] "
                          f"{(n.get('question') or '')[:70]!r}",
                          flush=True)
        last_ms = max(m["created_at_ms"] for m in msgs)
        windows += 1
        # checkpoint progress
        db = _connect()
        try:
            db.execute(
                "INSERT OR REPLACE INTO extraction_progress "
                "(chat_id, last_extracted_msg_ms, last_run_at) "
                "VALUES (?,?,?)",
                (chat_id, last_ms,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
            db.commit()
        finally:
            db.close()
    if verbose:
        print(f"[chat_nuggets] {chat_id} — windows={windows} "
              f"nuggets={extracted}", flush=True)
    return {"chat_id": chat_id, "windows": windows,
            "nuggets": extracted}


def extract_pending(limit: Optional[int] = None,
                    verbose: bool = True) -> Dict[str, int]:
    """Process every chat that has new messages past its last
    extraction watermark."""
    db = _msgs_connect()
    try:
        chats = [r["chat_id"] for r in db.execute(
            "SELECT chat_id FROM chats ORDER BY chat_id").fetchall()]
    finally:
        db.close()
    out = {"chats": 0, "nuggets": 0}
    for cid in chats:
        if limit and out["nuggets"] >= limit:
            break
        res = extract_chat(cid, verbose=verbose)
        out["chats"] += 1
        out["nuggets"] += res["nuggets"]
    if verbose:
        print(f"[chat_nuggets] extract_pending done — chats="
              f"{out['chats']} nuggets={out['nuggets']}", flush=True)
    return out


# ---------------------------------------------------------------------------
# Embed active nuggets into vectors.db so the agent's search() finds them
# ---------------------------------------------------------------------------

def backfill_names(verbose: bool = True) -> Dict[str, int]:
    """Resolve missing sender names across the chat corpus + nuggets.

    Ingest only resolves operators.yaml people, so ~40% of
    chat_messages rows (and consequently nugget attribution) have an
    open_id but no name. Resolution order: operators.yaml → usage.db
    users → Lark chat-members API (read-only, tenant token). Only ever
    FILLS EMPTY name fields — never overwrites. Also derives each
    nugget's contributors list from its source messages when missing."""
    name_map: Dict[str, str] = {}
    # usage.db display names (people who've used the bot)
    try:
        udb = sqlite3.connect(os.path.join(_home(), "indexes", "usage.db"))
        for oid, nm in udb.execute(
                "SELECT open_id, display_name FROM users "
                "WHERE display_name != ''"):
            name_map.setdefault(oid, nm)
        udb.close()
    except Exception:
        pass
    # Lark chat members (authoritative for anyone in the synced chats)
    try:
        from lark_client import LarkClient
        client = LarkClient()
        mdb = _msgs_connect()
        try:
            chat_ids = [r[0] for r in mdb.execute(
                "SELECT DISTINCT chat_id FROM chat_messages")]
        finally:
            mdb.close()
        for cid in chat_ids:
            try:
                for m in client.list_chat_members(cid):
                    if m["open_id"] and m["name"]:
                        name_map.setdefault(m["open_id"], m["name"])
            except Exception as e:
                if verbose:
                    print(f"[chat_nuggets] members({cid[:14]}…): "
                          f"{str(e)[:80]}")
    except Exception as e:
        if verbose:
            print(f"[chat_nuggets] Lark member lookup unavailable: "
                  f"{str(e)[:100]}")
    # operators.yaml (highest quality — apply last so it wins setdefault
    # ordering doesn't matter here; these are usually already named)
    try:
        from feedback_capture import name_for
        for oid in list(name_map):
            nm = name_for(oid)
            if nm:
                name_map[oid] = nm
    except Exception:
        pass

    updated_msgs = 0
    mdb = _msgs_connect()
    try:
        for oid, nm in name_map.items():
            cur = mdb.execute(
                "UPDATE chat_messages SET sender_name=? WHERE "
                "sender_open_id=? AND (sender_name='' OR "
                "sender_name IS NULL)", (nm, oid))
            updated_msgs += cur.rowcount
        mdb.commit()
    finally:
        mdb.close()

    # feedback table: rows captured live from non-operators historically
    # stored the raw open_id as user_name (or nothing) — fix both.
    updated_feedback = 0
    try:
        fdb = sqlite3.connect(os.path.join(_home(), "indexes",
                                           "feedback.db"))
        from sqlite_utils import harden as _h
        _h(fdb)
        for oid, nm in name_map.items():
            cur = fdb.execute(
                "UPDATE feedback SET user_name=? WHERE user_open_id=? "
                "AND (user_name='' OR user_name IS NULL "
                "OR user_name LIKE 'ou_%')", (nm, oid))
            updated_feedback += cur.rowcount
        fdb.commit()
        fdb.close()
    except Exception as e:
        if verbose:
            print(f"[chat_nuggets] feedback name backfill skipped: {e}")

    updated_nuggets = contribs = 0
    db = _connect()
    try:
        for oid, nm in name_map.items():
            cur = db.execute(
                "UPDATE chat_nuggets SET answerer_name=? WHERE "
                "answerer_open_id=? AND (answerer_name='' OR "
                "answerer_name IS NULL)", (nm, oid))
            updated_nuggets += cur.rowcount
            db.execute(
                "UPDATE chat_nuggets SET asker_name=? WHERE "
                "asker_open_id=? AND (asker_name='' OR "
                "asker_name IS NULL)", (nm, oid))
        # contributors from source messages, where missing
        rows = [dict(r) for r in db.execute(
            "SELECT id, source_msg_ids FROM chat_nuggets "
            "WHERE contributors IN ('', '[]')")]
        mdb = _msgs_connect()
        try:
            for r in rows:
                try:
                    ids = json.loads(r["source_msg_ids"] or "[]")
                except Exception:
                    continue
                if not ids:
                    continue
                q = ",".join("?" * len(ids))
                names: List[str] = []
                for (nm2, auth) in mdb.execute(
                        f"SELECT sender_name, sender_authority FROM "
                        f"chat_messages WHERE msg_id IN ({q}) "
                        f"ORDER BY created_at_ms", ids):
                    nm2 = (nm2 or "").strip()
                    if auth != "bot" and nm2 and nm2 not in names:
                        names.append(nm2)
                if names:
                    db.execute("UPDATE chat_nuggets SET contributors=? "
                               "WHERE id=?", (json.dumps(names), r["id"]))
                    contribs += 1
        finally:
            mdb.close()
        db.commit()
    finally:
        db.close()
    if verbose:
        print(f"[chat_nuggets] backfill — resolved {len(name_map)} "
              f"identities; {updated_msgs} message rows, "
              f"{updated_nuggets} nugget answerers, "
              f"{updated_feedback} feedback rows, "
              f"{contribs} contributor lists")
    return {"identities": len(name_map), "messages": updated_msgs,
            "nugget_answerers": updated_nuggets,
            "feedback_rows": updated_feedback,
            "contributor_lists": contribs}


def contextualize(nugget_id: int, timeout: int = 120,
                  verbose: bool = False) -> Dict[str, Any]:
    """Cross-check one nugget against the document corpus and store a
    context_note situating the chat answer — corroborations,
    contradictions, applicability limits. Best-effort: any failure
    leaves context_note empty and the nugget still embeds (with the
    point-in-time framing). The note is what keeps an approved nugget
    from reading as a standalone verdict."""
    r = get(nugget_id)
    if not r:
        return {"ok": False, "error": f"nugget #{nugget_id} not found"}
    excerpts = ""
    try:
        from doc_index import search_brief
        excerpts = search_brief(
            f"{r.get('topic') or ''} {r['question']}"[:200], k=6) or ""
    except Exception as e:
        if verbose:
            print(f"[chat_nuggets] contextualize retrieval failed: {e}")
    observed = (r.get("created_at") or "")[:7] or "unknown date"
    try:
        from noto_research import _claude
        prompt = (
            "You are Noto, your organization's knowledge "
            "assistant. A Q&A pair extracted from a team group chat is "
            "being added to your knowledge corpus. Chat answers are "
            "point-in-time takes — often right, rarely the whole "
            "picture, and sometimes tied to a specific project or "
            "deadline that will expire.\n\n"
            f"Q: {r['question']}\nA: {r['answer']}\n"
            f"(answered by {r.get('answerer_name') or '?'}, "
            f"authority: {r.get('authority')}, "
            f"chat: {r.get('chat_name') or '?'}, observed {observed})\n\n"
            "CORPUS EXCERPTS ON THE SAME TOPIC:\n"
            f"{excerpts or '(no relevant corpus excerpts found)'}\n\n"
            "Reply with ONLY a JSON object:\n"
            '{"context": "<2-4 sentences: where the corpus corroborates, '
            "contradicts, or bounds this answer; if the corpus is "
            "silent, say so — do NOT restate the answer>\",\n"
            ' "durability": "<durable | mixed | ephemeral — ephemeral = '
            "tied to a specific project/deadline/timeline that will "
            "expire; mixed = a durable lesson wrapped in dated "
            'specifics; durable = holds over time>",\n'
            ' "durable_reframe": "<for mixed/ephemeral: 1-2 sentences '
            "stating ONLY the part that outlives the moment — the "
            "recurring process/team dynamic, the how-to. Empty string "
            'if durable or nothing generalizes>"}')
        raw = (_claude(prompt, timeout=timeout) or "").strip()
    except Exception as e:
        return {"ok": False, "error": f"contextualize LLM failed: {e}"}
    if not raw:
        return {"ok": False, "error": "empty context from LLM"}
    note, durability, reframe = raw, "", ""
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            note = str(data.get("context", "")).strip() or raw
            durability = str(data.get("durability", "")).strip().lower()
            if durability not in ("durable", "mixed", "ephemeral"):
                durability = ""
            reframe = str(data.get("durable_reframe", "")).strip()
        except Exception:
            pass    # keep raw text as the context note
    db = _connect()
    try:
        db.execute("UPDATE chat_nuggets SET context_note=?, durability=?, "
                   "durable_reframe=?, embedded_at=NULL WHERE id=?",
                   (note[:2000], durability, reframe[:1000], nugget_id))
        db.commit()
    finally:
        db.close()
    if verbose:
        print(f"[chat_nuggets] contextualized #{nugget_id} "
              f"[{durability or '?'}]: {note[:70]}…")
    return {"ok": True, "id": nugget_id, "context_note": note,
            "durability": durability, "durable_reframe": reframe}


def contextualize_pending(verbose: bool = False,
                          limit: int = 10) -> Dict[str, int]:
    """Pre-check PENDING nuggets against the corpus so the durability
    verdict + context are already on the row when the reviewer opens
    the queue. Small batches per sweep — the panel's worker re-fires
    while any remain."""
    db = _connect()
    try:
        ids = [r[0] for r in db.execute(
            "SELECT id FROM chat_nuggets WHERE status='pending' "
            "AND context_note='' ORDER BY created_at DESC LIMIT ?",
            (limit,))]
    finally:
        db.close()
    done = failed = 0
    for nid in ids:
        res = contextualize(nid, verbose=verbose)
        if res.get("ok"):
            done += 1
        else:
            failed += 1
    return {"prechecked": done, "precheck_failed": failed}


def contextualize_unembedded(verbose: bool = False,
                             limit: int = 20) -> Dict[str, int]:
    """Contextualize every active nugget awaiting embedding that has no
    context_note yet. Called by the admin panel's embed worker before
    embed_active(), so approval = contextualize → embed."""
    db = _connect()
    try:
        ids = [r[0] for r in db.execute(
            "SELECT id FROM chat_nuggets WHERE status='active' "
            "AND embedded_at IS NULL AND context_note='' "
            "ORDER BY id LIMIT ?", (limit,))]
    finally:
        db.close()
    done = failed = 0
    for nid in ids:
        res = contextualize(nid, verbose=verbose)
        if res.get("ok"):
            done += 1
        else:
            failed += 1
    return {"contextualized": done, "failed": failed}


def embed_active(verbose: bool = True) -> Dict[str, int]:
    """Embed every status='active' nugget that hasn't been embedded yet.
    Uses the same embeddings module the rest of the bot uses; nuggets
    land as source_kind='nugget' so existing semantic search surfaces
    them naturally."""
    from embeddings import index_document
    db = _connect()
    try:
        rows = [dict(r) for r in db.execute(
            "SELECT id, question, answer, topic, chat_name, authority, "
            "answerer_name, reviewed_note, context_note, durability, "
            "durable_reframe, contributors, created_at FROM chat_nuggets "
            "WHERE status='active' AND embedded_at IS NULL"
        ).fetchall()]
    finally:
        db.close()
    embedded = 0
    for r in rows:
        # Framed as a point-in-time chat observation, NOT a standalone
        # answer — retrieval feeds synthesis, and synthesis must weigh
        # this WITH the document corpus (operator direction 2026-07-02:
        # "we can't approve a nugget as the word of God"). Always
        # date-anchored; time-sensitive answers lead with the durable
        # lesson and mark the specifics as dated (projects and deadlines
        # expire).
        observed = (r.get("created_at") or "")[:7] or "unknown date"
        durability = (r.get("durability") or "").strip()
        reframe = (r.get("durable_reframe") or "").strip()
        parts = [
            f"Team chat Q&A (point-in-time observation from group chat, "
            f"observed {observed} — one input to weigh with the document "
            f"corpus, not standalone policy):"]
        if reframe and durability in ("mixed", "ephemeral"):
            parts.append(f"Durable lesson: {reframe}")
        parts.append(f"Q: {r['question']}")
        a_prefix = (f"(time-sensitive as of {observed} — verify before "
                    f"relying) " if durability == "ephemeral" else "")
        parts.append(f"A: {a_prefix}{r['answer']}")
        try:
            contribs = json.loads(r.get("contributors") or "[]")
        except Exception:
            contribs = []
        answerer = r.get("answerer_name") or (contribs[0] if contribs
                                              else "?")
        others = [c for c in contribs if c != answerer]
        from_line = (f"topic: {r.get('topic') or '?'}  "
                     f"from: {answerer} ({r.get('authority')})")
        if others:
            from_line += f", in discussion with {', '.join(others)}"
        from_line += f" in {r.get('chat_name') or '?'}"
        parts.append(from_line)
        note = (r.get("reviewed_note") or "").strip()
        if note and note != "approved":
            parts.append(f"Operator note at approval: {note}")
        ctx = (r.get("context_note") or "").strip()
        if ctx:
            parts.append(f"Corpus context: {ctx}")
        blob = "\n".join(parts)
        try:
            index_document(source_kind="nugget",
                           source_id=f"nugget:{r['id']}",
                           text=blob,
                           heading=(r.get("topic") or
                                     r.get("question") or "")[:80])
            db = _connect()
            try:
                db.execute(
                    "UPDATE chat_nuggets SET embedded_at=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     r["id"]))
                db.commit()
            finally:
                db.close()
            embedded += 1
            if verbose:
                print(f"  ✓ embedded nugget #{r['id']}", flush=True)
        except Exception as e:
            if verbose:
                print(f"  ✗ embed #{r['id']}: {str(e)[:60]}",
                      flush=True)
    if verbose:
        print(f"[chat_nuggets] embed_active done — embedded={embedded}",
              flush=True)
    return {"embedded": embedded}


# ---------------------------------------------------------------------------
# Stats + listing
# ---------------------------------------------------------------------------

def stats() -> Dict[str, Any]:
    db = _connect()
    try:
        by_status = dict(db.execute(
            "SELECT status, COUNT(*) FROM chat_nuggets GROUP BY status"
        ).fetchall())
        by_authority = dict(db.execute(
            "SELECT authority, COUNT(*) FROM chat_nuggets GROUP BY authority"
        ).fetchall())
        by_chat = dict(db.execute(
            "SELECT chat_name, COUNT(*) FROM chat_nuggets GROUP BY chat_name"
        ).fetchall())
        total = sum(by_status.values())
        embedded = db.execute(
            "SELECT COUNT(*) FROM chat_nuggets WHERE embedded_at IS NOT NULL"
        ).fetchone()[0]
    finally:
        db.close()
    return {"total": total, "embedded": embedded,
            "by_status": by_status, "by_authority": by_authority,
            "by_chat": by_chat}


def list_pending(limit: int = 30,
                 chat_id: Optional[str] = None) -> List[Dict[str, Any]]:
    db = _connect()
    try:
        if chat_id:
            rows = db.execute(
                "SELECT * FROM chat_nuggets WHERE status='pending' "
                "AND chat_id=? ORDER BY created_at DESC LIMIT ?",
                (chat_id, limit)).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM chat_nuggets WHERE status='pending' "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def approve(nugget_id: int, reviewer_open_id: str = "",
            reviewer_name: str = "",
            edited_question: str = "",
            edited_answer: str = "",
            embed: bool = True,
            reviewer_note: str = "") -> Dict[str, Any]:
    """embed=False lets a caller (the admin panel) flip status now and
    run ONE embed_active() sweep later — batch approvals shouldn't pay
    the embedding round-trip per nugget. Default True preserves the
    original behavior for every existing caller.

    reviewer_note: the operator's caveat ("yes, but also consider …") —
    stored and carried into the embedded blob + answer-time rendering."""
    db = _connect()
    try:
        r = db.execute("SELECT * FROM chat_nuggets WHERE id=?",
                        (nugget_id,)).fetchone()
        if not r:
            return {"ok": False, "error": f"nugget #{nugget_id} not found"}
        q = (edited_question or r["question"]).strip()
        a = (edited_answer or r["answer"]).strip()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "UPDATE chat_nuggets SET status='active', question=?, "
            "answer=?, reviewed_at=?, reviewed_by=?, "
            "reviewed_note=?, embedded_at=NULL WHERE id=?",
            (q, a, now, reviewer_name or reviewer_open_id,
             (reviewer_note or "").strip() or "approved", nugget_id))
        db.commit()
    finally:
        db.close()
    # embed immediately so it's live in retrieval
    if embed:
        embed_active(verbose=False)
    return {"ok": True, "id": nugget_id}


def dismiss(nugget_id: int, reviewer_open_id: str = "",
            reviewer_name: str = "", reason: str = "") -> Dict[str, Any]:
    db = _connect()
    try:
        r = db.execute("SELECT 1 FROM chat_nuggets WHERE id=?",
                        (nugget_id,)).fetchone()
        if not r:
            return {"ok": False, "error": f"nugget #{nugget_id} not found"}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "UPDATE chat_nuggets SET status='superseded', "
            "reviewed_at=?, reviewed_by=?, reviewed_note=? "
            "WHERE id=?",
            (now, reviewer_name or reviewer_open_id,
             reason or "dismissed", nugget_id))
        db.commit()
    finally:
        db.close()
    # remove from vector index (we keep embedded_at NULL → next embed
    # cycle won't reindex; existing chunk gets swept by orphan_sweep)
    return {"ok": True, "id": nugget_id}


def get(nugget_id: int) -> Optional[Dict[str, Any]]:
    db = _connect()
    try:
        r = db.execute("SELECT * FROM chat_nuggets WHERE id=?",
                        (nugget_id,)).fetchone()
        return dict(r) if r else None
    finally:
        db.close()


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 0
    cmd = argv[0]
    if cmd == "extract-pending":
        lim = None
        for i, a in enumerate(argv):
            if a == "--limit" and i + 1 < len(argv):
                lim = int(argv[i + 1])
        extract_pending(limit=lim, verbose=True)
        return 0
    if cmd == "backfill-names":
        print(json.dumps(backfill_names(verbose=True), indent=2))
        return 0
    if cmd == "extract-chat" and len(argv) >= 2:
        extract_chat(argv[1], verbose=True)
        return 0
    if cmd == "embed-active":
        embed_active(verbose=True)
        return 0
    if cmd == "stats":
        print(json.dumps(stats(), indent=2))
        return 0
    if cmd == "list":
        status = "pending"
        for i, a in enumerate(argv):
            if a == "--status" and i + 1 < len(argv):
                status = argv[i + 1]
        db = _connect()
        try:
            rows = db.execute(
                "SELECT id, status, authority, answerer_name, topic, "
                "question FROM chat_nuggets WHERE status=? "
                "ORDER BY created_at DESC LIMIT 30",
                (status,)).fetchall()
        finally:
            db.close()
        for r in rows:
            print(f"#{r['id']} [{r['status']}/{r['authority']}] "
                  f"by={r['answerer_name'] or '?':16} "
                  f"topic={r['topic'] or '?':15} "
                  f"{(r['question'] or '')[:70]}")
        return 0
    if cmd == "approve" and len(argv) >= 2:
        print(json.dumps(approve(int(argv[1])), indent=2))
        return 0
    if cmd == "dismiss" and len(argv) >= 2:
        print(json.dumps(dismiss(int(argv[1])), indent=2))
        return 0
    print("commands: extract-pending [--limit N] | extract-chat <chat_id> "
          "| embed-active | stats | list [--status pending|active|...] | "
          "approve <id> | dismiss <id>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
