"""
Feedback loop — Phase D: weekly digest poster.

Posts pending recommended_rules to the right chat per workflow:

  general / doc_edit / q_and_a           → Management chat (admins)
  product                                → super-admin DM

Cadence: 7+ days since last post for that target (tracked in
brain/feedback-digest-state.json). Run nightly from noto-resync;
posts only when due, --force to post regardless.

  python tools/feedback_digest.py post           # post if due
  python tools/feedback_digest.py post --force   # post regardless
  python tools/feedback_digest.py status         # show last-post timestamps
"""

import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DIGEST_CADENCE_DAYS = 7

# Workflow → target chat mapping (the operator's design — skill-workflow
# feedback to Management chat, product-level only to super-admin DM).
SKILL_WORKFLOWS = ("general", "doc_edit", "q_and_a")
PRODUCT_WORKFLOWS = ("product",)


def _home() -> str:
    from config import get_home
    return get_home()


def _state_path() -> str:
    return os.path.join(_home(), "brain", "feedback-digest-state.json")


def _load_state() -> Dict[str, Any]:
    try:
        with open(_state_path()) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(state, f, indent=2)


def _management_chat_id() -> str:
    try:
        from config import load_config
        return (load_config().get("lark", {})
                .get("management_chat_id", "") or "").strip()
    except Exception:
        return ""


def _super_admin_open_id() -> str:
    """First super-admin open_id from operators.yaml."""
    from feedback_capture import _load_operators_by_id
    for oid, rec in _load_operators_by_id().items():
        if rec.get("super_admin"):
            return oid
    return ""


# ---------------------------------------------------------------------------
# Digest formatting
# ---------------------------------------------------------------------------

def _format_digest(rows: List[Dict[str, Any]],
                   header: str) -> str:
    if not rows:
        return ""
    lines = [f"📋 **{header}** — {len(rows)} pending rule(s) waiting "
             "for admin review."]
    for r in rows[:15]:
        marker = "🔥" if r["priority"] == "high" else "•"
        rejs = (f" · rejected {r['rejection_count']}×"
                if r["rejection_count"] else "")
        lines.append(
            f"\n{marker} **#{r['id']}** [{r['workflow']}] "
            f"(support {r['support_count']}×{rejs})\n"
            f"  _{r['pattern_signature']}_\n"
            f"  > {r['rule_text']}\n"
            f"  `/feedback rule-approve {r['id']}` · "
            f"`/feedback rule-reject {r['id']} [reason]` · "
            f"`/feedback rule-show {r['id']}`")
    if len(rows) > 15:
        lines.append(f"\n_…and {len(rows) - 15} more. Reply "
                     "`/feedback queue` any time for the full list._")
    else:
        lines.append("\n_Reply with the slash command on whichever you "
                     "want to act on. Approving a rule writes it to "
                     "the workflow's lessons file — the drafter picks "
                     "it up on the next draft._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Post
# ---------------------------------------------------------------------------

def post(force: bool = False, verbose: bool = True) -> Dict[str, Any]:
    """Post any due digests. Returns a summary."""
    from feedback_cluster import list_pending
    state = _load_state()
    now = datetime.utcnow()
    out: Dict[str, Any] = {"posted": {}, "skipped": {}}

    # --- 1) Management chat: rules from skill workflows -----------
    mgmt_chat = _management_chat_id()
    if not mgmt_chat:
        if verbose:
            print("  [warn] no lark.management_chat_id in config — "
                  "skipping skill-workflow digest",
                  file=sys.stderr, flush=True)
    else:
        last = state.get("management_chat", {}).get("last_posted_at")
        due = force or _is_due(last, now)
        if not due:
            out["skipped"]["management_chat"] = (
                f"last posted {last}; next due in "
                f"{_days_until_due(last, now):.1f}d")
            if verbose:
                print(f"  [skip] management_chat: {out['skipped']['management_chat']}",
                      flush=True)
        else:
            rows = [r for r in list_pending(limit=50)
                    if r["workflow"] in SKILL_WORKFLOWS]
            if not rows and not force:
                # Nothing pending → no point posting an empty digest;
                # don't update the cadence either so we'll check again
                # next nightly.
                if verbose:
                    print("  [empty] management_chat: no pending "
                          "skill-workflow rules; not posting",
                          flush=True)
            else:
                body = _format_digest(
                    rows or [],
                    "Weekly feedback digest (skill workflows)")
                if not body:
                    body = "📋 **Weekly feedback digest** — no pending rules."
                ok = _send_chat(mgmt_chat, body, verbose)
                if ok:
                    out["posted"]["management_chat"] = {
                        "rules_in_digest": len(rows),
                        "chat_id": mgmt_chat}
                    state.setdefault("management_chat", {})[
                        "last_posted_at"] = now.isoformat(timespec="seconds")

    # --- 2) Super-admin DM: rules from product workflow -----------
    super_oid = _super_admin_open_id()
    if not super_oid:
        if verbose:
            print("  [warn] no super_admin in operators.yaml — skipping "
                  "product-level digest", file=sys.stderr, flush=True)
    else:
        last = state.get("super_admin_dm", {}).get("last_posted_at")
        due = force or _is_due(last, now)
        if not due:
            out["skipped"]["super_admin_dm"] = (
                f"last posted {last}; next due in "
                f"{_days_until_due(last, now):.1f}d")
            if verbose:
                print(f"  [skip] super_admin_dm: {out['skipped']['super_admin_dm']}",
                      flush=True)
        else:
            rows = [r for r in list_pending(limit=50)
                    if r["workflow"] in PRODUCT_WORKFLOWS]
            if not rows and not force:
                if verbose:
                    print("  [empty] super_admin_dm: no pending "
                          "product-level rules; not posting",
                          flush=True)
            else:
                body = _format_digest(
                    rows or [],
                    "Weekly product-feedback digest")
                ok = _send_chat(super_oid, body, verbose, is_user=True)
                if ok:
                    out["posted"]["super_admin_dm"] = {
                        "rules_in_digest": len(rows),
                        "open_id": super_oid}
                    state.setdefault("super_admin_dm", {})[
                        "last_posted_at"] = now.isoformat(timespec="seconds")

    if out["posted"]:
        _save_state(state)
    return out


def _is_due(last_iso: Optional[str], now: datetime) -> bool:
    if not last_iso:
        return True
    try:
        last = datetime.fromisoformat(last_iso)
        return (now - last) >= timedelta(days=DIGEST_CADENCE_DAYS)
    except Exception:
        return True


def _days_until_due(last_iso: Optional[str], now: datetime) -> float:
    if not last_iso:
        return 0.0
    try:
        last = datetime.fromisoformat(last_iso)
        return max(0.0, DIGEST_CADENCE_DAYS - (now - last).total_seconds() / 86400)
    except Exception:
        return 0.0


def _send_chat(target_id: str, text: str, verbose: bool,
               is_user: bool = False) -> bool:
    """Send a chat message via the bot. target_id is a chat_id for
    group chats; if is_user=True, send as a DM to the user's open_id."""
    try:
        from lark_client import LarkClient
        client = LarkClient()
        client.send_text(target_id, text)
        if verbose:
            print(f"  [posted] {target_id} ({'DM' if is_user else 'chat'})",
                  flush=True)
        return True
    except Exception as e:
        print(f"  [err] send failed to {target_id}: {e}",
              file=sys.stderr, flush=True)
        return False


def status() -> Dict[str, Any]:
    state = _load_state()
    now = datetime.utcnow()
    out: Dict[str, Any] = {}
    for key in ("management_chat", "super_admin_dm"):
        last = state.get(key, {}).get("last_posted_at")
        if last:
            out[key] = {
                "last_posted_at": last,
                "days_until_due": _days_until_due(last, now),
            }
        else:
            out[key] = {"last_posted_at": None, "days_until_due": 0.0}
    return out


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 0
    cmd = argv[0]
    if cmd == "post":
        force = "--force" in argv
        res = post(force=force, verbose=True)
        print(json.dumps(res, indent=2))
        return 0
    if cmd == "status":
        print(json.dumps(status(), indent=2))
        return 0
    print("commands: post [--force] | status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
