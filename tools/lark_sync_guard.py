#!/usr/bin/env python3
"""
Pull-before-push guard for Noto's managed Lark docs (Phase 1).

The hard rule this enforces: **never silently overwrite a change a human
made directly in Lark.** Before the bot pushes ANY edit to a managed
artifact doc (target list / workup / firm-fit), it calls preflight_pull,
which reads the live Lark doc's metadata + content and compares against
the sync baseline stored in candidate_artifacts.db. If a human edited
the doc since the bot last synced it, the push is REFUSED and the
operator is alerted with the doc link.

Phase 1 is DETECT-ONLY: on a detected human edit (DIRTY), abort + alert.
Later phases (per docs/local-backend-architecture.md) upgrade this to
section-scoped re-pull-and-re-apply so most concurrent edits auto-merge
instead of aborting.

This module is READ-ONLY against Lark (get_doc_meta + get_docx_blocks).
It contains no removal-capable Lark calls, so assert_no_lark_delete is
unaffected.
"""

import hashlib
import os
import re
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# Header line shapes we strip before hashing (the operator-provenance
# header is rewritten by the bot on every push, so it must NOT count as
# a "human edit" when comparing the live doc to the baseline).
_HEADER_LINE_RE = re.compile(
    r"^\s*(?:last update requested by|on)\s*:", re.I)
_HEADER_RULE_RE = re.compile(r"^[\s═=–—-]{3,}\s*$")


def _block_text(b: Dict[str, Any]) -> str:
    """Plain text of a single block across every text-bearing field."""
    s = ""
    for fk in ("page", "text", "heading1", "heading2", "heading3",
               "heading4", "heading5", "heading6", "heading7",
               "heading8", "heading9", "bullet", "ordered", "code",
               "quote", "todo", "callout", "equation"):
        v = b.get(fk, {}) or {}
        for el in (v.get("elements", []) or []):
            tr = el.get("text_run", {}) or {}
            s += tr.get("content", "") or ""
    return s


def normalize_doc_text(blocks: List[Dict[str, Any]]) -> str:
    """Render a doc's blocks to a normalized plain-text form for
    hashing. Drops the operator-provenance header (bot-rewritten every
    push), collapses whitespace, lowercases — so cosmetic / bot-owned
    churn doesn't read as a human edit, but any real content change
    does."""
    lines: List[str] = []
    for b in blocks:
        t = _block_text(b)
        for ln in t.splitlines():
            s = ln.strip()
            if not s:
                continue
            if _HEADER_LINE_RE.match(s) or _HEADER_RULE_RE.match(s):
                continue
            lines.append(re.sub(r"\s+", " ", s).lower())
    return "\n".join(lines)


def compute_doc_hash(client: Any, doc_id: str) -> str:
    """sha256 of the normalized live doc body. Empty string on failure
    (caller treats a hash it can't compute conservatively)."""
    try:
        blocks = client.get_docx_blocks(doc_id)
    except Exception as e:
        print(f"[sync-guard] compute_doc_hash: get_docx_blocks failed "
              f"for {doc_id}: {e}", file=sys.stderr, flush=True)
        return ""
    norm = normalize_doc_text(blocks)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _bot_open_id() -> str:
    """The Noto bot's own user open_id, if resolvable — so the guard can
    tell the bot's own last edit apart from a user's. Best-effort;
    empty string if unavailable (then we fall back to content hashing,
    which is the authority anyway)."""
    try:
        from config import load_config
        cfg = load_config().get("lark", {}) or {}
        return (cfg.get("bot_open_id") or "").strip()
    except Exception:
        return ""


def preflight_pull(client: Any,
                    artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Read the live Lark doc and decide whether it's safe to push.

    Returns a dict:
      {"state": "clean" | "dirty" | "first_sync" | "error",
       "live_modify_time": str, "live_modify_user": str,
       "live_hash": str, "message": str}

    - clean      → no human edit since the baseline; safe to push. The
                   live_* fields carry the values the caller should
                   persist as the NEW baseline after a successful push.
    - first_sync → no baseline existed (TOFU). We captured the live
                   state as the baseline; caller may proceed (we cannot
                   detect a change we had no baseline for).
    - dirty      → the doc changed in Lark since the baseline → DO NOT
                   push (Phase 1 detect-only). message explains.
    - error      → couldn't read live state; caller decides (default:
                   refuse, to stay safe).
    """
    doc_id = artifact.get("doc_id")
    if not doc_id:
        return {"state": "error", "message": "artifact has no doc_id",
                "live_modify_time": "", "live_modify_user": "",
                "live_hash": ""}

    # 1) Cheap metadata gate.
    try:
        meta = client.get_doc_meta(doc_id)
    except Exception as e:
        return {"state": "error",
                "message": f"couldn't read Lark metadata: {e}",
                "live_modify_time": "", "live_modify_user": "",
                "live_hash": ""}
    live_t = str(meta.get("latest_modify_time") or "")
    live_u = str(meta.get("latest_modify_user") or "")

    base_t = str(artifact.get("lark_last_modify_time") or "")
    base_hash = str(artifact.get("synced_content_hash") or "")

    # No baseline yet → TOFU. Capture current state; can't judge change.
    if not base_t and not base_hash:
        live_hash = compute_doc_hash(client, doc_id)
        return {"state": "first_sync",
                "message": "no baseline — captured current state (TOFU)",
                "live_modify_time": live_t, "live_modify_user": live_u,
                "live_hash": live_hash}

    # mtime unchanged → nothing touched it. Safe.
    if base_t and live_t and live_t == base_t:
        return {"state": "clean",
                "message": "unchanged since last sync (mtime match)",
                "live_modify_time": live_t, "live_modify_user": live_u,
                "live_hash": base_hash}

    # 2) mtime moved (or missing) → confirm with content hash. Timestamps
    # lie (view-state / comments / format ops bump mtime); content is the
    # authority.
    live_hash = compute_doc_hash(client, doc_id)
    if live_hash and base_hash and live_hash == base_hash:
        # mtime moved but content identical → not a real edit. Refresh
        # the marker (caller persists) and treat as clean.
        return {"state": "clean",
                "message": "mtime moved but content identical "
                           "(spurious — refreshing marker)",
                "live_modify_time": live_t, "live_modify_user": live_u,
                "live_hash": live_hash}

    # 3) Content genuinely differs from baseline → a human edited Lark.
    who = live_u or "someone"
    bot_id = _bot_open_id()
    note = ""
    if bot_id and live_u and live_u != bot_id:
        note = " (edited by a user, not Noto)"
    return {"state": "dirty",
            "message": f"doc changed in Lark since last sync{note} — "
                       f"last editor: {who}",
            "live_modify_time": live_t, "live_modify_user": live_u,
            "live_hash": live_hash}


def capture_baseline_after_push(client: Any, artifact_id: int,
                                 doc_id: str) -> None:
    """After a successful bot push, re-read the live doc and store it as
    the new baseline (the 'commit' step). Re-pulling AFTER the write —
    rather than trusting what we intended to write — is what makes the
    baseline authoritative even if Lark applied the write slightly
    differently. Best-effort: a baseline-capture failure marks the
    artifact dirty_after_push so the next edit re-checks rather than
    trusting a stale baseline."""
    import candidate_artifacts as _ca
    try:
        meta = client.get_doc_meta(doc_id)
        live_t = str(meta.get("latest_modify_time") or "")
        live_u = str(meta.get("latest_modify_user") or "")
        live_hash = compute_doc_hash(client, doc_id)
        _ca.set_sync_baseline(artifact_id, live_t, live_u, live_hash,
                               sync_state="clean")
    except Exception as e:
        print(f"[sync-guard] capture_baseline_after_push failed for "
              f"{doc_id}: {e}", file=sys.stderr, flush=True)
        try:
            _ca.set_sync_state(artifact_id, "dirty_after_push")
        except Exception:
            pass


def persist_first_sync(artifact_id: int,
                        result: Dict[str, Any]) -> None:
    """Persist a first_sync (TOFU) or clean-with-refreshed-marker
    preflight result as the baseline."""
    import candidate_artifacts as _ca
    _ca.set_sync_baseline(
        artifact_id,
        result.get("live_modify_time", ""),
        result.get("live_modify_user", ""),
        result.get("live_hash", ""),
        sync_state="clean")


def dirty_alert_message(artifact: Dict[str, Any],
                         result: Dict[str, Any]) -> str:
    """Operator-facing message when a push is refused because the doc
    changed in Lark. Phase 1 detect-only behaviour."""
    cand = artifact.get("candidate_name", "?")
    atype = (artifact.get("artifact_type", "") or "").replace("_", " ")
    url = artifact.get("doc_url", "")
    return (f"✋ I didn't change **{cand}**'s {atype} — it was edited "
            f"directly in Lark since I last synced it, and I won't "
            f"overwrite that.\n\n_{result.get('message','')}_\n\n"
            f"[open the document]({url})\n\n"
            f"Re-send your change and I'll work from the current "
            f"version. (Auto-merge of in-Lark edits is coming — for "
            f"now I stop rather than risk clobbering a user's "
            f"work.)")


def _selftest() -> int:
    ok = True

    # normalize strips header + collapses whitespace + lowercases
    blocks = [
        {"heading1": {"elements": [{"text_run": {"content": "Strong Fit"}}]}},
        {"text": {"elements": [{"text_run": {
            "content": "Last update requested by: the operator"}}]}},
        {"text": {"elements": [{"text_run": {"content": "On: 2026-05-28"}}]}},
        {"text": {"elements": [{"text_run": {"content": "════════════"}}]}},
        {"text": {"elements": [{"text_run": {
            "content": "  Cooley   Singapore  "}}]}},
    ]
    norm = normalize_doc_text(blocks)
    checks = [
        ("strong fit" in norm, "heading kept + lowercased"),
        ("last update requested by" not in norm, "operator header stripped"),
        ("════" not in norm, "rule line stripped"),
        ("cooley singapore" in norm, "whitespace collapsed"),
    ]
    for good, label in checks:
        print(("PASS: " if good else "FAIL: ") + label)
        ok &= good

    # hash is stable + sensitive
    h1 = hashlib.sha256(norm.encode()).hexdigest()
    blocks2 = list(blocks)
    blocks2[-1] = {"text": {"elements": [{"text_run": {
        "content": "Cooley Hong Kong"}}]}}    # content changed
    h2 = hashlib.sha256(normalize_doc_text(blocks2).encode()).hexdigest()
    print(("PASS: " if h1 != h2 else "FAIL: ") +
          "hash changes when content changes")
    ok &= (h1 != h2)

    # header-only churn does NOT change the hash
    blocks3 = list(blocks)
    blocks3[1] = {"text": {"elements": [{"text_run": {
        "content": "Last update requested by: Alexis Lamb"}}]}}
    h3 = hashlib.sha256(normalize_doc_text(blocks3).encode()).hexdigest()
    print(("PASS: " if h1 == h3 else "FAIL: ") +
          "header churn does NOT change the hash")
    ok &= (h1 == h3)

    print("\nselftest:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


def backfill_baselines(verbose: bool = True) -> Dict[str, int]:
    """Capture a sync baseline for every registered artifact that
    doesn't have one yet. Run once after deploying Phase 1 so existing
    docs are protected from the next edit onward (rather than waiting
    for each one's first-edit TOFU). Idempotent — skips artifacts that
    already have a baseline."""
    import candidate_artifacts as _ca
    from lark_client import LarkClient
    try:
        from lark_oauth import get_user_token
        client = LarkClient(user_token=get_user_token())
    except Exception:
        client = LarkClient()
    done = skipped = errors = 0
    rows = _ca.all_artifacts(limit=10_000)
    if verbose:
        print(f"[sync-guard] backfilling baselines over {len(rows)} "
              f"artifacts…")
    for r in rows:
        if (r.get("synced_content_hash") or r.get("lark_last_modify_time")):
            skipped += 1
            continue
        try:
            meta = client.get_doc_meta(r["doc_id"])
            live_t = str(meta.get("latest_modify_time") or "")
            live_u = str(meta.get("latest_modify_user") or "")
            live_hash = compute_doc_hash(client, r["doc_id"])
            _ca.set_sync_baseline(r["id"], live_t, live_u, live_hash,
                                   sync_state="clean")
            done += 1
            if verbose:
                print(f"  ✓ {r['candidate_name']} / {r['artifact_type']}")
        except Exception as e:
            errors += 1
            if verbose:
                print(f"  ✗ {r.get('candidate_name')} / "
                      f"{r.get('artifact_type')}: {e}")
    if verbose:
        print(f"[sync-guard] backfill done — captured={done} "
              f"skipped={skipped} errors={errors}")
    return {"captured": done, "skipped": skipped, "errors": errors}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        return _selftest()
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        backfill_baselines(verbose=True)
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
