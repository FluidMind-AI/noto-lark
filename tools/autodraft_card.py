#!/usr/bin/env python3
"""
Auto-draft review cards — inbox triage from the Noto DM.

For every auto-draft, the user gets a DM card: subject on top, the
received email, Noto's draft, and three actions:
  ✅ Send    — send the draft as-is (their own mail send scope)
  ✗ Discard  — delete the draft (mail-DRAFT delete is the one
               operator-approved exception to the no-delete rule,
               2026-07-22 — drafts are Noto's own pre-send output)
  ✏️ Edit    — open Lark Mail to edit the draft by hand (URL button);
               the card stays open, they send from Mail themselves.

Queue: indexes/mail/draft_queue.db. Owner-gated: only the mailbox owner
(the DM recipient) can act; the card handler verifies the clicker.
Mirrors pipeline_card.py's CardKit 2.0 mechanics (buttons as top-level
column_set elements; card_id↔message_id mapping for updates).
"""

import base64
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_home                                   # noqa: E402

_DB = os.path.join(get_home(), "indexes", "mail", "draft_queue.db")
# AppLink → opens the Mail tab INSIDE the Lark app (a plain web URL
# kicks the user out to the browser inbox). No per-draft deep link is
# documented; the draft sits at the top of Drafts.
_TENANT_MAIL_URL = "https://applink.larksuite.com/client/mail/home"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  user           TEXT NOT NULL,
  owner_open_id  TEXT NOT NULL,
  identity       TEXT NOT NULL,        -- OAuth identity for send/delete
  lark_draft_id  TEXT,
  msg_id         TEXT NOT NULL,
  subject        TEXT,
  from_name      TEXT,
  from_email     TEXT,
  inbound_snip   TEXT,
  draft_body     TEXT,
  raw_eml        TEXT NOT NULL,        -- exactly what Send transmits
  status         TEXT NOT NULL DEFAULT 'pending',
  card_msg_id    TEXT,
  card_id        TEXT,
  created_at     TEXT NOT NULL,
  resolved_at    TEXT,
  resolved_by    TEXT
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB), exist_ok=True)
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.executescript(_SCHEMA)
    return conn


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Enqueue + card
# ---------------------------------------------------------------------------

def enqueue_and_notify(user: str, owner_open_id: str, identity: str,
                       lark_draft_id: str, m: Dict[str, Any],
                       draft_body: str, raw_eml: str,
                       note: str = "", confidence: int = -1,
                       missing: str = "") -> Optional[int]:
    conn = _connect()
    try:
        have = {c[1] for c in conn.execute("PRAGMA table_info(queue)")}
        for col, typ in (("note", "TEXT"), ("confidence", "INTEGER"),
                         ("missing", "TEXT")):
            if col not in have:
                conn.execute(f"ALTER TABLE queue ADD COLUMN {col} {typ}")
        cur = conn.execute(
            "INSERT INTO queue (user, owner_open_id, identity,"
            " lark_draft_id, msg_id, subject, from_name, from_email,"
            " inbound_snip, draft_body, raw_eml, created_at, note,"
            " confidence, missing)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user, owner_open_id, identity, lark_draft_id,
             m.get("msg_id") or "", m.get("subject") or "",
             m.get("from_name") or "", m.get("from_email") or "",
             (m.get("body_plain") or "")[:700], draft_body[:2800],
             raw_eml, _now(), note[:500], confidence, missing[:300]))
        qid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    try:
        from lark_client import LarkClient
        client = LarkClient()
        card_id = client.create_card(_open_card(qid, _row(qid)))
        msg_id = client.send_card(owner_open_id, card_id,
                                  receive_id_type="open_id")
        conn = _connect()
        conn.execute("UPDATE queue SET card_msg_id=?, card_id=?"
                     " WHERE id=?", (msg_id, card_id, qid))
        conn.commit()
        conn.close()
        return qid
    except Exception as e:
        print(f"[autodraft_card] card send failed (q{qid}): {e}",
              file=sys.stderr, flush=True)
        return qid


def qid_for_card_msg(message_id: str) -> Optional[int]:
    """Map a Lark message_id (the card the user replied to) back to its
    queue row. Any status — the handlers give the right 'already sent/
    discarded' answer themselves."""
    if not message_id:
        return None
    conn = _connect()
    try:
        r = conn.execute("SELECT id FROM queue WHERE card_msg_id=?"
                         " ORDER BY id DESC LIMIT 1",
                         (message_id,)).fetchone()
        return int(r[0]) if r else None
    finally:
        conn.close()


def _row(qid: int) -> Dict[str, Any]:
    conn = _connect()
    try:
        r = conn.execute("SELECT * FROM queue WHERE id=?", (qid,)).fetchone()
        return dict(r) if r else {}
    finally:
        conn.close()


def _open_card(qid: int, r: Dict[str, Any]) -> Dict[str, Any]:
    subject = r.get("subject") or "(no subject)"
    sender = r.get("from_name") or r.get("from_email") or "?"
    btn = lambda label, style, act: {          # noqa: E731
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": style,
        "behaviors": [{"type": "callback",
                       "value": {"action": act, "qid": qid}}],
    }
    edit_btn = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "✏️ Edit"},
        "type": "default",
        "behaviors": [{"type": "open_url",
                       "default_url": _TENANT_MAIL_URL}],
    }
    col = lambda el: {"tag": "column", "width": "weighted",   # noqa: E731
                      "weight": 1, "elements": [el]}
    # Confidence → header color + triage line. Green = probably fine to
    # Send without reading; red = needs your eyes.
    conf = r.get("confidence")
    conf = conf if isinstance(conf, int) else -1
    if conf >= 85:
        template, band = "green", f"🟢 Confidence {conf}/100 — routine; safe to Send"
    elif conf >= 60:
        template, band = "orange", f"🟡 Confidence {conf}/100 — worth a skim before Send"
    elif conf >= 0:
        template, band = "red", f"🔴 Confidence {conf}/100 — review carefully"
    else:
        template, band = "indigo", ""
    if conf >= 0 and r.get("missing"):
        band += f"\n**Missing / uncertain:** {r['missing']}"
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {"template": template,
                   "title": {"tag": "plain_text",
                              "content": f"📩 {subject[:120]}"}},
        "body": {"elements": [
            *([{"tag": "markdown", "content": f"**{band}**"},
               {"tag": "hr"}] if band else []),
            {"tag": "markdown",
             "content": f"**From:** {sender} <{r.get('from_email','')}>"},
            {"tag": "hr"},
            {"tag": "markdown",
             "content": ("**Received:**\n"
                         + (r.get("inbound_snip") or "(empty)"))},
            {"tag": "hr"},
            {"tag": "markdown",
             "content": "**Noto's draft (reply-all, in thread):**\n"
                        + (r.get("draft_body") or "")},
            *([{"tag": "hr"},
               {"tag": "markdown",
                "content": f"💡 **Noto's note (not in the email):** "
                           f"{r['note']}"}] if r.get("note") else []),
            {"tag": "hr"},
            {"tag": "column_set", "horizontal_spacing": "default",
             "columns": [
                 col(btn("✅ Send", "primary_filled", "autodraft_send")),
                 col(btn("✗ Discard", "danger_filled", "autodraft_discard")),
                 col(edit_btn),
             ]},
            {"tag": "markdown",
             "content": ("_Send transmits the draft as-is; Edit opens "
                         "Lark Mail. Or just ↩️ REPLY to this card: "
                         "describe any change for a new version, or say "
                         f"“send” / “discard”._ _q#{qid}_")},
        ]},
    }


def mark_superseded(qid: int) -> None:
    """A redo replaced this draft: mark the row + flip its card grey."""
    conn = _connect()
    conn.execute("UPDATE queue SET status='superseded', resolved_at=?"
                 " WHERE id=?", (_now(), qid))
    conn.commit()
    conn.close()
    try:
        from lark_client import LarkClient
        r = _row(qid)
        if r.get("card_id"):
            LarkClient().update_card(r["card_id"], _resolved_card(r),
                                     sequence=2)
    except Exception as e:
        print(f"[autodraft_card] supersede card update failed q{qid}: {e}",
              file=sys.stderr, flush=True)


def _resolved_card(r: Dict[str, Any]) -> Dict[str, Any]:
    status = r.get("status")
    head = {"sent": ("✅ Sent", "green"),
            "discarded": ("🗑 Discarded — draft deleted", "red"),
            "superseded": ("🔄 Superseded by a redo — see the new card",
                           "grey"),
            }.get(status, (f"Resolved: {status}", "grey"))
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {"template": head[1],
                   "title": {"tag": "plain_text",
                              "content": f"📩 {(r.get('subject') or '')[:110]}"}},
        "body": {"elements": [
            {"tag": "markdown",
             "content": f"**{head[0]}** · {r.get('resolved_at','')}"},
            {"tag": "hr"},
            {"tag": "markdown",
             "content": ("**From:** "
                         f"{r.get('from_name') or r.get('from_email','')}"
                         f"\n\n{(r.get('draft_body') or '')[:1200]}")},
        ]},
    }


# ---------------------------------------------------------------------------
# Actions (called by the bot's card.action.trigger worker)
# ---------------------------------------------------------------------------

def _mail_api(identity: str, method: str, path: str,
              body: Optional[dict] = None) -> Dict[str, Any]:
    from lark_oauth import get_user_token
    req = urllib.request.Request(
        "https://open.larksuite.com" + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {get_user_token(identity)}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"code": e.code, "msg": f"http {e.code}"}


def _delete_draft(identity: str, draft_id: str) -> bool:
    """Mail-DRAFT deletion — the single operator-approved exception to
    the no-delete rule (2026-07-22): drafts are Noto's own pre-send
    output, reviewed and dismissed by their owner. Real messages, docs,
    files, folders, wiki nodes and Bitable records remain absolutely
    protected."""
    enc = urllib.parse.quote(str(draft_id), safe="")
    r = _mail_api(identity, "DELETE",  # safety-scan-ok: operator-approved mail-DRAFT exception (2026-07-22)
                  f"/open-apis/mail/v1/user_mailboxes/me/drafts/{enc}")
    return r.get("code") == 0


def _compose_send_payload(r: Dict[str, Any]) -> Dict[str, Any]:
    """Structured reply payload built at send time from the queue row:
    reply text → signature → quoted original, as body_html +
    body_plain_text (+ inline logo attachment when configured)."""
    import html as _html
    from email_autodraft import (_fetch_reply_headers, _signature,
                                 _quoted_history)
    user = r["user"]
    try:
        h = _fetch_reply_headers(user, r["msg_id"])
    except Exception:
        h = {"from": r.get("from_email") or "", "from_name": "",
             "to": [], "cc": [], "body_html": "", "body_plain": ""}
    from email_autodraft import USERS
    mailbox = USERS[user][0]
    sender = h.get("from") or r.get("from_email") or ""
    cc, seen = [], {mailbox.lower(), sender.lower()}
    for a in (h.get("to") or []) + (h.get("cc") or []):
        if a and a.lower() not in seen:
            cc.append(a)
            seen.add(a.lower())
    sig = _signature(user)
    body_text = r.get("draft_body") or ""

    def para(t):
        return "".join(f"<div>{_html.escape(ln) or '<br>'}</div>"
                       for ln in t.splitlines())
    m_like = {"date_ms": 0, "from_name": h.get("from_name") or
              r.get("from_name") or "", "from_email": sender,
              "body_plain": r.get("inbound_snip") or ""}
    orig_html = h.get("body_html") or para(h.get("body_plain") or "")
    html_body = (para(body_text) + "<br>" + (sig.get("html") or
                 para(sig.get("plain") or "")) + "<br>"
                 "<div>On earlier message, "
                 f"{_html.escape(h.get('from_name') or sender)} wrote:"
                 "<blockquote style='margin:0 0 0 .8ex;border-left:1px "
                 f"#ccc solid;padding-left:1ex'>{orig_html}</blockquote>"
                 "</div>")
    plain_body = (body_text + "\n\n" + (sig.get("plain") or "") +
                  "\n\n" + _quoted_history(m_like))
    subj = r.get("subject") or ""
    payload = {
        "to": [{"mail_address": sender}],
        "subject": subj if subj.lower().startswith("re:") else f"Re: {subj}",
        "body_html": html_body,
        "body_plain_text": plain_body,
    }
    if cc:
        payload["cc"] = [{"mail_address": a} for a in cc]
    # inline signature logo (best effort; caller retries without it)
    imgs = sig.get("inline_images") or {}
    atts = []
    for cid, rel in imgs.items():
        try:
            path = rel if os.path.isabs(rel) else os.path.join(
                get_home(), rel)
            with open(path, "rb") as f:
                atts.append({"body": base64.urlsafe_b64encode(
                                 f.read()).decode(),
                             "filename": os.path.basename(path),
                             "is_inline": True, "cid": cid})
        except Exception:
            pass
    if atts:
        payload["attachments"] = atts
    return payload


def handle_click(qid: int, action: str, clicker_open_id: str,
                 clicker_name: str = "") -> str:
    """Send/Discard from the card. Returns a short outcome string (also
    used for logging). Owner-gated: only the mailbox owner may act."""
    r = _row(qid)
    if not r:
        return "unknown queue item"
    if clicker_open_id != r["owner_open_id"]:
        return "not the mailbox owner — ignored"
    if r["status"] != "pending":
        return f"already {r['status']}"

    def _notify(text: str) -> None:
        # a failed click must NEVER be silent (operator, 2026-07-22)
        try:
            from lark_client import LarkClient
            LarkClient().send_text(r["owner_open_id"], text,
                                   receive_id_type="open_id")
        except Exception:
            pass

    if action == "autodraft_send":
        # SEND-TIME GUARD (suspenders to the draft-time strip): if any
        # meta-commentary shape survives in the body, refuse to send.
        try:
            from email_autodraft import META_RE
            if META_RE.search(r.get("draft_body") or ""):
                _notify(f"⛔ Not sent — that draft (“{(r.get('subject') or '')[:50]}”) "
                        "still contains editor notes meant for you, so I "
                        "refused to transmit it. Use Edit in Lark Mail, or "
                        "Discard it (it predates the notes fix).")
                return "BLOCKED: draft still contains editor notes — use Edit"
        except Exception:
            pass
        # Send via the NATIVE REPLY endpoint with STRUCTURED FIELDS.
        # Hard-won findings (2026-07-22/23): the reply endpoint threads
        # into the SAME conversation (plain /send forks a new thread) —
        # but it silently DISCARDS raw EML bodies (any MIME shape; a
        # blank email reached a real recipient before we caught it).
        # Structured body_html/body_plain_text fields deliver intact.
        payload = _compose_send_payload(r)
        enc = urllib.parse.quote(str(r["msg_id"]), safe="")
        resp = _mail_api(r["identity"], "POST",
                         f"/open-apis/mail/v1/user_mailboxes/me/messages/"
                         f"{enc}/reply", payload)
        if resp.get("code") != 0 and payload.get("attachments"):
            # attachment encoding rejected? retry without the logo
            p2 = {k: v for k, v in payload.items() if k != "attachments"}
            resp = _mail_api(r["identity"], "POST",
                             f"/open-apis/mail/v1/user_mailboxes/me/"
                             f"messages/{enc}/reply", p2)
        if resp.get("code") != 0:
            print(f"[autodraft_card] native reply failed q{qid} "
                  f"(code {resp.get('code')}) — falling back to plain send",
                  file=sys.stderr, flush=True)
            resp = _mail_api(r["identity"], "POST",
                             "/open-apis/mail/v1/user_mailboxes/me/messages/send",
                             {"raw": r["raw_eml"]})
        if resp.get("code") != 0:
            print(f"[autodraft_card] send failed q{qid}: "
                  f"{resp.get('code')} {str(resp.get('msg'))[:120]}",
                  file=sys.stderr, flush=True)
            if resp.get("code") == 99991679:
                _notify("⚠️ Send failed — Noto doesn't have send permission "
                        "for your mailbox yet. The send scope must be added "
                        "in the Developer Console and you need one fresh "
                        "authorization click (Alejandro has the link). The "
                        "draft is untouched in your Drafts.")
            else:
                _notify(f"⚠️ Send failed (error {resp.get('code')}). The "
                        "draft is untouched in your Drafts — you can send "
                        "it from Lark Mail.")
            return f"send failed ({resp.get('code')})"
        # tidy up: remove the now-redundant draft (approved exception)
        if r.get("lark_draft_id"):
            _delete_draft(r["identity"], r["lark_draft_id"])
        new_status = "sent"
    elif action == "autodraft_discard":
        if r.get("lark_draft_id"):
            _delete_draft(r["identity"], r["lark_draft_id"])
        new_status = "discarded"
    else:
        return f"unknown action {action!r}"

    conn = _connect()
    conn.execute("UPDATE queue SET status=?, resolved_at=?, resolved_by=?"
                 " WHERE id=?", (new_status, _now(), clicker_name, qid))
    conn.commit()
    conn.close()
    # flip the card to its resolved state
    try:
        from lark_client import LarkClient
        r2 = _row(qid)
        if r2.get("card_id"):
            # single-update lifecycle: create=seq1, resolve=seq2
            LarkClient().update_card(r2["card_id"], _resolved_card(r2),
                                     sequence=2)
    except Exception as e:
        print(f"[autodraft_card] card update failed q{qid}: {e}",
              file=sys.stderr, flush=True)
    return new_status


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "stats":
        conn = _connect()
        print(dict(conn.execute(
            "SELECT status, COUNT(*) FROM queue GROUP BY status")))
        conn.close()
    else:
        print(__doc__)
