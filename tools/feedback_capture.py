"""
Feedback loop — Phase A: capture layer.

Every operator/admin edit becomes a feedback_event row, regardless of
how the edit happened:

    bot_edit       a doc-editing skill (e.g. edit_doc) applied an
                   edit — capture happens inside each skill
    nightly_diff   nightly walk of all registered artifacts; live doc
                   content diffed against last-known stored content
                   (operator edited the doc directly in Lark, without
                   going through the bot)
    feedback_cmd   /feedback <text> slash command — explicit teaching
    feedback_nl    agent detected NL feedback intent ("feedback: ...",
                   "remember this: ...") — explicit teaching

Phase B (analyze) reads where status='new'; Phase C (cluster +
recommend) groups by pattern_signature; Phase D (admin review) surfaces
recommended rules in the Management chat / super-admin DM.

See docs/feedback-loop.md for the full spec.
"""

import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Source vocabulary — keep in sync with the DB CHECK semantics.
SOURCE_BOT_EDIT     = "bot_edit"
SOURCE_NIGHTLY_DIFF = "nightly_diff"
SOURCE_FEEDBACK_CMD = "feedback_cmd"
SOURCE_FEEDBACK_NL  = "feedback_nl"

# Workflow tags — one per skill / artifact type.
WORKFLOW_GENERAL          = "general"
WORKFLOW_DOC_EDIT         = "doc_edit"
WORKFLOW_Q_AND_A          = "q_and_a"
WORKFLOW_PRODUCT          = "product"   # /feedback about bot behavior, not a skill output

# Capture threshold — don't log a "diff event" for trivial changes
# (timestamps, single-char whitespace tweaks). Phase A v1: 30 chars.
MIN_DIFF_CHARS = 30


# ---------------------------------------------------------------------------
# Admin tier resolver — reads memory/operators.yaml
# ---------------------------------------------------------------------------

_OPERATORS_CACHE: Dict[str, Any] = {"at": 0.0, "by_id": {}}
_OPERATORS_TTL = 60.0


def _load_operators_by_id() -> Dict[str, Dict[str, Any]]:
    """Returns {open_id: {name, role, admin, super_admin, authoritative,
    precedential}} from operators.yaml. Cached briefly so live edits to
    the file take effect within a minute."""
    now = time.time()
    if now - _OPERATORS_CACHE["at"] < _OPERATORS_TTL and _OPERATORS_CACHE["by_id"]:
        return _OPERATORS_CACHE["by_id"]
    try:
        import yaml
        from config import get_home
        with open(os.path.join(get_home(), "memory", "operators.yaml")) as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[feedback_capture] operators.yaml read failed: {e}",
              file=sys.stderr, flush=True)
        data = {}
    by_id: Dict[str, Dict[str, Any]] = {}
    for slug, info in data.items():
        if not isinstance(info, dict):
            continue
        oid = info.get("open_id", "")
        if not oid:
            continue
        by_id[oid] = {
            "slug":          slug,
            "name":          info.get("name", ""),
            "role":          info.get("role", ""),
            "admin":         bool(info.get("admin", False)),
            "super_admin":   bool(info.get("super_admin", False)),
            "authoritative": bool(info.get("authoritative", False)),
            "precedential":  bool(info.get("precedential", False)),
        }
    _OPERATORS_CACHE["at"] = now
    _OPERATORS_CACHE["by_id"] = by_id
    return by_id


def authority_for(open_id: str) -> str:
    """One of: 'super_admin' | 'admin' | 'authoritative' | 'standard'.
    Returns 'standard' for unknown senders. Used by capture + (later)
    cluster-and-recommend to weight events."""
    if not open_id:
        return "standard"
    rec = _load_operators_by_id().get(open_id)
    if not rec:
        return "standard"
    if rec.get("super_admin"):
        return "super_admin"
    if rec.get("admin"):
        return "admin"
    if rec.get("authoritative"):
        return "authoritative"
    return "standard"


def name_for(open_id: str) -> str:
    """Resolved display name for a known operator; '' otherwise."""
    rec = _load_operators_by_id().get(open_id or "")
    return rec.get("name", "") if rec else ""


def is_admin(open_id: str) -> bool:
    rec = _load_operators_by_id().get(open_id or "")
    return bool(rec and (rec.get("admin") or rec.get("super_admin")))


def is_super_admin(open_id: str) -> bool:
    rec = _load_operators_by_id().get(open_id or "")
    return bool(rec and rec.get("super_admin"))


def admin_ids() -> List[str]:
    """All admin open_ids (includes super-admins)."""
    return [oid for oid, rec in _load_operators_by_id().items()
            if rec.get("admin") or rec.get("super_admin")]


# ---------------------------------------------------------------------------
# Event writer — single entry point for all capture sources
# ---------------------------------------------------------------------------

def _bot_open_id() -> str:
    """The Noto service-account open_id from operators.yaml."""
    for oid, rec in _load_operators_by_id().items():
        if (rec.get("role") or "") == "service_account":
            return oid
    return ""


def capture_event(
        workflow: str,
        source: str,
        recruiter_open_id: str = "",
        doc_id: str = "",
        doc_url: str = "",
        candidate: str = "",
        chat_id: str = "",
        before_md: str = "",
        after_md: str = "",
        instruction: str = "") -> Optional[int]:
    """Write one feedback_event. Returns the row id, or None if the
    diff was below the noise floor and we skipped it. Best-effort: any
    failure is logged but never raised (capture must never break a
    user-facing flow)."""
    try:
        # Drop bot-own edits (template re-renders, doc updates the bot
        # itself made) — those aren't user feedback signal. Only
        # applies to direct-Lark diffs where attribution came from the
        # doc's latest_modify_user.
        if (source == SOURCE_NIGHTLY_DIFF and recruiter_open_id
                and recruiter_open_id == _bot_open_id()):
            return None
        # Noise filter — only for diff-shaped sources where the
        # before/after delta is meaningful. /feedback commands always
        # capture regardless of length.
        if source in (SOURCE_BOT_EDIT, SOURCE_NIGHTLY_DIFF):
            delta = abs(len(after_md or "") - len(before_md or ""))
            if delta < MIN_DIFF_CHARS and (before_md or "") == (after_md or ""):
                return None
        authority = authority_for(recruiter_open_id)
        name = name_for(recruiter_open_id)
        from feedback_store import FeedbackStore
        store = FeedbackStore()
        try:
            cur = store.conn.execute(
                "INSERT INTO feedback_events (workflow, source, doc_id, "
                "doc_url, candidate, chat_id, recruiter_open_id, "
                "recruiter_name, authority, before_md, after_md, "
                "instruction, captured_at, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (workflow, source, doc_id, doc_url, candidate,
                 chat_id, recruiter_open_id, name, authority,
                 before_md, after_md, instruction,
                 datetime.utcnow().isoformat(timespec="seconds"),
                 "new"))
            store.conn.commit()
            event_id = cur.lastrowid
        finally:
            store.close()
        print(f"[feedback_capture] event #{event_id} workflow={workflow!r} "
              f"source={source!r} authority={authority!r} "
              f"by={name or recruiter_open_id!r}",
              file=sys.stderr, flush=True)
        return event_id
    except Exception as e:
        print(f"[feedback_capture] capture failed: {e}",
              file=sys.stderr, flush=True)
        return None


# ---------------------------------------------------------------------------
# Nightly direct-edit diff scanner
# ---------------------------------------------------------------------------

def nightly_scan(verbose: bool = True) -> Dict[str, int]:
    """Diff every registered deliverable doc against its last-known
    stored content and capture a nightly_diff event for any meaningful
    change (someone edited the doc directly in Lark, without going
    through the bot).

    The upstream deployment scanned two domain-specific artifact
    registries here; those modules aren't part of the open release, so
    this ships as an extension point. To wire it up: iterate your own
    registry of {doc_id, doc_url, stored_md} rows, fetch the live doc
    (lark_client.get_docx_blocks + lark_sync.render_blocks_markdown),
    gate with _meaningfully_changed(stored, live), attribute the edit
    via the doc's latest_modify_user (falling back to your registry's
    last-updated-by), then call
        capture_event(workflow=WORKFLOW_DOC_EDIT,
                      source=SOURCE_NIGHTLY_DIFF, ...)
    and advance your stored baseline so the same delta doesn't re-fire
    the next night. Best-effort per-doc: a single fetch failure is
    logged but shouldn't stop the scan. Returns counts."""
    scanned = captured = errors = 0
    if verbose:
        print(f"[feedback_capture] nightly_scan — scanned={scanned} "
              f"captured={captured} errors={errors} (no doc registry "
              f"wired; see nightly_scan docstring)", flush=True)
    return {"scanned": scanned, "captured": captured, "errors": errors}


def _meaningfully_changed(stored: str, live: str) -> bool:
    """Cheap noise filter: ignore whitespace-only / very-small diffs.
    A real diff has at least MIN_DIFF_CHARS delta in length OR clear
    content change. Used to avoid re-firing on bot-rewritten headers."""
    s = (stored or "").strip()
    l = (live or "").strip()
    if s == l:
        return False
    if abs(len(s) - len(l)) >= MIN_DIFF_CHARS:
        return True
    # same length-ish but content differs — only fire if a meaningful
    # token actually changed (cheap heuristic: 30+ chars of new text)
    return len(set(l.split()) - set(s.split())) >= 6


# ---------------------------------------------------------------------------
# CLI for ops
# ---------------------------------------------------------------------------

def _cmd_admins() -> int:
    print("Admins (from memory/operators.yaml):")
    for oid, rec in _load_operators_by_id().items():
        flags = []
        if rec.get("super_admin"):   flags.append("super_admin")
        if rec.get("admin"):         flags.append("admin")
        if rec.get("authoritative"): flags.append("authoritative")
        if rec.get("precedential"):  flags.append("precedential")
        if flags:
            print(f"  {oid}  {rec.get('name')!r}  → {', '.join(flags)}")
    return 0


def _cmd_events(limit: int = 20) -> int:
    from feedback_store import FeedbackStore
    s = FeedbackStore()
    try:
        rows = s.conn.execute(
            "SELECT id, captured_at, workflow, source, authority, "
            "recruiter_name, doc_id, candidate, status FROM "
            "feedback_events ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    finally:
        s.close()
    print(f"Last {len(rows)} feedback_events:")
    for r in rows:
        print(f"  #{r['id']} {r['captured_at']} {r['workflow']}/"
              f"{r['source']} by={r['recruiter_name'] or '?'} "
              f"({r['authority']}) cand={r['candidate'] or '-'} "
              f"status={r['status']}")
    return 0


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 0
    cmd = argv[0]
    if cmd == "admins":
        return _cmd_admins()
    if cmd == "events":
        n = int(argv[1]) if len(argv) > 1 else 20
        return _cmd_events(n)
    if cmd == "nightly":
        nightly_scan(verbose=True)
        return 0
    print("commands: admins | events [N] | nightly", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
