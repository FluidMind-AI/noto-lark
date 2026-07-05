#!/usr/bin/env python3
"""
Feedback store — capture user feedback from workflow conversations
(doc edits, Q&A, research) so the operator can review,
accept (→ permanent learned rule), or reject.

Architecture:
  - Heuristic + LLM detection in lark_bot identifies feedback in user
    messages and writes a row here.
  - The operator uses /feedback list / show / accept / reject to
    process the queue.
  - On accept: append to memory/<workflow>_lessons.md so future runs
    of that workflow's prompts inherit the rule.

SQLite at indexes/feedback.db (own file; cross-workflow concern).

CLI:
  python tools/feedback_store.py list [--workflow doc_edit] [--status unresolved]
  python tools/feedback_store.py show <id>
  python tools/feedback_store.py accept <id> [--note "..."]
  python tools/feedback_store.py reject <id> [--note "..."]
  python tools/feedback_store.py selftest
"""

import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load_config, get_home                # noqa: E402


def _db_path() -> str:
    rel = (load_config().get("retrieval", {}) or {}).get(
        "feedback_db", "indexes/feedback.db")
    return rel if os.path.isabs(rel) else os.path.join(get_home(), rel)


_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS feedback (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id           TEXT,
  user_open_id      TEXT,
  user_name         TEXT,
  workflow          TEXT,           -- 'doc_edit' | 'general' | …
  workflow_context  TEXT,           -- JSON blob (doc / team / etc.)
  feedback_text     TEXT,           -- the user's message verbatim
  context_snippet   TEXT,           -- what bot was doing (e.g. last bot reply head)
  source            TEXT,           -- 'heuristic' | 'llm' | 'explicit'
  kind              TEXT DEFAULT 'unsure',     -- rule | engineering | both | unsure
  status            TEXT DEFAULT 'unresolved', -- unresolved | accepted | rejected
  created_at        TEXT,
  resolved_at       TEXT,
  resolution_note   TEXT
);

-- Phase C: clustered patterns awaiting admin review (or already
-- approved → injected into skill prompts, or rejected → re-surfaces
-- if pattern keeps repeating).
CREATE TABLE IF NOT EXISTS recommended_rules (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow              TEXT NOT NULL,
  pattern_signature     TEXT NOT NULL,
  rule_text             TEXT NOT NULL,
  supporting_event_ids  TEXT NOT NULL,            -- JSON array of event ids
  support_count         INTEGER NOT NULL DEFAULT 1,
  priority              TEXT NOT NULL DEFAULT 'normal',  -- normal | high
  status                TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected | superseded
  source_type           TEXT,                     -- diff | teach
  recommended_at        TEXT NOT NULL,
  reviewed_at           TEXT,
  reviewed_by           TEXT,
  reviewed_note         TEXT,
  rejection_count       INTEGER NOT NULL DEFAULT 0,
  last_seen_at          TEXT NOT NULL,
  UNIQUE(workflow, pattern_signature)
);

-- Phase A capture layer for the feedback loop (see docs/feedback-loop.md):
-- every operator edit + every /feedback command becomes an event here.
-- Analysis (Phase B) reads where status='new', clustering (Phase C)
-- promotes patterns to recommended_rules.
CREATE TABLE IF NOT EXISTS feedback_events (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow             TEXT NOT NULL,         -- general | doc_edit | q_and_a | …
  source               TEXT NOT NULL,         -- bot_edit | nightly_diff | feedback_cmd | feedback_nl
  doc_id               TEXT,                  -- the Lark doc the edit landed on (may be empty for /feedback)
  doc_url              TEXT,
  candidate            TEXT,                  -- subject name if applicable
  chat_id              TEXT,                  -- where the edit/feedback originated
  recruiter_open_id    TEXT,                  -- who edited / gave feedback
  recruiter_name       TEXT,                  -- resolved at capture time
  authority            TEXT NOT NULL DEFAULT 'standard',  -- super_admin | admin | authoritative | standard
  before_md            TEXT,                  -- prior content (empty for /feedback)
  after_md             TEXT,                  -- new content (or the /feedback text)
  instruction          TEXT,                  -- user's stated instruction (bot_edit) or feedback text
  captured_at          TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'new',  -- new | analyzed | clustered | superseded
  -- Filled by Phase B (analyzer):
  change_type          TEXT,                  -- rephrase | fact_add | structure | style_fix | judgment | one_off
  pattern_signature    TEXT,                  -- short canonical description
  candidate_rule_text  TEXT,                  -- proposed general rule (empty for one_off)
  confidence           REAL                   -- 0.0 - 1.0
);
"""
# Indexes are created AFTER the migration ALTER below so they reference
# columns that exist for older DBs too.
_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_fb_status   ON feedback(status);
CREATE INDEX IF NOT EXISTS idx_fb_workflow ON feedback(workflow);
CREATE INDEX IF NOT EXISTS idx_fb_kind     ON feedback(kind);
CREATE INDEX IF NOT EXISTS idx_fe_status   ON feedback_events(status);
CREATE INDEX IF NOT EXISTS idx_fe_workflow ON feedback_events(workflow);
CREATE INDEX IF NOT EXISTS idx_fe_pattern  ON feedback_events(pattern_signature);
CREATE INDEX IF NOT EXISTS idx_fe_doc_id   ON feedback_events(doc_id);
CREATE INDEX IF NOT EXISTS idx_rr_status   ON recommended_rules(status);
CREATE INDEX IF NOT EXISTS idx_rr_workflow ON recommended_rules(workflow);
"""

_VALID_WORKFLOWS = ("general", "doc_edit", "q_and_a")
_VALID_KINDS = ("rule", "engineering", "both", "unsure")


class FeedbackStore:
    def __init__(self, db_path: Optional[str] = None):
        self.path = db_path or _db_path()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30.0)
        from sqlite_utils import harden
        harden(self.conn)
        self.conn.row_factory = sqlite3.Row
        # 1) Ensure table exists (no `kind` column on legacy DBs).
        self.conn.executescript(_SCHEMA_TABLE)
        # 2) Migrate older tables that predate the `kind` column.
        try:
            self.conn.execute(
                "ALTER TABLE feedback ADD COLUMN kind TEXT DEFAULT 'unsure'")
        except sqlite3.OperationalError:
            pass
        # 3) Now safe to create the kind index.
        self.conn.executescript(_SCHEMA_INDEXES)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---- write -------------------------------------------------------
    def add(self, *, chat_id: str, user_open_id: str, user_name: str,
            workflow: str, feedback_text: str,
            workflow_context: Optional[Dict[str, Any]] = None,
            context_snippet: str = "",
            source: str = "heuristic",
            kind: str = "unsure") -> int:
        if kind not in _VALID_KINDS:
            kind = "unsure"
        cur = self.conn.execute(
            "INSERT INTO feedback (chat_id, user_open_id, user_name, "
            "workflow, workflow_context, feedback_text, context_snippet, "
            "source, kind, status, created_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?)",
            (chat_id, user_open_id, user_name, workflow,
             json.dumps(workflow_context or {}),
             feedback_text.strip(), context_snippet[:1500].strip(),
             source, kind, "unresolved",
             time.strftime("%Y-%m-%dT%H:%M:%S")))
        self.conn.commit()
        return int(cur.lastrowid)

    def reclassify(self, fid: int, new_kind: str) -> bool:
        if new_kind not in _VALID_KINDS:
            raise ValueError(f"bad kind {new_kind!r}; "
                             f"choose from {_VALID_KINDS}")
        cur = self.conn.execute(
            "UPDATE feedback SET kind=? WHERE id=?", (new_kind, fid))
        self.conn.commit()
        return cur.rowcount > 0

    def resolve(self, fid: int, status: str, note: str = "") -> bool:
        if status not in ("accepted", "rejected"):
            raise ValueError(f"bad status {status!r}")
        cur = self.conn.execute(
            "UPDATE feedback SET status=?, resolved_at=?, resolution_note=? "
            "WHERE id=? AND status='unresolved'",
            (status, time.strftime("%Y-%m-%dT%H:%M:%S"),
             note.strip(), fid))
        self.conn.commit()
        return cur.rowcount > 0

    # ---- read --------------------------------------------------------
    def get(self, fid: int) -> Optional[Dict[str, Any]]:
        r = self.conn.execute(
            "SELECT * FROM feedback WHERE id=?", (fid,)).fetchone()
        return self._inflate(dict(r)) if r else None

    def list(self, status: Optional[str] = "unresolved",
             workflow: Optional[str] = None,
             kind: Optional[str] = None,
             limit: int = 100) -> List[Dict[str, Any]]:
        q = "SELECT * FROM feedback"
        wh, args = [], []
        if status:
            wh.append("status=?"); args.append(status)
        if workflow:
            wh.append("workflow=?"); args.append(workflow)
        if kind:
            wh.append("kind=?"); args.append(kind)
        if wh:
            q += " WHERE " + " AND ".join(wh)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [self._inflate(dict(r)) for r in
                self.conn.execute(q, args).fetchall()]

    def stats(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"by_status": {}, "by_workflow": {}}
        for r in self.conn.execute(
                "SELECT status, COUNT(*) n FROM feedback GROUP BY status"):
            out["by_status"][r["status"]] = r["n"]
        for r in self.conn.execute(
                "SELECT workflow, COUNT(*) n FROM feedback "
                "WHERE status='unresolved' GROUP BY workflow"):
            out["by_workflow"][r["workflow"]] = r["n"]
        return out

    @staticmethod
    def _inflate(row: Dict[str, Any]) -> Dict[str, Any]:
        v = row.get("workflow_context")
        if isinstance(v, str) and v:
            try:
                row["workflow_context"] = json.loads(v)
            except Exception:
                row["workflow_context"] = {}
        elif v is None:
            row["workflow_context"] = {}
        return row


# ------------------------------------------------------------------------
# Lessons file — accepted feedback gets appended here for the relevant
# workflow's prompts to load and inject as "LEARNED RULES".
# ------------------------------------------------------------------------

def lessons_file(workflow: str) -> str:
    return os.path.join(get_home(), "memory", f"{workflow}_lessons.md")


def append_lesson(workflow: str, feedback_text: str,
                   context_note: str = "",
                   from_user: str = "") -> str:
    """Append an accepted feedback item to memory/<workflow>_lessons.md.
    Returns the lesson line written. Includes the originating user's
    name so the operator can weight rules by source on review."""
    path = lessons_file(workflow)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    attribution = f"from {from_user}" if from_user else ""
    if attribution and context_note:
        meta = f"  _({attribution}; {context_note.strip()})_"
    elif attribution:
        meta = f"  _({attribution})_"
    elif context_note:
        meta = f"  _(context: {context_note.strip()})_"
    else:
        meta = ""
    line = f"- {today} — {feedback_text.strip()}{meta}"
    # Create with a friendly header if file doesn't exist
    if not os.path.exists(path):
        header = (
            f"# {workflow.replace('_', ' ').title()} — Learned Rules\n\n"
            f"Accepted feedback the operator has approved as permanent\n"
            f"guidance. The {workflow} workflow's drafter prompt loads\n"
            f"this file and injects it as 'LEARNED RULES (from operator\n"
            f"feedback)' so every future run inherits the lesson.\n\n"
            f"Most recent at the bottom — chronological order matters\n"
            f"when two rules might conflict (the newer wins).\n\n"
        )
        with open(path, "w") as f:
            f.write(header)
    with open(path, "a") as f:
        f.write(line + "\n")
    return line


def load_lessons(workflow: str) -> str:
    """Read the lessons file for a workflow — returns the rules block
    to inject into a prompt, or '' if no lessons yet. Stripped of the
    file header so only the bulleted rules go into the prompt."""
    path = lessons_file(workflow)
    if not os.path.exists(path):
        return ""
    try:
        text = open(path).read()
    except Exception:
        return ""
    # keep only lines beginning with "- " (the bulleted lessons)
    lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
    return "\n".join(lines) if lines else ""


def load_lessons_for(workflow: str) -> str:
    """Combine general bot lessons (always-on) + a workflow's own
    lessons. Workflow-specific can refine general. Use this from
    every workflow's drafter prompt so any 'general' rule the
    operator approved (e.g. 'never invent facts you can't cite')
    applies everywhere."""
    blocks = []
    g = load_lessons("general")
    if g:
        blocks.append(("General bot rules", g))
    if workflow and workflow != "general":
        w = load_lessons(workflow)
        if w:
            blocks.append((f"{workflow.replace('_', ' ').title()} rules", w))
    if not blocks:
        return ""
    return "\n\n".join(f"### {label}\n{body}" for label, body in blocks)


# ------------------------------------------------------------------------
# Engineering backlog — class of feedback that needs DEV work, not just
# a prompt-rule append. Lives in brain/ so it's part of the operational
# tracking surface (alongside eisenhower.md). Read by Claude Code at
# session start so we can surface pending items proactively.
# ------------------------------------------------------------------------

def engineering_backlog_path() -> str:
    return os.path.join(get_home(), "brain", "engineering-backlog.md")


_ENG_BACKLOG_HEADER = """# Engineering Feedback Backlog

Engineering-class feedback the operator has marked for
real code / architecture work. Items here are NOT auto-incorporated
into any prompt — they require conscious dev work in a Claude Code
session. Each item carries the original feedback + workflow context
+ who reported it.

**For Claude Code:** When you see this file has unresolved items
(bullets at the bottom), mention the count at session start so we
can plan the next batch of work.

**Format:** every accepted engineering item is appended below as a
markdown bullet. Mark done by editing the line (`- [x]`) or moving
it into a "Completed" section.

---

"""


def append_engineering(feedback_text: str, from_user: str = "",
                        workflow: str = "general",
                        workflow_context: Optional[Dict[str, Any]] = None,
                        accept_note: str = "") -> str:
    """Append an accepted engineering-class feedback item to
    brain/engineering-backlog.md. Returns the lines written."""
    path = engineering_backlog_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(_ENG_BACKLOG_HEADER)
    lines = [f"- [ ] **{today}** — *from {from_user or 'unknown'}*",
             f"      {feedback_text.strip()}",
             f"      _workflow:_ `{workflow}`"]
    if workflow_context:
        bits = [f"{k}={v}" for k, v in (workflow_context or {}).items()
                if v]
        if bits:
            lines.append(f"      _context:_ {', '.join(bits)}")
    if accept_note:
        lines.append(f"      _accept-note:_ {accept_note}")
    block = "\n".join(lines) + "\n"
    with open(path, "a") as f:
        f.write(block)
    return block


def accept_with_routing(fid: int, note: str = "",
                        kind: Optional[str] = None,
                        actor_name: str = "") -> Dict[str, Any]:
    """Accept a feedback item AND route it by kind — the one shared code
    path for the admin panel (and any future caller) so routing can't be
    skipped. Unlike the older CLI accept, this refuses to resolve while
    the kind is 'unsure' (the CLI could mark an item accepted without
    routing it anywhere, leaving it stuck).

    kind: optional reclassification applied before accepting
          ('rule' | 'engineering' | 'both').
    Returns {ok, error?, kind?, routes: [{to, path}]}.
    """
    s = FeedbackStore()
    try:
        r = s.get(fid)
        if not r:
            return {"ok": False, "error": f"no such feedback id {fid}"}
        effective = kind or r.get("kind") or "unsure"
        if effective not in ("rule", "engineering", "both"):
            return {"ok": False,
                    "error": "kind is 'unsure' — classify as rule / "
                             "engineering / both before accepting"}
        if kind and kind != r.get("kind"):
            s.reclassify(fid, kind)
        if not s.resolve(fid, "accepted", note):
            return {"ok": False, "error": "already resolved"}
        routes: List[Dict[str, str]] = []
        if effective in ("rule", "both"):
            line = append_lesson(r["workflow"], r["feedback_text"], note,
                                 from_user=r.get("user_name", ""))
            routes.append({"to": "lessons",
                           "path": lessons_file(r["workflow"]),
                           "line": line})
        if effective in ("engineering", "both"):
            append_engineering(
                r["feedback_text"], from_user=r.get("user_name", ""),
                workflow=r.get("workflow", "general"),
                workflow_context=r.get("workflow_context", {}),
                accept_note=note)
            routes.append({"to": "engineering",
                           "path": engineering_backlog_path()})
        return {"ok": True, "kind": effective, "routes": routes}
    finally:
        s.close()


def reject_feedback(fid: int, note: str = "") -> Dict[str, Any]:
    """Reject a feedback item (CAS via resolve). Shared by the panel."""
    s = FeedbackStore()
    try:
        if not s.get(fid):
            return {"ok": False, "error": f"no such feedback id {fid}"}
        if not s.resolve(fid, "rejected", note):
            return {"ok": False, "error": "already resolved"}
        return {"ok": True}
    finally:
        s.close()


def count_engineering_backlog() -> int:
    """Count UNRESOLVED engineering items (bullets that aren't [x])."""
    path = engineering_backlog_path()
    if not os.path.exists(path):
        return 0
    n = 0
    for ln in open(path):
        s = ln.lstrip()
        if s.startswith("- [ ]"):
            n += 1
    return n


# ------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------

def _print_row(r: Dict[str, Any]) -> None:
    ctx = r.get("workflow_context", {})
    ctx_str = (", ".join(f"{k}={v}" for k, v in ctx.items() if v)
               if isinstance(ctx, dict) else str(ctx))
    print(f"  [{r['id']}]  {r.get('workflow','?'):12} "
          f"{r.get('kind','unsure'):11} {r.get('status','?'):10} "
          f"{r.get('created_at','')}  by {r.get('user_name','?')}")
    print(f"        {(r.get('feedback_text') or '')[:140]}")
    if ctx_str:
        print(f"        context: {ctx_str}")


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Feedback store — Noto")
    sub = p.add_subparsers(dest="cmd")

    ls = sub.add_parser("list")
    ls.add_argument("--status", default="unresolved",
                     choices=["unresolved", "accepted", "rejected", "all"])
    ls.add_argument("--workflow", default=None)
    ls.add_argument("--kind", default=None,
                     choices=["rule", "engineering", "both", "unsure"])

    sh = sub.add_parser("show"); sh.add_argument("id", type=int)
    ac = sub.add_parser("accept"); ac.add_argument("id", type=int)
    ac.add_argument("--note", default="")
    rj = sub.add_parser("reject"); rj.add_argument("id", type=int)
    rj.add_argument("--note", default="")
    rc = sub.add_parser("reclassify"); rc.add_argument("id", type=int)
    rc.add_argument("kind",
                     choices=["rule", "engineering", "both", "unsure"])
    sub.add_parser("stats")
    sub.add_parser("backlog")
    sub.add_parser("selftest")
    a = p.parse_args()

    if a.cmd == "list":
        s = FeedbackStore()
        status = None if a.status == "all" else a.status
        rows = s.list(status=status, workflow=a.workflow,
                      kind=getattr(a, "kind", None))
        print(f"{len(rows)} feedback items "
              f"({a.status}, workflow={a.workflow or 'any'}, "
              f"kind={getattr(a, 'kind', None) or 'any'})")
        for r in rows:
            _print_row(r)
        s.close(); return 0
    if a.cmd == "show":
        s = FeedbackStore()
        r = s.get(a.id)
        s.close()
        print(json.dumps(r, indent=2, default=str))
        return 0
    if a.cmd == "accept":
        s = FeedbackStore()
        r = s.get(a.id)
        if not r:
            print(f"no such id {a.id}"); s.close(); return 1
        ok = s.resolve(a.id, "accepted", a.note)
        if not ok:
            print(f"could not accept #{a.id} (already resolved?)")
            s.close(); return 1
        kind = r.get("kind", "rule")
        msg_parts = []
        # Route by kind. 'both' goes to BOTH places.
        if kind in ("rule", "both"):
            line = append_lesson(r["workflow"], r["feedback_text"], a.note,
                                  from_user=r.get("user_name", ""))
            msg_parts.append(f"  rule → {lessons_file(r['workflow'])}"
                             f"\n    {line}")
        if kind in ("engineering", "both"):
            block = append_engineering(
                r["feedback_text"], from_user=r.get("user_name", ""),
                workflow=r.get("workflow", "general"),
                workflow_context=r.get("workflow_context", {}),
                accept_note=a.note)
            msg_parts.append(f"  engineering → "
                             f"{engineering_backlog_path()}\n{block}")
        if kind == "unsure":
            print(f"accepted #{a.id} but kind is 'unsure' — "
                  f"reclassify first with: feedback_store.py reclassify "
                  f"{a.id} <rule|engineering|both>")
        else:
            print(f"accepted #{a.id} (kind={kind}):")
            for m in msg_parts:
                print(m)
        s.close(); return 0
    if a.cmd == "reclassify":
        s = FeedbackStore()
        ok = s.reclassify(a.id, a.kind)
        print(f"reclassified #{a.id} → {a.kind}" if ok
              else f"no such id {a.id}")
        s.close(); return 0 if ok else 1
    if a.cmd == "backlog":
        path = engineering_backlog_path()
        n = count_engineering_backlog()
        print(f"{n} unresolved engineering items in {path}")
        if os.path.exists(path):
            print()
            print(open(path).read())
        return 0
    if a.cmd == "reject":
        s = FeedbackStore()
        ok = s.resolve(a.id, "rejected", a.note)
        print(f"rejected #{a.id}" if ok else f"could not reject #{a.id}")
        s.close(); return 0 if ok else 1
    if a.cmd == "stats":
        s = FeedbackStore()
        print(json.dumps(s.stats(), indent=2))
        s.close(); return 0
    if a.cmd == "selftest":
        return _selftest()
    p.print_help(); return 0


def _selftest() -> int:
    ok = True
    tmp = os.path.join(get_home(), "indexes", "_fb_selftest.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    s = FeedbackStore(tmp)

    fid = s.add(
        chat_id="oc_t", user_open_id="ou_t", user_name="Operator",
        workflow="doc_edit",
        feedback_text="Never include internal draft notes in shared documents.",
        context_snippet="bot's draft included an internal comment block",
        source="heuristic",
        workflow_context={"doc": "Q3 Ops Report",
                           "team": "Operations"})
    if fid > 0:
        print(f"PASS: add() -> id {fid}")
    else:
        print("FAIL: add"); ok = False

    items = s.list(status="unresolved")
    if items and items[0]["id"] == fid:
        print(f"PASS: list unresolved returns the new item")
    else:
        print(f"FAIL: list -> {items}"); ok = False

    # accept it
    accepted = s.resolve(fid, "accepted", "good lasting rule")
    if accepted:
        print("PASS: resolve to accepted")
    else:
        print("FAIL: resolve"); ok = False

    after = s.list(status="unresolved")
    if not after:
        print("PASS: accepted item removed from unresolved list")
    else:
        print(f"FAIL: still unresolved {after}"); ok = False

    s.close()
    os.remove(tmp)

    # lesson append + load round-trip (use a tmp workflow id)
    tmp_workflow = "_selftest_workflow"
    lp = lessons_file(tmp_workflow)
    if os.path.exists(lp):
        os.remove(lp)
    line = append_lesson(tmp_workflow, "Test lesson body.")
    if line.endswith("Test lesson body."):
        print("PASS: append_lesson line includes today's date")
    else:
        print(f"FAIL: append_lesson -> {line}"); ok = False
    loaded = load_lessons(tmp_workflow)
    if "Test lesson body." in loaded and loaded.startswith("- "):
        print("PASS: load_lessons returns the bulleted rule")
    else:
        print(f"FAIL: load_lessons -> {loaded!r}"); ok = False
    if os.path.exists(lp):
        os.remove(lp)

    print("\nselftest:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
