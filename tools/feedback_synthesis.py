"""
Feedback synthesis — the reasoning layer between raw user feedback
and the rules Noto actually obeys.

Raw feedback (the `feedback` table: users reacting to Noto's
output — "that's not what I meant", "do it this way") is EVIDENCE, not
rules. Some of it is a one-off comment about one specific document;
some of it is "that's not how we do things here". Promoting raw items
straight into lessons files skips the thinking step.

This module has Noto read the accumulated, not-yet-synthesized feedback
and derive LESSONS, each carrying:
  - lesson_text     — the proposed rule, in injectable form
  - scope           — global | workflow | engineering |
                      candidate_specific | insufficient_evidence
  - reasoning       — WHY Noto concluded this (the audit trail)
  - supporting_feedback_ids — exactly which feedback items fed it

Every input item lands in some lesson row — including "this is a
candidate-specific one-off, no rule derivable" — so nothing is silently
dropped and the operator can always see what Noto did with a piece of
feedback.

Scopes global/workflow/engineering surface as status='pending' for
review in the admin panel; candidate_specific/insufficient_evidence are
auto-parked as status='deferred' (visible, no action required). A later
synthesis pass may SUPERSEDE a deferred lesson when new feedback
strengthens the pattern — the superseding lesson absorbs its evidence.

Approving a lesson routes it (global→general lessons, workflow→that
workflow's lessons file, engineering→brain backlog) and resolves its
supporting feedback rows; rejecting resolves them as rejected. Either
way the raw queue drains through the lesson decision — one review
surface, with reasoning attached.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SCOPES = ("global", "workflow", "engineering",
           "candidate_specific", "insufficient_evidence",
           "personal_preference")
_ACTIONABLE = ("global", "workflow", "engineering")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS derived_lessons (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_text             TEXT NOT NULL,
    scope                   TEXT NOT NULL CHECK (scope IN
        ('global','workflow','engineering',
         'candidate_specific','insufficient_evidence',
         'personal_preference')),
    workflow                TEXT NOT NULL DEFAULT '',
    reasoning               TEXT NOT NULL DEFAULT '',
    supporting_feedback_ids TEXT NOT NULL DEFAULT '[]',
    superseded_lesson_ids   TEXT NOT NULL DEFAULT '[]',
    confidence              REAL NOT NULL DEFAULT 0.5,
    model                   TEXT NOT NULL DEFAULT '',
    synthesized_at          TEXT,
    status                  TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
        ('pending','deferred','approved','rejected','superseded')),
    reviewed_at             TEXT,
    reviewed_by             TEXT,
    reviewed_note           TEXT
);
"""


def _store():
    from feedback_store import FeedbackStore
    s = FeedbackStore()
    s.conn.executescript(_SCHEMA)
    _migrate_pp_scope(s.conn)
    s.conn.commit()
    return s


def _migrate_pp_scope(conn) -> None:
    """One-time table rebuild: the pre-F1 derived_lessons CHECK doesn't
    allow scope='personal_preference' and SQLite can't ALTER a CHECK.
    Rebuild preserves ids and all rows; no-op once migrated."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='derived_lessons'").fetchone()
    if not row or "personal_preference" in (row[0] or ""):
        return
    cols = ("id, lesson_text, scope, workflow, reasoning, "
            "supporting_feedback_ids, superseded_lesson_ids, confidence, "
            "model, synthesized_at, status, reviewed_at, reviewed_by, "
            "reviewed_note")
    conn.executescript(
        "ALTER TABLE derived_lessons RENAME TO derived_lessons_pre_f1;"
        + _SCHEMA +
        f"INSERT INTO derived_lessons ({cols}) "
        f"SELECT {cols} FROM derived_lessons_pre_f1;"
        "DROP TABLE derived_lessons_pre_f1;")
    print("[feedback_synthesis] migrated derived_lessons: scope CHECK "
          "now allows personal_preference", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def unsynthesized_feedback(limit: int = 40) -> List[Dict[str, Any]]:
    """Unresolved feedback rows not yet linked to any lesson."""
    s = _store()
    try:
        consumed: set = set()
        for (ids_json,) in s.conn.execute(
                "SELECT supporting_feedback_ids FROM derived_lessons"):
            try:
                consumed.update(json.loads(ids_json or "[]"))
            except Exception:
                pass
        rows = [dict(r) for r in s.conn.execute(
            "SELECT id, user_name, workflow, kind, source, feedback_text, "
            "context_snippet, created_at FROM feedback "
            "WHERE status='unresolved' ORDER BY created_at ASC")]
        return [r for r in rows if r["id"] not in consumed][:limit]
    finally:
        s.close()


def list_lessons(status: Optional[str] = None, scope: Optional[str] = None,
                 limit: int = 200) -> List[Dict[str, Any]]:
    s = _store()
    try:
        wh, args = [], []
        if status and status != "all":
            wh.append("status=?"); args.append(status)
        if scope:
            wh.append("scope=?"); args.append(scope)
        sql = ("SELECT * FROM derived_lessons"
               + (" WHERE " + " AND ".join(wh) if wh else "")
               + " ORDER BY CASE status WHEN 'pending' THEN 0 "
                 "WHEN 'deferred' THEN 1 ELSE 2 END, "
                 "confidence DESC, id DESC LIMIT ?")
        lessons = [dict(r) for r in s.conn.execute(sql, args + [limit])]
        # attach the evidence (quoted feedback) — the audit trail
        for les in lessons:
            try:
                ids = json.loads(les["supporting_feedback_ids"] or "[]")
            except Exception:
                ids = []
            les["evidence"] = []
            if ids:
                q = ",".join("?" * len(ids))
                les["evidence"] = [dict(r) for r in s.conn.execute(
                    f"SELECT id, user_name, workflow, kind, source, "
                    f"feedback_text, context_snippet, created_at, status "
                    f"FROM feedback WHERE id IN ({q})", ids)]
        return lessons
    finally:
        s.close()


def stats() -> Dict[str, Any]:
    s = _store()
    try:
        by_status = dict(s.conn.execute(
            "SELECT status, COUNT(*) FROM derived_lessons GROUP BY status"))
    finally:
        s.close()
    return {"by_status": by_status,
            "unsynthesized": len(unsynthesized_feedback(limit=1000))}


# ---------------------------------------------------------------------------
# Synthesis (the LLM pass)
# ---------------------------------------------------------------------------

_PROMPT = """You are Noto, your organization's knowledge assistant, reviewing \
feedback that users gave about YOUR OWN outputs (documents, reports, \
answers). Your job: derive durable lessons — and, just as \
important, recognize what does NOT generalize.

Classify and group the feedback items below into lessons. For each lesson:
- "lesson_text": one imperative sentence, written to be injected into your \
own future prompts (e.g. "Never include internal notes in externally shared \
documents."). For non-actionable scopes, a short description of what the \
feedback was instead.
- "scope": one of
    "global"      — applies to everything Noto does, clearly a house rule
    "workflow"    — applies to one workflow (set "workflow" to its tag)
    "engineering" — needs code/feature work, not a prompt rule
    "candidate_specific" — a one-off about a specific person/doc; no \
general rule should be derived
    "insufficient_evidence" — might generalize, but one ambiguous data \
point isn't enough; wait for more{pp_scope}
- "reasoning": 2-4 sentences explaining HOW you reached this conclusion — \
what the feedback items have in common, who said them (weigh multiple \
people saying the same thing heavily), and why this scope. An operator \
will read this to decide whether to trust you.
- "supporting_feedback_ids": the ids of EVERY feedback item that fed this \
lesson. Every input id must appear in exactly one lesson (group one-offs \
about the same theme together when classifying them candidate_specific).
- "confidence": 0.0-1.0.
- "supersedes_lesson_ids": ids from EXISTING LESSONS below that this lesson \
replaces/strengthens (usually deferred ones now backed by more evidence); \
else [].

Do NOT invent rules beyond what the feedback supports. Prefer \
insufficient_evidence over a shaky global rule. Multiple people \
independently saying the same thing is the strongest signal.

PREFER DURABLE LESSONS. Feedback tied to a specific project, deadline, \
or this month's circumstances is not a lasting rule — projects come \
and go. If the feedback carries a lesson that outlives the moment, \
phrase the lesson in its durable form; if it's purely time-bound, \
classify it candidate_specific or insufficient_evidence and say why in \
the reasoning.

Workflow tags you may use: general, doc_edit, q_and_a.

EXISTING LESSONS (do not duplicate; supersede deferred ones if new \
evidence strengthens them):
{existing}

FEEDBACK ITEMS TO SYNTHESIZE:
{items}

Reply with ONLY a JSON object: {{"lessons": [ ... ]}}
"""

# F1: extra scope offered to the synthesis LLM only when
# h2.personal_preferences_enabled is on (flag off -> prompt is
# byte-identical to pre-F1). The LLM only PROPOSES this scope; a code
# gate (same single person, >=2 separate days) decides whether it
# auto-applies to that user's private memory or parks.
_PP_SCOPE_TEXT = """
    "personal_preference" — ONE person's OWN stylistic preference about \
how NOTO should work with THEM ("when I ask, I want…", "for my drafts \
do…"), where nobody else gave similar feedback and it is clearly not a \
house rule. Never use this scope if two different people said it — \
that's evidence of a global/workflow rule instead."""


def _pp_enabled() -> bool:
    from config import load_config
    return bool((load_config().get("h2") or {})
                .get("personal_preferences_enabled", False))


def synthesize(verbose: bool = True, limit: int = 20,
               timeout: int = 420) -> Dict[str, Any]:
    """Run one synthesis pass over unconsumed feedback. Idempotent-ish:
    consumed feedback (linked to any lesson) is never re-fed."""
    items = unsynthesized_feedback(limit=limit)
    if not items:
        return {"ok": True, "created": 0,
                "message": "no unsynthesized feedback"}

    existing = list_lessons(status=None, limit=100)
    existing_brief = [
        {"id": e["id"], "status": e["status"], "scope": e["scope"],
         "workflow": e["workflow"], "lesson_text": e["lesson_text"][:200]}
        for e in existing if e["status"] in ("pending", "deferred",
                                             "approved")]
    items_brief = [
        {"id": r["id"], "from": r["user_name"] or "unknown",
         "workflow": r["workflow"], "source": r["source"],
         "text": (r["feedback_text"] or "")[:800],
         "context": (r["context_snippet"] or "")[:300],
         "when": r["created_at"]}
        for r in items]

    prompt = _PROMPT.format(
        existing=json.dumps(existing_brief, indent=1) or "[]",
        items=json.dumps(items_brief, indent=1),
        pp_scope=_PP_SCOPE_TEXT if _pp_enabled() else "")

    if verbose:
        print(f"[feedback_synthesis] synthesizing {len(items)} feedback "
              f"item(s) against {len(existing_brief)} existing lesson(s)…",
              flush=True)
    from noto_research import _claude
    raw = _claude(prompt, timeout=timeout)
    lessons = _parse_lessons(raw)
    if lessons is None:
        return {"ok": False, "error": "LLM returned unparseable output",
                "raw_head": (raw or "")[:400]}

    valid_ids = {r["id"] for r in items}
    existing_ids = {e["id"] for e in existing}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    created, deferred, personal_applied, errors = 0, 0, 0, []
    s = _store()
    try:
        for les in lessons:
            scope = str(les.get("scope", ""))
            if scope not in _SCOPES:
                errors.append(f"bad scope {scope!r}"); continue
            text = str(les.get("lesson_text", "")).strip()
            if not text:
                errors.append("empty lesson_text"); continue
            sup = [i for i in (les.get("supporting_feedback_ids") or [])
                   if isinstance(i, int) and i in valid_ids]
            if not sup:
                errors.append(f"lesson without valid evidence: {text[:60]}")
                continue
            supersedes = [i for i in (les.get("supersedes_lesson_ids") or [])
                          if isinstance(i, int) and i in existing_ids]
            # absorb evidence from superseded lessons
            for sid in supersedes:
                row = s.conn.execute(
                    "SELECT supporting_feedback_ids, status FROM "
                    "derived_lessons WHERE id=?", (sid,)).fetchone()
                if row and row["status"] in ("pending", "deferred"):
                    try:
                        sup = sorted(set(sup) | set(
                            json.loads(row["supporting_feedback_ids"])))
                    except Exception:
                        pass
                    s.conn.execute(
                        "UPDATE derived_lessons SET status='superseded', "
                        "reviewed_note=? WHERE id=? AND status IN "
                        "('pending','deferred')",
                        (f"superseded by new synthesis at {now}", sid))
            status = "pending" if scope in _ACTIONABLE else "deferred"
            cur = s.conn.execute(
                "INSERT INTO derived_lessons (lesson_text, scope, workflow, "
                "reasoning, supporting_feedback_ids, superseded_lesson_ids, "
                "confidence, model, synthesized_at, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (text, scope, str(les.get("workflow", "") or ""),
                 str(les.get("reasoning", "")).strip(),
                 json.dumps(sorted(sup)), json.dumps(supersedes),
                 float(les.get("confidence", 0.5) or 0.5),
                 "claude -p (noto_research._claude)", now, status))
            created += 1
            if status == "deferred":
                deferred += 1
            # F1: personal preferences auto-apply to that ONE
            # user's private memory when the code gate passes —
            # the LLM's scope call alone is never trusted.
            if scope == "personal_preference" and _pp_enabled():
                gate = _pp_gate(s.conn, sup)
                if gate["passes"]:
                    slug = _apply_personal_preference(
                        cur.lastrowid, text,
                        str(les.get("workflow", "") or ""), gate)
                    if slug:
                        s.conn.execute(
                            "UPDATE derived_lessons SET status='approved',"
                            " reviewed_at=?, reviewed_by=?, reviewed_note=?"
                            " WHERE id=?",
                            (now, "noto-auto (F1 gate)",
                             f"auto-applied to {gate['user_name'] or gate['open_id']}'s "
                             f"private memory as '{slug}' — same person on "
                             f"{len(gate['days'])} separate days, nobody "
                             f"else similar; DM notice sent",
                             cur.lastrowid))
                        personal_applied += 1
                else:
                    s.conn.execute(
                        "UPDATE derived_lessons SET reviewed_note=? "
                        "WHERE id=?",
                        (f"F1 gate NOT passed ({gate['why']}) — parked "
                         f"for operator review", cur.lastrowid))
        s.conn.commit()
    finally:
        s.close()
    if verbose:
        print(f"[feedback_synthesis] created {created} lesson(s) "
              f"({deferred} auto-deferred, {personal_applied} personal "
              f"pref(s) auto-applied); {len(errors)} skipped",
              flush=True)
    return {"ok": True, "created": created, "deferred": deferred,
            "personal_applied": personal_applied,
            "errors": errors, "consumed_feedback": len(items)}


# ---------------------------------------------------------------------------
# F1 — personal-preference gate + apply
# ---------------------------------------------------------------------------

def _pp_gate(conn, sup_ids: List[int]) -> Dict[str, Any]:
    """Code-enforced gate for auto-applying a personal preference: ALL
    supporting feedback must come from the SAME identified person, on
    at least TWO separate days. (The 'nobody else similar' half is
    structural: any lesson whose evidence spans two people can't reach
    here with a single open_id.) Returns {passes, why, open_id,
    user_name, days}."""
    qmarks = ",".join("?" * len(sup_ids))
    rows = [dict(r) for r in conn.execute(
        f"SELECT user_open_id, user_name, created_at FROM feedback "
        f"WHERE id IN ({qmarks})", sup_ids)]
    oids = {r["user_open_id"] for r in rows if r["user_open_id"]}
    named = all(r["user_open_id"] for r in rows)
    days = sorted({(r["created_at"] or "")[:10] for r in rows
                   if r["created_at"]})
    out = {"open_id": next(iter(oids)) if len(oids) == 1 else "",
           "user_name": (rows[0].get("user_name") or "") if rows else "",
           "days": days}
    if not rows or not named or len(oids) != 1:
        out.update(passes=False,
                   why="evidence not attributable to exactly one person")
    elif len(days) < 2:
        out.update(passes=False,
                   why=f"only {len(days)} distinct day(s) of evidence — "
                       f"need >=2 separate days")
    else:
        out.update(passes=True, why="")
    return out


def _apply_personal_preference(lesson_id: int, lesson_text: str,
                               workflow: str,
                               gate: Dict[str, Any]) -> Optional[str]:
    """Write the preference into that user's PRIVATE (DM-scoped)
    memory and DM them a notice with the undo handle. Returns the fact
    slug, or None if user_memory declined (e.g. confidential-shape
    filter) — caller then leaves the lesson parked."""
    open_id = gate["open_id"]
    try:
        import user_memory
        slug_base = re.sub(r"[^a-z0-9]+", "-",
                           " ".join(lesson_text.lower().split()[:6])
                           ).strip("-")
        name = f"pref-{slug_base}"[:60]
        body = (f"{lesson_text}\n\n(Auto-derived by Noto from your "
                f"feedback on {', '.join(gate['days'])}; lesson "
                f"#{lesson_id}. This only affects how Noto works with "
                f"you.)")
        slug = user_memory.write_fact(
            open_id, {"name": name, "body": body,
                      "description": lesson_text[:140]},
            chat_type="p2p", workflow=workflow or "general",
            source_excerpt=f"derived_lesson:{lesson_id}")
        if not slug:
            return None
    except Exception as e:
        print(f"[feedback_synthesis] pp apply failed for lesson "
              f"{lesson_id}: {str(e)[:150]}", file=sys.stderr, flush=True)
        return None
    try:
        from lark_client import LarkClient
        LarkClient().send_text(
            open_id,
            f"📝 I noticed a preference from your feedback and saved it "
            f"to how I work with you (only you):\n\n“{lesson_text}”\n\n"
            f"If that's wrong, reply /forget {slug} and I'll drop it "
            f"(and won't re-learn it for 30 days).",
            receive_id_type="open_id")
    except Exception as e:
        print(f"[feedback_synthesis] pp DM notice failed ({open_id}): "
              f"{str(e)[:120]}", file=sys.stderr, flush=True)
    return slug


def _parse_lessons(raw: str) -> Optional[List[Dict[str, Any]]]:
    if not raw:
        return None
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    if start > 0:
        text = text[start:]
    try:
        data = json.loads(text)
    except Exception:
        return None
    lessons = data.get("lessons")
    return lessons if isinstance(lessons, list) else None


# ---------------------------------------------------------------------------
# Review actions (panel + CLI)
# ---------------------------------------------------------------------------

def approve_lesson(lesson_id: int, reviewer_open_id: str = "",
                   reviewer_name: str = "",
                   edited_text: str = "",
                   reviewer_note: str = "") -> Dict[str, Any]:
    """Approve a lesson: route by scope, resolve its supporting feedback.
    CAS on status so double-approval can't double-append.

    reviewer_note: the operator's caveat ("yes, but also consider …") —
    stored on the lesson AND appended inline with the rule so the
    drafter inherits the nuance, not just the rule."""
    from feedback_store import (FeedbackStore, append_lesson,
                                append_engineering, lessons_file,
                                engineering_backlog_path)
    from feedback_cluster import _lessons_workflow_for
    s = _store()
    try:
        r = s.conn.execute("SELECT * FROM derived_lessons WHERE id=?",
                           (lesson_id,)).fetchone()
        if not r:
            return {"ok": False, "error": f"lesson #{lesson_id} not found"}
        r = dict(r)
        if r["scope"] not in _ACTIONABLE:
            return {"ok": False,
                    "error": f"scope '{r['scope']}' is not actionable — "
                             f"it exists as an audit record, not a rule"}
        text = (edited_text or r["lesson_text"]).strip()
        note = (reviewer_note or "").strip()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = s.conn.execute(
            "UPDATE derived_lessons SET status='approved', lesson_text=?, "
            "reviewed_at=?, reviewed_by=?, reviewed_note=? "
            "WHERE id=? AND status IN ('pending','deferred')",
            (text, now, reviewer_name or reviewer_open_id,
             note or "approved", lesson_id))
        s.conn.commit()
        if cur.rowcount != 1:
            return {"ok": False, "error": "already reviewed"}
        sup = json.loads(r["supporting_feedback_ids"] or "[]")
    finally:
        s.close()

    routes: List[Dict[str, str]] = []
    ctx = f"derived lesson #{lesson_id}"
    if note:
        ctx += f"; operator: {note}"
    if r["scope"] == "engineering":
        append_engineering(text, from_user=reviewer_name or "(lesson)",
                           workflow=r["workflow"] or "general",
                           accept_note=ctx)
        routes.append({"to": "engineering",
                       "path": engineering_backlog_path()})
    else:
        wf = "general" if r["scope"] == "global" \
            else _lessons_workflow_for(r["workflow"] or "general")
        line = append_lesson(wf, text, context_note=ctx,
                             from_user=reviewer_name or "(lesson-review)")
        routes.append({"to": "lessons", "path": lessons_file(wf),
                       "line": line})

    resolved = _resolve_supporting(
        sup, "accepted", f"folded into approved lesson #{lesson_id}")
    return {"ok": True, "lesson_id": lesson_id, "routes": routes,
            "feedback_resolved": resolved}


def reject_lesson(lesson_id: int, reviewer_open_id: str = "",
                  reviewer_name: str = "",
                  reason: str = "") -> Dict[str, Any]:
    """Reject a lesson; its supporting feedback resolves as rejected so
    the queue drains through the lesson decision."""
    s = _store()
    try:
        r = s.conn.execute(
            "SELECT supporting_feedback_ids FROM derived_lessons WHERE id=?",
            (lesson_id,)).fetchone()
        if not r:
            return {"ok": False, "error": f"lesson #{lesson_id} not found"}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = s.conn.execute(
            "UPDATE derived_lessons SET status='rejected', reviewed_at=?, "
            "reviewed_by=?, reviewed_note=? "
            "WHERE id=? AND status IN ('pending','deferred')",
            (now, reviewer_name or reviewer_open_id,
             reason or "rejected", lesson_id))
        s.conn.commit()
        if cur.rowcount != 1:
            return {"ok": False, "error": "already reviewed"}
        sup = json.loads(r["supporting_feedback_ids"] or "[]")
    finally:
        s.close()
    resolved = _resolve_supporting(
        sup, "rejected", f"lesson #{lesson_id} rejected"
                         + (f": {reason}" if reason else ""))
    return {"ok": True, "lesson_id": lesson_id,
            "feedback_resolved": resolved}


def _resolve_supporting(ids: List[int], status: str, note: str) -> int:
    from feedback_store import FeedbackStore
    s = FeedbackStore()
    n = 0
    try:
        for fid in ids:
            try:
                if s.resolve(int(fid), status, note):
                    n += 1
            except Exception:
                pass
    finally:
        s.close()
    return n


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 0
    cmd = argv[0]
    if cmd == "synthesize":
        print(json.dumps(synthesize(verbose=True), indent=2))
        return 0
    if cmd == "list":
        for r in list_lessons(status=argv[1] if len(argv) > 1 else None):
            print(f"#{r['id']} [{r['status']}] {r['scope']}"
                  f"{'/' + r['workflow'] if r['workflow'] else ''} "
                  f"conf={r['confidence']:.2f} "
                  f"({len(r['evidence'])} evidence)\n"
                  f"   {r['lesson_text']}\n"
                  f"   ∵ {r['reasoning'][:200]}\n")
        return 0
    if cmd == "stats":
        print(json.dumps(stats(), indent=2))
        return 0
    if cmd == "approve" and len(argv) >= 2:
        print(json.dumps(approve_lesson(int(argv[1]),
                                        reviewer_name="(cli)"), indent=2))
        return 0
    if cmd == "reject" and len(argv) >= 2:
        print(json.dumps(reject_lesson(
            int(argv[1]), reviewer_name="(cli)",
            reason=" ".join(argv[2:])), indent=2))
        return 0
    print("commands: synthesize | list [status] | stats | approve <id> | "
          "reject <id> [reason]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
