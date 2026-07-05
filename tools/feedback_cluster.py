"""
Feedback loop — Phase C: cluster analyzed events into recommended_rules.

Promotion logic (per docs/feedback-loop.md):
  • 2+ events sharing a pattern_signature        → recommended (priority=normal)
  • 1+ event from an authoritative author        → recommended (priority=high)
  • 1+ event from /feedback (slash or NL)        → recommended (priority=high)
  • Rejected rules can RE-SURFACE if the pattern keeps repeating
    (support_count rises past rejection_count + 5)
  • Approved rules don't re-promote — they're already live
  • One-off events (change_type='one_off') are not clustered (factual
    fixes, no general rule)
"""

import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_AUTHORITATIVE_AUTHORITIES = {"super_admin", "admin", "authoritative"}


def cluster_pending(verbose: bool = True) -> Dict[str, int]:
    """Walk all status='analyzed' events with a non-empty
    pattern_signature, group by (workflow, pattern_signature), promote
    per the rules above. Marks each clustered event status='clustered'.
    Resumable: a crash mid-run leaves remaining events as 'analyzed'."""
    from feedback_store import FeedbackStore
    s = FeedbackStore()
    try:
        rows = [dict(r) for r in s.conn.execute(
            "SELECT id, workflow, source, authority, change_type, "
            "pattern_signature, candidate_rule_text, confidence "
            "FROM feedback_events "
            "WHERE status='analyzed' "
            "  AND pattern_signature != '' "
            "  AND change_type != 'one_off' "
            "ORDER BY id ASC").fetchall()]
    finally:
        s.close()

    # one_off events still get marked clustered (so we don't re-process)
    # but never feed a rule.
    s = FeedbackStore()
    try:
        s.conn.execute(
            "UPDATE feedback_events SET status='clustered' "
            "WHERE status='analyzed' "
            "  AND (change_type='one_off' OR pattern_signature='')")
        s.conn.commit()
    finally:
        s.close()

    if verbose:
        print(f"[feedback_cluster] clustering {len(rows)} analyzed "
              f"event(s)…", flush=True)

    # group by (workflow, pattern_signature)
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for r in rows:
        key = (r["workflow"], r["pattern_signature"])
        groups.setdefault(key, []).append(r)

    promoted = updated = skipped = 0
    for (workflow, sig), events in groups.items():
        outcome = _promote_or_update(workflow, sig, events,
                                     verbose=verbose)
        if outcome == "promoted":
            promoted += 1
        elif outcome == "updated":
            updated += 1
        elif outcome == "skipped":
            skipped += 1
        # mark events clustered regardless (even sub-threshold ones
        # stay clustered so we don't re-analyze; their support is
        # already recorded on the recommended_rule row if one exists,
        # or they accumulate via support_count for the next pass)
        _mark_clustered([e["id"] for e in events])

    if verbose:
        print(f"[feedback_cluster] done — promoted={promoted} "
              f"updated={updated} skipped(below-threshold)={skipped}",
              flush=True)
    return {"promoted": promoted, "updated": updated,
            "skipped_below_threshold": skipped}


def _promote_or_update(workflow: str, signature: str,
                       events: List[Dict[str, Any]],
                       verbose: bool) -> str:
    """Returns 'promoted' | 'updated' | 'skipped' | 'noop'."""
    from feedback_store import FeedbackStore

    has_teach = any(e["source"] in ("feedback_cmd", "feedback_nl")
                     for e in events)
    has_authoritative = any(
        (e["authority"] or "") in _AUTHORITATIVE_AUTHORITIES
        for e in events)
    priority = "high" if (has_teach or has_authoritative) else "normal"

    # Best rule_text from this batch — prefer the highest-confidence
    # one from a teach event, else the highest-confidence overall.
    best = sorted(events, key=lambda e: (
        0 if e["source"] in ("feedback_cmd", "feedback_nl") else 1,
        -float(e["confidence"] or 0)))[0]
    rule_text = (best["candidate_rule_text"] or "").strip()
    if not rule_text:
        # Skill-less rule — nothing actionable to inject. Skip.
        return "skipped"

    now = datetime.utcnow().isoformat(timespec="seconds")
    source_type = "teach" if has_teach else "diff"

    s = FeedbackStore()
    try:
        existing = s.conn.execute(
            "SELECT id, support_count, supporting_event_ids, status, "
            "rejection_count, priority, rule_text FROM recommended_rules "
            "WHERE workflow=? AND pattern_signature=?",
            (workflow, signature)).fetchone()

        new_event_ids = [e["id"] for e in events]
        if existing:
            prior_ids = json.loads(existing["supporting_event_ids"] or "[]")
            merged_ids = sorted(set(prior_ids + new_event_ids))
            new_support = len(merged_ids)
            new_priority = priority if priority == "high" \
                else existing["priority"]

            # Re-surface a rejected rule if pattern keeps repeating
            new_status = existing["status"]
            if existing["status"] == "rejected":
                if new_support >= (existing["rejection_count"] + 5):
                    new_status = "pending"   # re-surface
                    if verbose:
                        print(f"  ↻ resurfaced (rejected, count "
                              f"{new_support} ≥ {existing['rejection_count']}+5):"
                              f" {signature!r}", flush=True)
            elif existing["status"] == "approved":
                # Already live; no need to re-promote, just record support
                pass
            elif existing["status"] in ("pending", "superseded"):
                new_status = "pending"   # keep / restore to pending

            s.conn.execute(
                "UPDATE recommended_rules SET supporting_event_ids=?, "
                "support_count=?, priority=?, status=?, last_seen_at=?, "
                "rule_text=COALESCE(NULLIF(?, ''), rule_text) "
                "WHERE id=?",
                (json.dumps(merged_ids), new_support, new_priority,
                 new_status, now, rule_text, existing["id"]))
            s.conn.commit()
            if verbose:
                print(f"  + #{existing['id']} support={new_support} "
                      f"priority={new_priority} status={new_status} "
                      f"{signature!r}", flush=True)
            return "updated"

        # New pattern — apply promotion threshold
        meets_threshold = (priority == "high" or
                           len(new_event_ids) >= 2)
        if not meets_threshold:
            if verbose:
                print(f"  · below-threshold ({len(new_event_ids)}×, "
                      f"non-authoritative): {signature!r}", flush=True)
            return "skipped"

        cur = s.conn.execute(
            "INSERT INTO recommended_rules (workflow, "
            "pattern_signature, rule_text, supporting_event_ids, "
            "support_count, priority, status, source_type, "
            "recommended_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (workflow, signature, rule_text,
             json.dumps(new_event_ids), len(new_event_ids),
             priority, "pending", source_type, now, now))
        s.conn.commit()
        if verbose:
            print(f"  ✓ #{cur.lastrowid} PROMOTED ({priority}, "
                  f"{len(new_event_ids)}× support): {signature!r}",
                  flush=True)
        return "promoted"
    finally:
        s.close()


def _mark_clustered(event_ids: List[int]) -> None:
    if not event_ids:
        return
    from feedback_store import FeedbackStore
    s = FeedbackStore()
    try:
        s.conn.execute(
            f"UPDATE feedback_events SET status='clustered' "
            f"WHERE id IN ({','.join('?'*len(event_ids))})",
            tuple(event_ids))
        s.conn.commit()
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Admin actions on recommended_rules
# ---------------------------------------------------------------------------

def list_pending(workflow: Optional[str] = None,
                 limit: int = 50) -> List[Dict[str, Any]]:
    """Pending (or recently-resurfaced) recommended rules, priority-high
    first. Workflow filter optional."""
    from feedback_store import FeedbackStore
    s = FeedbackStore()
    try:
        if workflow:
            rows = s.conn.execute(
                "SELECT * FROM recommended_rules WHERE status='pending' "
                "AND workflow=? ORDER BY priority='high' DESC, "
                "recommended_at ASC LIMIT ?",
                (workflow, limit)).fetchall()
        else:
            rows = s.conn.execute(
                "SELECT * FROM recommended_rules WHERE status='pending' "
                "ORDER BY priority='high' DESC, recommended_at ASC "
                "LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        s.close()


def approve(rule_id: int, reviewer_open_id: str,
            reviewer_name: str = "",
            edited_text: str = "",
            reviewer_note: str = "") -> Dict[str, Any]:
    """Approve a recommended rule. Appends to the workflow's lessons
    file via append_lesson so the drafter picks it up. Returns the
    updated rule row.

    reviewer_note (optional, backward-compatible): the reviewer's caveat
    — stored on the rule and appended inline with the lesson line."""
    from feedback_store import FeedbackStore, append_lesson
    s = FeedbackStore()
    try:
        r = s.conn.execute(
            "SELECT * FROM recommended_rules WHERE id=?",
            (rule_id,)).fetchone()
        if not r:
            return {"ok": False, "error": f"rule #{rule_id} not found"}
        r = dict(r)
        text = (edited_text or r["rule_text"]).strip()
        if not text:
            return {"ok": False, "error": "empty rule text"}
        note = (reviewer_note or "").strip()
        now = datetime.utcnow().isoformat(timespec="seconds")
        s.conn.execute(
            "UPDATE recommended_rules SET status='approved', "
            "rule_text=?, reviewed_at=?, reviewed_by=?, "
            "reviewed_note=? WHERE id=?",
            (text, now, reviewer_name or reviewer_open_id,
             note or "approved", rule_id))
        s.conn.commit()
    finally:
        s.close()
    # Inject into the workflow's lessons file (the drafter reads it)
    # workflow tag → lessons_file workflow name
    lessons_workflow = _lessons_workflow_for(r["workflow"])
    try:
        line = append_lesson(lessons_workflow, text,
                             f"operator: {note}" if note else "",
                             from_user=reviewer_name
                                       or "(feedback-review)")
    except Exception as e:
        return {"ok": False, "error": f"approved but lesson-file "
                                       f"append failed: {e}"}
    return {"ok": True, "rule_id": rule_id, "text": text,
            "lessons_workflow": lessons_workflow, "appended": line}


def reject(rule_id: int, reviewer_open_id: str,
           reviewer_name: str = "", reason: str = "") -> Dict[str, Any]:
    """Reject a recommended rule. Bumps rejection_count so we know how
    many more repeats are needed to re-surface."""
    from feedback_store import FeedbackStore
    s = FeedbackStore()
    try:
        r = s.conn.execute(
            "SELECT support_count, rejection_count FROM recommended_rules "
            "WHERE id=?", (rule_id,)).fetchone()
        if not r:
            return {"ok": False, "error": f"rule #{rule_id} not found"}
        now = datetime.utcnow().isoformat(timespec="seconds")
        new_rejection_count = max(r["support_count"],
                                  (r["rejection_count"] or 0) + 1)
        s.conn.execute(
            "UPDATE recommended_rules SET status='rejected', "
            "rejection_count=?, reviewed_at=?, reviewed_by=?, "
            "reviewed_note=? WHERE id=?",
            (new_rejection_count, now,
             reviewer_name or reviewer_open_id,
             reason or "rejected", rule_id))
        s.conn.commit()
    finally:
        s.close()
    return {"ok": True, "rule_id": rule_id,
            "rejection_count": new_rejection_count}


def _lessons_workflow_for(rec_workflow: str) -> str:
    """Map a recommended_rules.workflow tag to the lessons-file
    workflow name the drafter loads. The drafter loads
    load_lessons_for('submissions') today — so submission_draft maps
    there."""
    return {
        "submission_draft":  "submissions",
        "target_list_edit":  "target_list",
        "workup_edit":       "workup",
        "firm_fit_edit":     "firm_fit",
        "doc_edit":          "general",
        "product":           "general",
    }.get(rec_workflow, "general")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 0
    cmd = argv[0]
    if cmd == "cluster-pending":
        cluster_pending(verbose=True)
        return 0
    if cmd == "list-pending":
        wf = argv[1] if len(argv) > 1 else None
        rows = list_pending(workflow=wf)
        if not rows:
            print("(no pending recommended_rules)")
            return 0
        for r in rows:
            print(f"#{r['id']} [{r['priority']}] {r['workflow']} "
                  f"(support {r['support_count']}, "
                  f"rejected {r['rejection_count']}×)\n"
                  f"   pattern:  {r['pattern_signature']}\n"
                  f"   rule:     {r['rule_text']}\n")
        return 0
    if cmd == "approve" and len(argv) >= 2:
        res = approve(int(argv[1]), reviewer_open_id="",
                      reviewer_name="(cli)")
        print(json.dumps(res, indent=2))
        return 0
    if cmd == "reject" and len(argv) >= 2:
        reason = " ".join(argv[2:]) if len(argv) > 2 else ""
        res = reject(int(argv[1]), reviewer_open_id="",
                     reviewer_name="(cli)", reason=reason)
        print(json.dumps(res, indent=2))
        return 0
    print("commands: cluster-pending | list-pending [workflow] | "
          "approve <id> | reject <id> [reason]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
