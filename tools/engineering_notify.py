#!/usr/bin/env python3
"""
Engineering update notifications — the "ready to test" leg of the
engineering-lesson loop.

Flow (docs/admin-panel-v2.md + operator direction 2026-07-03):
  approve engineering lesson in panel → brain/engineering-backlog.md →
  Noto fixes on the branch → send() posts a ready-to-test update to the
  Noto Engineering group chat so the operator + testers can verify.

Destination resolution:
  1. notolark.yaml → lark.engineering_chat_id   (the dedicated group)
  2. fallback: DM the first super_admin from memory/operators.yaml
     (so updates are never silently dropped before the chat exists)

Send-only (create message) — no reading, no deletion, consistent with
the Lark data-safety rules.

CLI:
  python tools/engineering_notify.py send "Fixed X — test by doing Y"
  python tools/engineering_notify.py where     # show resolved target
"""

import os
import sys
from typing import Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load_config  # noqa: E402


def _target() -> Tuple[str, str]:
    """Returns (receive_id, receive_id_type)."""
    cfg = load_config()
    chat_id = str((cfg.get("lark") or {}).get(
        "engineering_chat_id", "") or "").strip()
    if chat_id:
        return chat_id, "chat_id"
    try:
        from feedback_capture import admin_ids, is_super_admin
        for oid in admin_ids():
            if is_super_admin(oid):
                return oid, "open_id"
    except Exception:
        pass
    return "", ""


def send(text: str) -> bool:
    """Post an engineering update. Returns True on success."""
    rid, rtype = _target()
    if not rid:
        print("[engineering_notify] no target — set "
              "lark.engineering_chat_id in notolark.yaml or define a "
              "super_admin in operators.yaml", file=sys.stderr)
        return False
    from lark_client import LarkClient
    prefix = "🔧 **Noto engineering update**\n"
    LarkClient().send_text(rid, prefix + text, receive_id_type=rtype)
    return True


def main(argv) -> int:
    if not argv:
        print(__doc__.strip())
        return 0
    if argv[0] == "send" and len(argv) >= 2:
        ok = send(" ".join(argv[1:]))
        print("sent" if ok else "FAILED")
        return 0 if ok else 1
    if argv[0] == "where":
        rid, rtype = _target()
        print(f"{rtype or '(none)'}: {rid or '(no target configured)'}")
        return 0
    print("commands: send <text> | where", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
