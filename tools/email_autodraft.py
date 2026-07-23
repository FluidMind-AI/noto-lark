#!/usr/bin/env python3
"""
F4-4b — the To-only auto-draft engine.

For each pilot user: poll their INBOX mirror for new messages where the
user is a DIRECT `To:` recipient (never CC, never lists/no-reply, never
their own sends). An LLM triage decides if the email actually needs a
reply; if so, Noto drafts one — HOUSE PLAYBOOK first (how VP answers
this situation), the user's own voice second (their similar past
replies as style exemplars) — saves it to the user's real Lark Mail
Drafts via THEIR OWN consented token, and DMs them a heads-up.

Nothing is ever sent — Noto writes drafts; the human reviews and sends.
Drafting requires the per-user `<user>_mail` OAuth token; users without
one (no consent yet) are skipped for drafting.

State: indexes/mail/autodraft_state.json  {user: last_processed date_ms}
CLI:  python tools/email_autodraft.py run [--user U] [--limit N] [--dry]
      python tools/email_autodraft.py status
"""

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_home                                   # noqa: E402
import mail_store                                              # noqa: E402
import email_playbook                                          # noqa: E402

# Users come from config — lolabot.yaml:
#   mail:
#     users:
#       <slug>: {mailbox: user@yourco.com, open_id: ou_...}
# Draft OAuth identity is always "<slug>_mail" (see lark_oauth).
def _users():
    from config import load_config
    users = (load_config().get("mail", {}) or {}).get("users", {}) or {}
    return {slug: ((u or {}).get("mailbox", ""), f"{slug}_mail",
                   (u or {}).get("open_id", ""))
            for slug, u in users.items() if (u or {}).get("mailbox")}


class _Users(dict):
    def __missing__(self, k):
        self.update(_users())
        return dict.__getitem__(self, k)
    def __contains__(self, k):
        if not dict.__contains__(self, k):
            self.update(_users())
        return dict.__contains__(self, k)
    def __iter__(self):
        self.update(_users())
        return dict.__iter__(self)
    def keys(self):
        self.update(_users())
        return dict.keys(self)


USERS = _Users()

STATE = os.path.join(get_home(), "indexes", "mail", "autodraft_state.json")
_MAX_PER_RUN = 6          # per user per poll — safety valve
def _internal_domain():
    from config import load_config
    return (load_config().get("mail", {}) or {}).get("internal_domain", "")


def _state() -> Dict[str, int]:
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def _save_state(st: Dict[str, int]) -> None:
    json.dump(st, open(STATE, "w"))


# ---------------------------------------------------------------------------
# Candidate selection — the To-only rule
# ---------------------------------------------------------------------------

def _candidates(user: str, since_ms: int, limit: int) -> List[Dict[str, Any]]:
    mailbox = USERS[user][0]
    conn = mail_store._connect(user)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT msg_id, thread_id, date_ms, from_email, from_name,"
            " to_json, cc_json, subject, body_plain FROM messages"
            " WHERE label='INBOX' AND date_ms > ? AND is_noreply = 0"
            " ORDER BY date_ms ASC", (since_ms,))]
        out = []
        for m in rows:
            to = json.loads(m.get("to_json") or "[]")
            if mailbox not in to:
                continue                       # CC/BCC/list → skip (the rule)
            if (m.get("from_email") or "") == mailbox:
                continue                       # own message
            out.append(m)
            if len(out) >= limit:
                break
        return out
    finally:
        conn.close()


def _thread_context(user: str, thread_id: str, before_ms: int,
                    cap: int = 3) -> str:
    conn = mail_store._connect(user)
    try:
        rows = conn.execute(
            "SELECT date_ms, from_email, body_plain FROM messages"
            " WHERE thread_id=? AND date_ms < ? ORDER BY date_ms DESC"
            " LIMIT ?", (thread_id, before_ms, cap)).fetchall()
        parts = []
        for r in reversed([dict(x) for x in rows]):
            parts.append(f"[{r['from_email']}]: "
                         f"{(r['body_plain'] or '')[:900]}")
        return "\n---\n".join(parts)
    finally:
        conn.close()


def _style_exemplars(user: str, query: str, k: int = 2) -> List[str]:
    """The user's own past replies most similar to this situation —
    their voice, applied at draft time (no separate style store)."""
    conn = mail_store._connect(user)
    try:
        terms = [t for t in re.findall(r"[A-Za-z]{3,}", query)][:10]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        try:
            rows = conn.execute(
                "SELECT m.body_plain FROM messages_fts f"
                " JOIN messages m ON m.msg_id=f.msg_id"
                " WHERE messages_fts MATCH ? AND m.label='SENT'"
                " ORDER BY rank LIMIT ?", (match, k)).fetchall()
        except Exception:
            return []
        return [(r[0] or "")[:1200] for r in rows if r[0]]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Draft generation — playbook first, personal style second
# ---------------------------------------------------------------------------

_RESEARCH_SPEC = """
"research": {{"mail_queries": ["<up to 2 searches over {name}'s OWN mailbox
   — past exchanges with this person, prior commitments, earlier threads
   on this topic>"],
  "entities": [{{"type": "candidate|firm", "name": "<exact name>"}} —
   up to 3 people/firms this email is really about],
  "corpus_queries": ["<up to 2 searches over the company knowledge base —
   firm intel, practice-area facts, process/policy>"]}}"""

_PROMPT = """You draft email replies for {name} at {company}. Below: an inbound email addressed directly to them, thread \
context, the HOUSE PLAYBOOK entries for this kind of situation, and {name}'s \
own past replies to similar situations.

First decide: does this email actually need a reply from {name}? \
(Questions, requests, negotiations, scheduling → yes. Pure FYI, newsletters, \
receipts, automated notices → no.)

Second decide: can you draft this WELL from the thread + playbook alone
("complete": true — simple scheduling, acknowledgments, follow-ups), or does
a good reply depend on knowledge you don't have here ("complete": false —
questions about a candidate's status, a firm, past commitments, anything
substantive)? When complete=false, fill "research" with what you need; Noto
will search {name}'s own mailbox, the candidate/firm records, and the company
knowledge base, then you draft again WITH that material. Prefer research over
a vague draft — the brain exists, use it.{research_spec}

Output STRICT JSON only:
{{"needs_reply": true/false,
  "reason": "<one line>",
  "complete": true/false,
  "draft": "<the reply body, plain text, 100% ready to send — or empty>",
  "note_to_user": "<optional, INFORMATIONAL ONLY: context about the draft
   for {name}, shown on the review card, NEVER part of the email. State
   facts ('the thread names no time, so the draft asks for it') — NEVER
   offer actions, promise a redraft, or invite {name} to reply to you;
   there is no redraft mechanism. If the draft isn't right, {name} uses
   Edit or Discard.>",
  "confidence": <0-100 — how safe this draft is to send WITHOUT review>,
  "missing": "<one line: what you didn't know / had to work around — or empty>"}}

Confidence calibration (be honest, err LOW):
- 90-100: routine, all facts present in-thread, strong playbook match —
  sendable blind (scheduling acks, standard follow-ups, thanks+next-step).
- 70-89: solid draft, but a name/date/commitment deserves a glance.
- 40-69: material gap — you lacked a fact, guessed intent, or the ask is
  unusual; {name} should read before sending.
- 0-39: sensitive, high-stakes, or you're unsure what they want —
  {name} should probably write this one (your draft is a starting point).
Anything involving compensation figures, offers, rejections, legal terms,
or conflict caps at 65.

Drafting rules, in priority order:
1. THE DRAFT MUST BE SENDABLE VERBATIM. It goes out EXACTLY as written
   if {name} clicks Send. NEVER put meta-commentary, editor notes,
   bracketed instructions, placeholders, or anything addressed to
   {name} inside the draft — a recipient must never see a word that
   wasn't meant for them. Anything you want to tell {name} goes in
   note_to_user ONLY.
2. THE PLAYBOOK IS CANON: follow the house approach for this situation — the
   moves, the ordering, what to commit vs. deflect, what never to say.
3. Layer {name}'s personal voice from their exemplars ONLY as seasoning
   (greeting style, sign-off, sentence rhythm). House approach beats
   personal habit on any conflict.
4. Never invent facts, dates, names, or commitments not present in the
   thread. If a needed fact is missing, write the natural email move —
   ask the SENDER for it in the reply — and flag alternatives in
   note_to_user (e.g. "if you already know the time, tell me and I'll
   redraft as a confirmation").
5. LENGTH AND REGISTER COME FROM THE EXEMPLARS, not from how much you
   know. Match the playbook exemplars and {name}'s own replies — VP
   emails are typically short and direct. Research exists to make the
   draft ACCURATE (answer what was asked, get facts right), never to
   make it longer: use only the findings the reply actually needs and
   leave the rest out (note_to_user if {name} should know it). Some
   situations genuinely warrant length — let the matching exemplars be
   the judge of that, not the volume of research.
6. Concise, professional, no exclamation marks.
7. END the draft with {name}'s closing: pick whichever of their usual
   sign-offs fits the email — {closings} — then "{first}" on its own
   line. NOTHING after that: the full signature block (title, phone,
   links) is appended automatically below the closing.

INBOUND EMAIL (from {from_email}, subject "{subject}"):
{body}

THREAD CONTEXT (earlier messages):
{thread}

HOUSE PLAYBOOK (canonical — follow these):
{playbook}

{name}'S OWN SIMILAR REPLIES (style only):
{style}"""


# Meta-commentary shapes that must NEVER ride in an email body: bracketed
# segments naming the user / Noto / editor verbs. Shared with the send-time
# guard in autodraft_card (belt AND suspenders — operator, 2026-07-22,
# after a bracketed operator note once landed in a draft body).
def _meta_re():
    names = "|".join(re.escape(u) for u in USERS.keys()) or "user"
    pattern = (r"\[[^\]\n]{0,160}?(?:" + names + "|noto|note to|"
               r"replace th|fill in|todo|tbd|if you (?:already )?know|"
               r"placeholder|editor)[^\]\n]{0,160}\]")
    return re.compile(pattern, re.I)


class _MetaRe:
    def search(self, t):
        return _meta_re().search(t)

    def finditer(self, t):
        return _meta_re().finditer(t)

    def sub(self, r, t):
        return _meta_re().sub(r, t)


META_RE = _MetaRe()


def strip_meta(draft: str) -> Tuple[str, str]:
    """Remove meta-commentary from a draft body. Returns (clean_body,
    extracted_notes) — the notes get surfaced on the review card."""
    notes = [mm.group(0).strip("[] ") for mm in META_RE.finditer(draft)]
    clean = META_RE.sub("", draft)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, "; ".join(notes)


def _gather_research(user: str, plan: Dict[str, Any]) -> Tuple[str, str]:
    """Execute the model's research plan against the real brain:
    the user's OWN mailbox (isolation preserved — never anyone else's),
    the candidate/firm entity records, and the company semantic corpus.
    Returns (research_block, consulted_summary)."""
    parts, consulted = [], []
    # 1. the user's own mailbox — past exchanges, commitments
    for q in (plan.get("mail_queries") or [])[:2]:
        try:
            import mail_retrieval
            threads = mail_retrieval.retrieve_threads(user, str(q), 2)
            if threads:
                parts.append(f"### {name_q(q)} — from {user}'s own mailbox:\n"
                             + mail_retrieval._render_threads(threads, 5000))
                consulted.append(f"mailbox:“{str(q)[:30]}”")
        except Exception as e:
            print(f"[autodraft] mail research failed: {e}",
                  file=sys.stderr, flush=True)
    # 2. candidate / firm entity records
    for ent in (plan.get("entities") or [])[:3]:
        try:
            import entity_store
            etype = "candidate" if "cand" in str(ent.get("type", "")).lower() \
                else "firm"
            rec = entity_store.get_entity(etype, str(ent.get("name") or ""))
            if rec:
                slim = json.dumps(rec)[:1400]
                parts.append(f"### {etype} record — {ent.get('name')}:\n{slim}")
                consulted.append(f"{etype}:{str(ent.get('name'))[:24]}")
        except Exception as e:
            print(f"[autodraft] entity research failed: {e}",
                  file=sys.stderr, flush=True)
    # 3. company knowledge corpus (semantic; min_sim floor per the
    #    2026-07 accuracy review — never inject nearest-but-irrelevant)
    for q in (plan.get("corpus_queries") or [])[:2]:
        try:
            from embeddings import search as corpus_search
            hits = corpus_search(str(q), k=4, min_sim=0.35)
            if hits:
                blob = "\n".join(f"- [{h.get('source_kind')}/"
                                 f"{str(h.get('heading') or '')[:40]}] "
                                 f"{(h.get('text') or '')[:500]}"
                                 for h in hits)
                parts.append(f"### company knowledge — {name_q(q)}:\n{blob}")
                consulted.append(f"corpus:“{str(q)[:30]}”")
        except Exception as e:
            print(f"[autodraft] corpus research failed: {e}",
                  file=sys.stderr, flush=True)
    return ("\n\n".join(parts) or "(research returned nothing useful)",
            ", ".join(consulted))


def name_q(q) -> str:
    return f"“{str(q)[:60]}”"


def _llm_json(prompt: str) -> Optional[Dict[str, Any]]:
    from noto_research import _claude
    raw = _claude(prompt, timeout=240, web=False) or ""
    j = re.search(r"\{.*\}", raw, re.S)
    if not j:
        return None
    try:
        return json.loads(j.group(0))
    except Exception:
        return None


def _generate(user: str, m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = user.capitalize()
    query = f"{m.get('subject') or ''} {(m.get('body_plain') or '')[:400]}"
    pb = email_playbook.search(query, k=3)
    pb_txt = "\n\n".join(
        f"[{e['situation_type']}] {e['situation']}\nAPPROACH: {e['approach']}"
        f"\nTONE: {e['tone']}\nEXEMPLAR:\n{(e['exemplar'] or '')[:900]}"
        for e in pb) or "(no matching playbook entries yet)"
    style = "\n\n---\n\n".join(_style_exemplars(user, query)) or "(none found)"
    from config import load_config as _lc
    company = (_lc().get("agent", {}) or {}).get("company_name", "the company")
    closings = " / ".join(f'"{c}"' for c in _signature(user)["closings"])
    base_kwargs = dict(
        name=name, first=name, company=company, closings=closings, from_email=m.get("from_email") or "?",
        subject=(m.get("subject") or "")[:150],
        body=(m.get("body_plain") or "")[:2400],
        thread=_thread_context(user, m.get("thread_id") or "",
                               m.get("date_ms") or 0) or "(none)",
        playbook=pb_txt, style=style)

    # PHASE A: triage + simple-draft-or-research-plan
    g = _llm_json(_PROMPT.format(research_spec=_RESEARCH_SPEC.format(name=name),
                                 **base_kwargs))
    if not g or not g.get("needs_reply"):
        return g
    if g.get("complete") and (g.get("draft") or "").strip():
        return g

    # PHASE B: gather from the real brain, then draft with it
    research, consulted = _gather_research(user, g.get("research") or {})
    prompt_b = _PROMPT.format(research_spec="", **base_kwargs) + (
        "\n\nRESEARCH FINDINGS (you asked for these — from "
        f"{name}'s own mailbox, the candidate/firm records, and the "
        "company knowledge base). Draft the reply NOW using them; set "
        "\"complete\": true. Ground every claim in the thread or these "
        "findings — anything still unknown goes to note_to_user/missing, "
        "never guessed. The findings are REFERENCE, not content to "
        "showcase: keep the reply at the length the exemplars set, "
        "answer only what the email asks, and let unused findings go to "
        "note_to_user if worth {name}'s attention:\n\n".replace(
            "{name}", name) + research)
    g2 = _llm_json(prompt_b)
    if not g2:
        return g          # fall back to phase-A result
    if consulted:
        g2["note_to_user"] = ((g2.get("note_to_user") or "").strip() +
                              f" (Consulted: {consulted})").strip()
    return g2


# ---------------------------------------------------------------------------
# Draft creation (user's own token) + DM heads-up
# ---------------------------------------------------------------------------

_SIG_CACHE = os.path.join(get_home(), "indexes", "mail", "signatures.json")

# ---------------------------------------------------------------------------
# The VP house signature template (two-column: logo left, details right). Every user gets the SAME layout with their own fields:
# two-column table, VP logo left (160px cell, 135px logo → ~25px gap),
# 18px bold name + WhatsApp link + phone lines + LinkedIn | site right.
# Attribute-based widths on purpose — Lark strips CSS but honors them.
# ---------------------------------------------------------------------------

def vp_signature(name: str, whatsapp_url: str = "",
                 phone_lines: Optional[List[str]] = None,
                 linkedin_url: str = "",
                 title: str = "",
                 ) -> Dict[str, Any]:
    """Render the house signature for a user. Returns the signatures.json
    record: {plain, html, inline_images}. `title` is the optional role
    line under the name (e.g. "Associate Legal Recruiter (GMT+8)")."""
    phones = phone_lines or []
    html_rows = [f"<div style='font-size:18px;font-weight:bold'>"
                 f"<b>{name}</b></div>"]
    plain = [name]
    if title:
        html_rows.append(f"<div>{title}</div>")
        plain.append(title)
    if whatsapp_url:
        html_rows.append(f"<div><a href='{whatsapp_url}'>"
                         f"Click Here to WhatsApp Me</a></div>")
        plain.append(f"Click Here to WhatsApp Me: {whatsapp_url}")
    for ln in phones:
        html_rows.append(f"<div>{ln}</div>")
        plain.append(ln)
    cfg = (__import__("config").load_config().get("mail", {}) or {}).get("signature", {}) or {}
    site_url = cfg.get("site_url", "")
    site_label = cfg.get("site_label", site_url)
    linkedin_url = linkedin_url or cfg.get("linkedin_url", "")
    tail_h, tail_p = [], []
    if linkedin_url:
        tail_h.append(f"<a href='{linkedin_url}'>LinkedIn</a>")
        tail_p.append(f"LinkedIn: {linkedin_url}")
    if site_url:
        tail_h.append(f"<a href='{site_url}'>{site_label}</a>")
        tail_p.append(site_label)
    if tail_h:
        html_rows.append("<div>" + " | ".join(tail_h) + "</div>")
        plain.append(" | ".join(tail_p))
    logo = cfg.get("logo_path", "")
    alt = cfg.get("company_name", "")
    if logo:
        html = ("<table width='500' cellpadding='0' cellspacing='0' border='0'>"
                "<tr><td width='160' valign='middle'>"
                f"<img src='cid:sig_logo' width='135' alt='{alt}'></td>"
                "<td valign='middle'>" + "".join(html_rows) + "</td></tr></table>")
        imgs = {"sig_logo": logo}
    else:
        html = "".join(html_rows)
        imgs = {}
    return {"plain": chr(10).join(plain), "html": html, "inline_images": imgs}


def pin_signature(user: str, name: str, whatsapp_url: str = "",
                  phone_lines: Optional[List[str]] = None,
                  linkedin_url: str = "",
                  title: str = "",
                  ) -> None:
    """Pin the house signature for a user (overwrites any learned one)."""
    try:
        cache = json.load(open(_SIG_CACHE))
    except Exception:
        cache = {}
    cache[user] = vp_signature(name, whatsapp_url, phone_lines,
                               linkedin_url, title=title)
    json.dump(cache, open(_SIG_CACHE, "w"), indent=1)
_QUOTE_RE = re.compile(
    r"^(>|On .{8,120} wrote:|From: .+|-{3,}\s*Original Message|"
    r"发件人|________________________________)", re.M)


def _strip_quotes(body: str) -> str:
    """Cut a sent body at the first quoted-history marker, so signature
    detection sees only the author's own text."""
    m = _QUOTE_RE.search(body or "")
    return (body[:m.start()] if m else (body or "")).rstrip()


def _signature(user: str) -> Dict[str, str]:
    """The user's signature as {plain, html}. A PINNED signature (set by
    the operator/user, stored in signatures.json as a dict) always wins;
    otherwise fall back to the learned plain block. VP standard
    practice: every draft carries the signature."""
    try:
        cache = json.load(open(_SIG_CACHE))
    except Exception:
        cache = {}
    v = cache.get(user)
    if isinstance(v, dict) and v.get("plain"):
        return {"plain": v["plain"], "html": v.get("html") or "",
                "inline_images": v.get("inline_images") or {},
                "closings": v.get("closings") or ["Best,", "Thank you,"]}
    plain = _extract_signature(user)
    return {"plain": plain, "html": "", "inline_images": {},
            "closings": ["Best,", "Thank you,"]}


def _extract_signature(user: str) -> str:
    """Learned fallback: the longest trailing line-block shared by a
    plurality of recent sent messages (quotes stripped first)."""
    try:
        cache = json.load(open(_SIG_CACHE))
    except Exception:
        cache = {}
    if isinstance(cache.get(user), str):
        return cache[user]
    conn = mail_store._connect(user)
    try:
        bodies = [_strip_quotes(r[0]) for r in conn.execute(
            "SELECT body_plain FROM messages WHERE label='SENT'"
            " ORDER BY date_ms DESC LIMIT 25") if r[0]]
    finally:
        conn.close()
    sig = ""
    tails = [[ln.strip() for ln in b.splitlines() if ln.strip()][-12:]
             for b in bodies if b.strip()]
    if len(tails) >= 4:
        newest = tails[0]
        for k in range(min(10, len(newest)), 1, -1):
            cand = newest[-k:]
            hits = sum(1 for t in tails if t[-k:] == cand)
            if hits >= max(3, int(len(tails) * 0.4)):
                sig = "\n".join(cand)
                break
    if not sig:
        from config import load_config as _lc2
        _co = (_lc2().get("agent", {}) or {}).get("company_name", "")
        sig = "Best regards," + chr(10) + user.capitalize() + (f" | {_co}" if _co else "")
    cache[user] = sig
    json.dump(cache, open(_SIG_CACHE, "w"))
    return sig


def _quoted_history(m: Dict[str, Any]) -> str:
    """The standard quoted block a reply carries below the signature —
    what makes the draft read (and thread) like a real reply."""
    ts = m.get("date_ms") or 0
    day = time.strftime("%a, %b %d, %Y at %H:%M",
                        time.localtime(ts / 1000 if ts > 10**12 else ts)) \
        if ts else ""
    who = m.get("from_name") or ""
    frm = m.get("from_email") or ""
    quoted = "\n".join("> " + ln for ln in
                       (m.get("body_plain") or "").splitlines()[:80])
    return f"On {day}, {who} <{frm}> wrote:\n{quoted}"


def _fetch_reply_headers(user: str, msg_id: str) -> Dict[str, Any]:
    """Live-fetch the original message for the fields the store doesn't
    mirror: smtp_message_id (RFC Message-ID), the References chain, and
    the FULL to/cc recipient sets — everything a Reply-All needs."""
    from lark_client import get_tenant_access_token
    mb = urllib.parse.quote(USERS[user][0], safe="")
    enc = urllib.parse.quote(str(msg_id), safe="")
    req = urllib.request.Request(
        f"https://open.larksuite.com/open-apis/mail/v1/user_mailboxes/"
        f"{mb}/messages/{enc}",
        headers={"Authorization": f"Bearer {get_tenant_access_token()}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        msg = (json.loads(r.read()).get("data") or {}).get("message") or {}
    def addrs(v):
        return [a.get("mail_address") or a.get("email")
                for a in (v or []) if isinstance(a, dict)
                and (a.get("mail_address") or a.get("email"))]
    def b64(s):
        try:
            s = (s or "").strip()
            return base64.urlsafe_b64decode(
                s + "=" * (-len(s) % 4)).decode("utf-8", "replace")
        except Exception:
            return ""
    frm = (msg.get("head_from") or {})
    return {
        "smtp_message_id": msg.get("smtp_message_id") or "",
        "references": msg.get("references") or "",
        "from": frm.get("mail_address") or frm.get("email") or "",
        "from_name": frm.get("name") or "",
        "to": addrs(msg.get("to")),
        "cc": addrs(msg.get("cc")),
        # The original's own HTML — quoted VERBATIM in the reply so all
        # formatting (and its internal earlier quotes) survives intact.
        "body_html": b64(msg.get("body_html")),
        "body_plain": b64(msg.get("body_plain_text")),
    }


def _create_draft(user: str, m: Dict[str, Any], body_text: str) -> bool:
    """OPERATOR RULE (2026-07-22): drafts are ALWAYS Reply-All and
    ALWAYS in-thread. To = original sender; Cc = every other original
    recipient (To+Cc minus the user, minus the sender); In-Reply-To +
    References carry the original's RFC Message-ID so every mail client
    (Lark included) threads the draft into the same conversation."""
    from lark_oauth import get_user_token
    mailbox, identity, _ = USERS[user]
    h = None
    for attempt in (1, 2, 3):
        try:
            h = _fetch_reply_headers(user, m["msg_id"])
            break
        except Exception as e:
            # fresh messages can 400 for a minute while Lark indexes
            # them — retry before degrading to a non-threaded reply
            print(f"[autodraft] header fetch attempt {attempt} failed "
                  f"({user}): {e}", file=sys.stderr, flush=True)
            time.sleep(3 * attempt)
    if h is None:
        h = {"smtp_message_id": "", "references": "", "from_name": "",
             "from": m.get("from_email") or "", "to": [], "cc": [],
             "body_html": "", "body_plain": ""}
    sender = h["from"] or (m.get("from_email") or "")
    # Reply-All recipient set, order-preserved, deduped, minus self+sender.
    cc, seen = [], {mailbox.lower(), sender.lower()}
    for a in (h["to"] + h["cc"]):
        if a.lower() not in seen:
            cc.append(a)
            seen.add(a.lower())
    em = EmailMessage()
    subj = m.get("subject") or ""
    em["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    em["From"] = mailbox
    em["To"] = sender
    if cc:
        em["Cc"] = ", ".join(cc)
    if h["smtp_message_id"]:
        em["In-Reply-To"] = h["smtp_message_id"]
        em["References"] = ((h["references"] + " ")
                            if h["references"] else "") + h["smtp_message_id"]
    # Reply like a real mail client: our text + signature on top, then
    # ONE quote level wrapping the original's OWN HTML verbatim — never
    # rebuild the thread from the plain-text mirror (it destroys the
    # formatting and flattens nested quotes).
    sig_rec = _signature(user)
    sig = sig_rec["plain"]
    ts = m.get("date_ms") or 0
    day = time.strftime("%a, %b %d, %Y at %H:%M",
                        time.localtime(ts / 1000 if ts > 10**12 else ts)) \
        if ts else ""
    who = h.get("from_name") or m.get("from_name") or ""
    import html as _html
    def para(text):
        return "".join(f"<div>{_html.escape(ln) or '<br>'}</div>"
                       for ln in text.splitlines())
    orig_html = h.get("body_html") or \
        para(h.get("body_plain") or m.get("body_plain") or "")
    sig_html = sig_rec["html"] or para(sig)
    html_body = (
        f"{para(body_text.rstrip())}<br>{sig_html}<br>"
        f"<div class='gmail_quote'>On {_html.escape(day)}, "
        f"{_html.escape(who)} &lt;{_html.escape(sender)}&gt; wrote:<br>"
        f"<blockquote style='margin:0 0 0 .8ex;border-left:1px #ccc "
        f"solid;padding-left:1ex'>{orig_html}</blockquote></div>")
    # plain-text alternative for clients that want it
    em.set_content(body_text.rstrip() + "\n\n" + sig + "\n\n"
                   + _quoted_history(m))
    em.add_alternative(html_body, subtype="html")
    # Inline signature images (e.g. the VP logo): attach as related
    # parts of the HTML alternative, referenced via cid: in the HTML.
    for cid, rel_path in (sig_rec.get("inline_images") or {}).items():
        p = rel_path if os.path.isabs(rel_path) \
            else os.path.join(get_home(), rel_path)
        try:
            with open(p, "rb") as f:
                img = f.read()
            em.get_payload()[-1].add_related(
                img, maintype="image",
                subtype=os.path.splitext(p)[1].lstrip(".") or "png",
                cid=f"<{cid}>")
        except Exception as e:
            print(f"[autodraft] inline image {cid} failed: {e}",
                  file=sys.stderr, flush=True)
    raw = base64.urlsafe_b64encode(em.as_bytes()).decode().rstrip("=")
    req = urllib.request.Request(
        "https://open.larksuite.com/open-apis/mail/v1/user_mailboxes/me/drafts",
        data=json.dumps({"raw": raw}).encode(), method="POST",
        headers={"Authorization": f"Bearer {get_user_token(identity)}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
            if d.get("code") != 0:
                return {"ok": False}
            return {"ok": True, "raw": raw,
                    "draft_id": ((d.get("data") or {}).get("draft")
                                 or {}).get("id") or ""}
    except urllib.error.HTTPError as e:
        print(f"[autodraft] draft create failed ({user}): "
              f"{e.read().decode()[:150]}", file=sys.stderr, flush=True)
        return {"ok": False}


def _dm(user: str, text: str) -> None:
    try:
        from lark_client import LarkClient
        LarkClient().send_text(USERS[user][2], text,
                               receive_id_type="open_id")
    except Exception as e:
        print(f"[autodraft] DM failed ({user}): {e}",
              file=sys.stderr, flush=True)


def _has_consent(user: str) -> bool:
    return os.path.exists(os.path.join(
        get_home(), "lark", f"user_token_{USERS[user][1]}.json"))


def run(only_user: Optional[str] = None, limit: int = _MAX_PER_RUN,
        dry: bool = False) -> Dict[str, Any]:
    st = _state()
    report: Dict[str, Any] = {}
    for user in USERS:
        if only_user and user != only_user:
            continue
        if not _has_consent(user):
            report[user] = "no consent token — skipped"
            continue
        since = int(st.get(user) or 0)
        if since == 0:
            # First run: start from NOW — never backfill-draft old mail.
            conn = mail_store._connect(user)
            since = conn.execute(
                "SELECT COALESCE(MAX(date_ms),0) FROM messages"
                " WHERE label='INBOX'").fetchone()[0]
            conn.close()
            st[user] = since
            report[user] = "initialized cursor to newest message"
            continue
        cands = _candidates(user, since, limit)
        drafted, skipped = 0, 0
        for m in cands:
            g = _generate(user, m)
            if g and g.get("needs_reply") and (g.get("draft") or "").strip():
                # HARD RULE: no meta-commentary in the body — strip any
                # that slipped through the prompt; it moves to the card.
                body, stripped_notes = strip_meta(g["draft"].strip())
                note = "; ".join(x for x in
                                 (g.get("note_to_user") or "",
                                  stripped_notes) if x)
                if dry:
                    drafted += 1
                elif not body:
                    skipped += 1
                else:
                    res = _create_draft(user, m, body)
                    if res.get("ok"):
                        drafted += 1
                        # review card in the owner's DM: subject +
                        # received + draft + Send/Discard/Edit buttons
                        import autodraft_card
                        try:
                            conf = max(0, min(100,
                                              int(g.get("confidence"))))
                        except (TypeError, ValueError):
                            conf = -1          # unknown → neutral display
                        autodraft_card.enqueue_and_notify(
                            user, USERS[user][2], USERS[user][1],
                            res.get("draft_id") or "", m,
                            body, res["raw"], note=note,
                            confidence=conf,
                            missing=(g.get("missing") or "")[:300])
                    else:
                        skipped += 1
            else:
                skipped += 1
                # every skip is LOGGED with its reason — an email must
                # never disappear silently (operator, 2026-07-22)
                why = (g or {}).get("reason") or \
                    ("triage: no reply needed" if g else "generate failed")
                print(f"[autodraft] {user}: skipped "
                      f"“{(m.get('subject') or '')[:50]}” from "
                      f"{m.get('from_email')} — {why}",
                      file=sys.stderr, flush=True)
            st[user] = max(st[user], int(m.get("date_ms") or 0))
        if not dry:
            _save_state(st)
        report[user] = {"drafted": drafted, "skipped": skipped,
                        "examined": len(cands)}
    if not dry:
        _save_state(st)
    return report


def redo_draft(qid: int, instruction: str,
               requester_open_id: str) -> str:
    """Regenerate a pending queued draft per the owner's instruction
    ("redo q#8: shorter, confirm Thursday"). Deletes the old Lark draft
    (sanctioned draft-delete), creates a new one + a fresh card, marks
    the old queue row superseded. Returns a short human reply."""
    import autodraft_card
    r = autodraft_card._row(qid)
    if not r:
        return f"I don't have a draft q#{qid}."
    if requester_open_id != r["owner_open_id"]:
        return "Only the mailbox owner can redo that draft."
    if r["status"] != "pending":
        return f"q#{qid} is already {r['status']} — nothing to redo."
    user = r["user"]
    # the original inbound, from the user's own mirror
    conn = mail_store._connect(user)
    m = conn.execute(
        "SELECT msg_id, thread_id, date_ms, from_email, from_name,"
        " to_json, cc_json, subject, body_plain FROM messages"
        " WHERE msg_id=?", (r["msg_id"],)).fetchone()
    conn.close()
    if not m:
        return f"The original email behind q#{qid} isn't in your mirror."
    m = dict(m)
    g = _generate_redo(user, m, r["draft_body"] or "", instruction)
    if not g or not (g.get("draft") or "").strip():
        return "Redo failed — the generator returned nothing. Try again."
    body, stripped = strip_meta(g["draft"].strip())
    if not body:
        return "Redo produced only editor notes — try rephrasing."
    note = "; ".join(x for x in (g.get("note_to_user") or "", stripped) if x)
    res = _create_draft(user, m, body)
    if not res.get("ok"):
        return "Redo drafted, but saving to your Mail Drafts failed."
    # retire the old draft + card
    if r.get("lark_draft_id"):
        autodraft_card._delete_draft(r["identity"], r["lark_draft_id"])
    autodraft_card.mark_superseded(qid)
    try:
        conf = max(0, min(100, int(g.get("confidence"))))
    except (TypeError, ValueError):
        conf = -1
    new_qid = autodraft_card.enqueue_and_notify(
        user, r["owner_open_id"], r["identity"],
        res.get("draft_id") or "", m, body, res["raw"],
        note=note, confidence=conf, missing=(g.get("missing") or "")[:300])
    return (f"🔄 Redone — new draft is q#{new_qid} (card above). "
            f"The old q#{qid} draft was removed.")


def _generate_redo(user: str, m: Dict[str, Any], old_draft: str,
                   instruction: str) -> Optional[Dict[str, Any]]:
    """One-shot regeneration: same grounding as _generate's phase B, plus
    the previous draft and the owner's explicit instruction."""
    name = user.capitalize()
    query = f"{m.get('subject') or ''} {(m.get('body_plain') or '')[:400]}"
    pb = email_playbook.search(query, k=3)
    pb_txt = "\n\n".join(
        f"[{e['situation_type']}] {e['situation']}\nAPPROACH: {e['approach']}"
        f"\nTONE: {e['tone']}\nEXEMPLAR:\n{(e['exemplar'] or '')[:900]}"
        for e in pb) or "(no matching playbook entries yet)"
    style = "\n\n---\n\n".join(_style_exemplars(user, query)) or "(none found)"
    from config import load_config as _lc
    company = (_lc().get("agent", {}) or {}).get("company_name", "the company")
    closings = " / ".join(f'"{c}"' for c in _signature(user)["closings"])
    prompt = _PROMPT.format(
        research_spec="", name=name, first=name, company=company,
        closings=closings,
        from_email=m.get("from_email") or "?",
        subject=(m.get("subject") or "")[:150],
        body=(m.get("body_plain") or "")[:2400],
        thread=_thread_context(user, m.get("thread_id") or "",
                               m.get("date_ms") or 0) or "(none)",
        playbook=pb_txt, style=style) + (
        f"\n\nYOU ALREADY DRAFTED THIS REPLY once. {name} reviewed it and "
        f"wants it REDONE. Their instruction (follow it exactly; it "
        f"overrides style defaults but never the never-invent and "
        f"no-meta-commentary rules):\n{instruction[:600]}\n\n"
        f"PREVIOUS DRAFT:\n{old_draft[:2000]}\n\n"
        f"Output the same JSON with the revised draft; set "
        f"\"complete\": true; needs_reply true.")
    return _llm_json(prompt)


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "help"
    if cmd == "run":
        u = args[args.index("--user") + 1] if "--user" in args else None
        lim = int(args[args.index("--limit") + 1]) if "--limit" in args \
            else _MAX_PER_RUN
        print(json.dumps(run(u, lim, dry="--dry" in args), indent=2))
    elif cmd == "status":
        print(json.dumps({"state": _state(),
                          "consent": {u: _has_consent(u) for u in USERS}},
                         indent=2))
    elif cmd == "test-thread" and len(args) > 1:
        # Verify reply-all + threading in the real mail UI: drafts a
        # plain marker reply to the user's newest To-addressed inbound.
        u = args[1]
        conn = mail_store._connect(u)
        rows = [dict(r) for r in conn.execute(
            "SELECT msg_id, thread_id, date_ms, from_email, from_name,"
            " to_json, cc_json, subject, body_plain FROM messages"
            " WHERE label='INBOX' AND is_noreply=0"
            " ORDER BY date_ms DESC LIMIT 40")]
        conn.close()
        eligible = [m for m in rows
                    if USERS[u][0] in json.loads(m.get("to_json") or "[]")
                    and (m.get("from_email") or "") != USERS[u][0]]
        # Prefer a MULTI-recipient email so the test also proves the
        # Cc-preservation half of Reply-All; fall back to any.
        target = next(
            (m for m in eligible
             if len(json.loads(m.get("to_json") or "[]"))
             + len(json.loads(m.get("cc_json") or "[]")) >= 2),
            eligible[0] if eligible else None)
        if not target:
            print("no recent To-addressed inbound found")
        else:
            body = ("[Noto threading test — safe to delete]\n"
                    "This draft should sit INSIDE the thread above, with "
                    "everyone from the original email kept on copy "
                    "(Reply-All).")
            res = _create_draft(u, target, body)
            print(("✓ test draft created — check the thread: "
                   if res.get("ok") else "✗ failed on: ")
                  + f"“{(target.get('subject') or '')[:70]}”")
            if res.get("ok") and "--card" in args:
                import autodraft_card
                qid = autodraft_card.enqueue_and_notify(
                    u, USERS[u][2], USERS[u][1],
                    res.get("draft_id") or "", target, body, res["raw"])
                print(f"✓ review card DM'd (queue #{qid})")
    else:
        print(__doc__)
