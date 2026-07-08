"""
Noto agent — LLM dispatcher that picks skills + tools per request.

The bot is a window. THIS is the agent. It reads the user's message
+ recent chat history, decides what to do (which skill with which
args), executes via the skill modules, and updates the streaming
card. When the user pushes back ("that's not what I asked"), the
agent re-reads the history including its own prior action and
CORRECTS.

NOT here:
  - the strict-command path (/help, ...) still runs in _triage —
    those are unambiguous and shouldn't pay an LLM round-trip.
  - pending-state confirms (calendar slot-fills) keep their existing
    handlers — they're explicit slot-fills.

Everything else flows here.
"""

import json
import os
import re
import sys
import time
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Context gathering
# ---------------------------------------------------------------------------

_DOC_URL_RE = re.compile(r"https?://[^\s)>'\"]+/docx/([A-Za-z0-9]+)")


def _recent_doc_links(chat_id: str, max_docs: int = 4) -> List[str]:
    """Walk recent bot messages in this chat for Lark doc URLs we
    sent. Newest first — so the planner can resolve a short imperative
    ("tighten the intro") to the doc it just created or edited."""
    from lark_bot import _CHAT_HISTORY
    history = _CHAT_HISTORY.get(chat_id, [])
    tokens: List[str] = []
    for role, text in reversed(history):
        if role != "noto" or not text:
            continue
        for m in _DOC_URL_RE.finditer(text):
            tok = m.group(1)
            if tok not in tokens:
                tokens.append(tok)
                if len(tokens) >= max_docs:
                    return tokens
    return tokens


def _history_text(history: List[Any], n: int = 8) -> str:
    """Last n turns formatted for the planner prompt."""
    if not history:
        return "(empty)"
    lines = []
    for role, text in history[-n:]:
        t = (text or "").strip().replace("\n", " ")
        if len(t) > 400:
            t = t[:400] + "…"
        who = "USER" if role == "user" else "NOTO"
        lines.append(f"  {who}: {t}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Planner — one LLM call → structured plan
# ---------------------------------------------------------------------------

_PLAN_PROMPT = """\
You are Noto, your organization's knowledge agent on Lark. You see the
user's message + recent chat context. Decide what to do.

SKILLS YOU CAN CALL (each takes the args shown):
- answer_question(question?)
    General Q&A — research the company corpus (docs + chats + vector
    RAG) and answer. Use for any INFORMATIONAL request that isn't a
    doc or calendar operation: "summarize the Q3 planning doc", "what
    did we decide about the pricing change", "who owns the onboarding
    process", "compare the two vendor proposals". If `question` is
    omitted, the user's original message is used as the question. This
    is the DEFAULT when no other skill obviously applies.

- create_doc(title, content_or_brief, folder?, is_brief=true)
    Create a NEW Lark doc — meeting notes, a memo, a writeup, anything
    the user wants captured as a document. If `is_brief=true`
    (default), `content_or_brief` is a DESCRIPTION of what to write
    and the skill generates the body via LLM. If `is_brief=false`,
    it's the literal markdown body and we use it verbatim. `folder` is
    an optional Drive folder token; omit it and the doc lands in the
    configured outputs folder.

- edit_doc(doc_id, instruction)
    LLM-driven edit of an existing Lark doc by instruction: "tighten
    the intro", "add a section on remote setup to the onboarding wiki
    page", "remove the stale Q2 numbers". `doc_id` may be a docx token
    OR the full pasted Lark URL — including /wiki/ page links (they
    resolve to the underlying doc automatically; if the user pasted a
    link, pass it through verbatim rather than extracting the token
    yourself). Base/sheet links are not editable and will be explained
    to the user. When the user refers to a doc you just created or
    edited, pick doc_id from RECENT DOC LINKS below.

- add_calendar_entry(details)
    The user asks to put something on THEIR calendar ("add to my
    calendar dinner with Joe Kim on Wednesday 8pm at COTE", "schedule
    a call with X tomorrow 3pm"). Pass their request text through
    VERBATIM as `details` — extraction happens downstream, and missing
    info (time, venue for in-person) gets asked automatically.

- add_reminder(text)
    The user asks Noto to remind/remember a personal to-do ("noto
    remind me to call Joe tomorrow at 3pm", "remind me to send the
    report on friday", "don't let me forget to reply to Kim"). Pass
    their request VERBATIM as `text` — date/time words are resolved
    downstream against their own timezone. The reminder lands in their
    personal "Noto — <name>" Lark task list; a specific clock time
    gets a Lark alert at that time, and due/overdue items show in
    their morning digest. This is for THEIR OWN to-dos:
    meetings/calls with a counterparty belong on the calendar
    (add_calendar_entry).

- list_reminders()
    "what's on my list", "show my reminders / my tasks", "what did I
    ask you to remind me about".

- complete_reminder(query)
    "done with the Joe call", "mark the report one done", "clear the
    reminder about Kim" — `query` = the user's words identifying
    which reminder to tick off.

- clarify(question)
    Reply WITHOUT taking action — use when you need more info OR when
    two paths are plausible and the user should pick. COMPOSE THE
    QUESTION USING THE DATA YOU HAVE: reference what's actually in the
    chat (docs you sent, what they asked earlier) and ask like a human
    would. Don't template generic questions — that's rigid.

- defer
    Use ONLY as a last-resort escape — the caller will fall through to
    the direct research path. Prefer answer_question for anything
    informational; prefer clarify if you're not sure what to do.

SELF-CORRECTION RULES:
- The chat history shows what YOU (NOTO) replied previously. If the
  user's CURRENT MESSAGE is pushing back on a prior action ("no
  that's wrong", "I said a new doc", "not what I asked"), re-read the
  relevant prior turn and DO what they actually wanted now. Don't
  apologize and ask — fix it.
- If you previously edited the wrong doc, don't try to "undo" — take
  the right action now (e.g. create a fresh doc) and acknowledge it
  briefly in `reply`.

USERS WRITE INFORMALLY. Casual phrasing, details omitted. Use chat
history to fill in what's implicit. A single short imperative
("tighten the intro") right after a doc was created or edited →
edit_doc with that doc's doc_id from RECENT DOC LINKS.

RECENT DOC LINKS WE SENT in this chat (most recent first):
{recent_docs_block}
CHAT HISTORY (oldest → newest, last 8 turns):
{history}
{reply_block}
CURRENT MESSAGE:
{message}

Output ONE JSON object on a single line. No prose, no fences. Schema:
{{"skill": "answer_question|create_doc|edit_doc|add_calendar_entry|add_reminder|list_reminders|complete_reminder|clarify|defer",
  "args": {{...}},
  "reply": "optional short text to put in the card AFTER the skill runs (e.g. ack of self-correction)",
  "reason": "one short sentence explaining your choice"}}
"""


def _plan(message: str, history: List[Any], parent_text: str,
          recent_docs: List[str]) -> Dict[str, Any]:
    from noto_research import _claude
    if recent_docs:
        rb = "\n".join(f"  - doc_id={t!r}" for t in recent_docs)
    else:
        rb = "  (none — no doc links sent recently)"
    reply_block = (
        f"\nUSER REPLIED DIRECTLY TO THIS PRIOR BOT MESSAGE:\n  "
        f"\"{parent_text}\"\n" if parent_text else "")
    prompt = _PLAN_PROMPT.format(
        recent_docs_block=rb,
        history=_history_text(history),
        reply_block=reply_block, message=message)
    raw = _claude(prompt, timeout=45, web=False)
    if not raw:
        return {"skill": "defer", "args": {}, "reply": "",
                "reason": "planner LLM returned empty"}
    try:
        plan = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        plan = {}
        if m:
            try:
                plan = json.loads(m.group(0))
            except Exception:
                pass
    if not isinstance(plan, dict) or "skill" not in plan:
        return {"skill": "defer", "args": {}, "reply": "",
                "reason": "planner LLM unparseable"}
    plan.setdefault("args", {})
    plan.setdefault("reply", "")
    plan.setdefault("reason", "")
    if plan["skill"] not in (
            "answer_question", "create_doc", "edit_doc",
            "add_calendar_entry",
            "add_reminder", "list_reminders", "complete_reminder",
            "clarify", "defer"):
        plan["skill"] = "defer"
    return plan


# ---------------------------------------------------------------------------
# Executor — runs the plan, drives the card, returns "defer" / "done"
# ---------------------------------------------------------------------------

# Observability ONLY — the last plan, read by lark_bot's _worker (single
# thread) right after handle() returns, to attribute usage by skill for
# the admin panel's use-case analytics. Never read inside this module;
# no effect on planning or execution.
LAST_PLAN: Dict[str, Any] = {}


def _resolve_doc_ref(raw: str, message: str) -> Dict[str, Any]:
    """Turn whatever the planner put in doc_id — a docx token, a full
    URL, or a wiki NODE token it lifted from a /wiki/ link — into the
    editable docx token. Uses lark_url against the links actually
    present in the message, so wiki links resolve to the wrapped
    object and base/sheet links fail with a reason instead of a
    cryptic API error.

    Returns {"doc_id": token, "kind": "docx", "via": ...} on success,
    else {"error": <user-facing sentence>}."""
    raw = (raw or "").strip()
    try:
        from lark_url import resolve, extract_links
    except Exception:
        return {"doc_id": raw, "kind": "docx", "via": "passthrough"}
    _KIND_LABEL = {"base": "a Base (Bitable)", "sheets": "a spreadsheet",
                   "folder": "a Drive folder", "file": "a binary file",
                   "mindnote": "a mindnote"}

    def _judge(res: Dict[str, Any]) -> Dict[str, Any]:
        if not res.get("ok"):
            return {"error": "I couldn't resolve that Lark link"
                    + (f" ({res.get('reason')})" if res.get("reason")
                       else "") + " — could you re-paste it?"}
        kind = res.get("kind")
        if kind in ("docx", "doc"):
            return {"doc_id": res["token"], "kind": "docx",
                    "via": res.get("url_kind", ""),
                    "title": res.get("title", "")}
        return {"error": f"That link points to "
                f"{_KIND_LABEL.get(kind, f'a {kind}')} — I can only "
                f"edit Lark docs (docx), including wiki doc pages."}

    if raw.startswith("http"):
        return _judge(resolve(raw))
    links = extract_links(message or "")
    for res in links:
        # the planner may hand us either the raw URL token (for /wiki/
        # links that's the NODE token) or the already-correct obj token
        if raw and (raw == res.get("token")
                    or f"/{raw}" in (res.get("url") or "")):
            return _judge(res)
    if raw:
        return {"doc_id": raw, "kind": "docx", "via": "bare_token"}
    if len(links) == 1:
        return _judge(links[0])
    return {"error": "Which doc should I edit? Reply with the doc "
            "link or doc_id."}


def _route_doc_edit(args: Dict[str, Any], message: str
                    ) -> "tuple[str, Dict[str, Any], str]":
    """(skill, args, note) for an edit_doc plan. Resolves the link; on
    resolution failure returns a clarify plan so the user gets a real
    sentence."""
    ref = _resolve_doc_ref(args.get("doc_id") or "", message)
    if ref.get("error"):
        return ("clarify", {"question": ref["error"]},
                "unresolvable link")
    new_args = dict(args)
    new_args["doc_id"] = ref["doc_id"]
    note = ""
    if ref.get("via") == "wiki":
        note = "wiki link resolved to docx"
    return ("edit_doc", new_args, note)


def handle(message: str, chat_id: str, sender_open_id: str,
           history: List[Any], parent_text: str, card: Any,
           client: Any, user_context: str = "") -> str:
    """Plan-and-execute the user's request.

    Returns:
      'done'   — agent owned the response (card finalized here)
      'defer'  — last-resort escape; caller should fall through to the
                 direct research path
    """
    recent_docs = _recent_doc_links(chat_id)
    plan = _plan(message, history, parent_text, recent_docs)
    print(f"[noto_agent] plan={plan.get('skill')} args={plan.get('args')} "
          f"reason={plan.get('reason')!r}", file=sys.stderr, flush=True)

    skill = plan["skill"]
    args = plan["args"] or {}
    reply = plan.get("reply", "")
    LAST_PLAN.update(chat_id=chat_id, sender=sender_open_id,
                     skill=skill, ts=time.time())

    # Doc-by-link: normalize edit_doc plans BEFORE the branch chain —
    # resolve pasted /wiki/ (and any other) Lark links to the editable
    # docx token.
    if skill == "edit_doc":
        skill, args, reroute_note = _route_doc_edit(args, message)
        if reroute_note:
            print(f"[noto_agent] doc-edit reroute → {skill} "
                  f"({reroute_note})", file=sys.stderr, flush=True)
            LAST_PLAN.update(skill=skill)

    if skill == "defer":
        return "defer"

    if skill == "clarify":
        msg = (args.get("question") or args.get("message") or reply
               or "Could you say a bit more about what you'd like me "
               "to do?")
        if card:
            card.finalize(status="❓ Need a bit more",
                          answer=msg, error=False)
        try:
            from lark_bot import _record_bot
            _record_bot(chat_id, msg)
        except Exception:
            pass
        return "done"

    on_prog = card.progress if card else None

    if skill == "answer_question":
        # General research / Q&A. Streams to the card via on_token
        # exactly like the legacy direct-research path — the card UX is
        # unchanged.
        question = (args.get("question") or "").strip() or message
        from skills.research import answer_question as _answer
        on_tok = card.stream_answer if card else None
        try:
            answer = _answer(question, history=history,
                             user_context=user_context,
                             on_progress=on_prog, on_token=on_tok)
        except Exception as e:
            if card:
                card.finalize(status="⚠️ research failed",
                              answer=f"I hit an error researching that: "
                              f"{e}", error=True)
            return "done"
        if card:
            # research already streamed the body via on_token; finalize
            # commits the terminal state.
            try:
                card.finalize(answer=answer)
            except Exception:
                pass
        try:
            from lark_bot import _record_bot
            _record_bot(chat_id, answer[:2000])
        except Exception:
            pass
        return "done"

    if skill == "create_doc":
        title = (args.get("title") or "").strip()
        content = (args.get("content_or_brief")
                   or args.get("content") or "").strip()
        folder = (args.get("folder") or "").strip()
        is_brief = bool(args.get("is_brief", True))
        if not title:
            if card:
                card.finalize(
                    status="❓ need a title",
                    answer="What should I title the doc?")
            return "done"
        if not content:
            if card:
                card.finalize(
                    status="❓ need content / brief",
                    answer="What should the doc say? Give me a short "
                    "brief or paste the content.")
            return "done"
        from skills.general_doc import create_doc as _cd
        res = _cd(client, title, content, folder=folder,
                  is_brief=is_brief, on_progress=on_prog)
        if not res.get("ok"):
            if card:
                card.finalize(
                    status=f"⚠️ {res.get('error', 'doc creation failed')}",
                    answer=(res.get("draft", "") or "")[:1500],
                    error=True)
            return "done"
        if card:
            where = res.get("landed_in", "")
            body = reply or (
                f"Created **{res['title']}**"
                + (f" in **{where}**" if where else "") + ".")
            card.finalize(
                status="📄 doc created",
                answer=body,
                note=f"[open the document]({res['doc_url']})")
        try:
            from lark_bot import _record_bot
            _record_bot(chat_id, f"📄 Created '{res['title']}': "
                        f"{res['doc_url']}")
        except Exception:
            pass
        return "done"

    if skill == "edit_doc":
        doc_id = (args.get("doc_id") or "").strip()
        instr = (args.get("instruction") or message).strip()
        if not doc_id:
            if card:
                card.finalize(
                    status="❓ which doc",
                    answer="Which doc should I edit? Reply with the "
                    "doc link or doc_id.")
            return "done"
        from skills.general_doc import edit_doc as _ed
        res = _ed(client, doc_id, instr, on_progress=on_prog)
        if not res.get("ok"):
            if card:
                card.finalize(
                    status=f"⚠️ {res.get('error', 'edit failed')}",
                    answer=(res.get("new_draft", "") or "")[:1500],
                    error=True)
            return "done"
        if card:
            body = reply or f"Updated per: *{instr[:200]}*"
            note = ""
            # try to construct a clickable link from the doc_url cache
            try:
                from noto_research import _doc_url
                note = f"[open the document]({_doc_url(doc_id)})"
            except Exception:
                pass
            card.finalize(status="✅ doc updated", answer=body,
                          note=note)
        try:
            from lark_bot import _record_bot
            _record_bot(chat_id, f"✅ doc updated: {instr[:100]}")
        except Exception:
            pass
        return "done"

    if skill == "add_calendar_entry":
        details = (args.get("details") or message).strip()
        import screenshot_calendar as sc
        import lark_bot as _lb
        ev = sc.extract_from_text(details)
        if not ev or not ev.get("is_event"):
            if card:
                card.finalize(status="❓ Need a bit more",
                              answer="I couldn't find an event in that — "
                              "tell me what, when and (if in person) "
                              "where, e.g. 'add to my calendar dinner "
                              "with Joe Kim on Wednesday 8pm at COTE'.")
            return "done"
        miss = sc.missing_fields(ev)
        if miss:
            _lb._PENDING_CAL_EVENT[chat_id] = {
                "event": ev, "sender": sender_open_id,
                "ts": time.time()}
            body = sc.question_for(miss, ev)
        else:
            clash = sc.check_clashes(sender_open_id, ev)
            if clash.get("duplicate") or clash.get("conflicts"):
                _lb._PENDING_CAL_EVENT[chat_id] = {
                    "event": ev, "sender": sender_open_id,
                    "ts": time.time(), "stage": "decide"}
                body = sc.clash_question(clash, ev)
            else:
                res = sc.create_for(sender_open_id, ev)
                body = (sc.confirmation(ev, res) if res.get("ok")
                        else f"Couldn't create the event "
                             f"({res.get('error', '?')}).")
        if card:
            card.finalize(status="📅 Calendar", answer=body)
        try:
            from lark_bot import _record_bot
            _record_bot(chat_id, body)
        except Exception:
            pass
        return "done"

    if skill == "add_reminder":
        details = (args.get("text") or args.get("details")
                   or message).strip()
        from skills.reminders import add_reminder as _addr
        res = _addr(sender_open_id, details,
                    on_progress=(card.progress if card else None))
        if res.get("ok"):
            body = f"⏰ Got it — I'll remind you to **{res['summary']}**"
            if res.get("due_display"):
                body += f" ({res['due_display']}"
                body += (", with a Lark alert at that time)."
                         if res.get("alert") else ").")
            else:
                body += "."
            body += (f"\nIt's on your “{res.get('tasklist_name','Noto')}” "
                     f"list in Lark Tasks — due items show up in your "
                     f"morning digest.")
        elif res.get("error") == "reminders_disabled":
            body = ("Reminders aren't switched on for this bot yet — "
                    "ask your admin to enable h2.reminders_enabled.")
        elif res.get("error") == "not_a_reminder":
            body = ("I couldn't find a to-do in that — try something "
                    "like “remind me to call Joe tomorrow 3pm”.")
        else:
            body = (f"Couldn't save that reminder "
                    f"({res.get('error', '?')}).")
        if card:
            card.finalize(status="⏰ Reminders", answer=body)
        try:
            from lark_bot import _record_bot
            _record_bot(chat_id, body)
        except Exception:
            pass
        return "done"

    if skill == "list_reminders":
        from skills.reminders import list_reminders as _lsr
        res = _lsr(sender_open_id)
        if res.get("error") == "reminders_disabled":
            body = ("Reminders aren't switched on for this bot yet — "
                    "ask your admin to enable h2.reminders_enabled.")
        elif not res.get("ok"):
            body = f"Couldn't fetch your list ({res.get('error','?')})."
        elif not res.get("items"):
            body = ("📋 Your list is clear — nothing pending. Say "
                    "“remind me to …” to add something.")
        else:
            lines = [f"📋 You have {res['count']} open "
                     f"reminder{'s' if res['count'] != 1 else ''}:"]
            for it in res["items"]:
                line = f"  • {it['summary']}"
                if it.get("due_display"):
                    line += (f"  _({it['due_display']}"
                             + (" — overdue ⚠️" if it.get("overdue")
                                else "") + ")_")
                lines.append(line)
            lines.append("\n_Say “done with …” to tick one off._")
            body = "\n".join(lines)
        if card:
            card.finalize(status="📋 Your reminders", answer=body)
        try:
            from lark_bot import _record_bot
            _record_bot(chat_id, body)
        except Exception:
            pass
        return "done"

    if skill == "complete_reminder":
        q = (args.get("query") or "").strip() or message
        from skills.reminders import complete_reminder as _cpr
        res = _cpr(sender_open_id, q)
        if res.get("ok"):
            body = f"✅ Done — ticked off **{res['summary']}**."
        elif res.get("error") == "reminders_disabled":
            body = ("Reminders aren't switched on for this bot yet — "
                    "ask your admin to enable h2.reminders_enabled.")
        elif res.get("error") == "no_open_reminders":
            body = "Your list is already clear — nothing to tick off."
        elif res.get("error") == "ambiguous":
            body = ("A few reminders match — which one?\n" +
                    "\n".join(f"  • {s}" for s in res.get("items", [])))
        elif res.get("error") == "no_match":
            body = ("Nothing on your list matches that. Open items:\n" +
                    "\n".join(f"  • {s}" for s in res.get("items", [])))
        else:
            body = (f"Couldn't complete that reminder "
                    f"({res.get('error', '?')}).")
        if card:
            card.finalize(status="⏰ Reminders", answer=body)
        try:
            from lark_bot import _record_bot
            _record_bot(chat_id, body)
        except Exception:
            pass
        return "done"

    # Unknown skill name — defer
    return "defer"
