"""
General document skill — create / edit any Lark doc. Users can ask for
arbitrary docs like meeting notes, memos, one-off writeups, etc.

Composes doc_tools primitives + the LLM for content generation/editing.
No template assumptions — content is whatever the user described or
whatever lives in the doc being edited.
"""

import os
import sys
from typing import Any, Callable, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_CONTENT_GEN_PROMPT = """\
You are Noto, drafting a document for your organization. The user
described what they want; produce the FULL markdown content of the
document. Use clear headings, bullet lists where appropriate, and a
professional, concise tone. Do NOT include the title (Lark adds the
title separately from the body).

DOC TITLE: {title}
USER'S BRIEF / CONTENT:
{brief}

Output only the markdown body. No fences, no meta-commentary."""


_EDIT_PROMPT = """\
You are revising a Lark document per the user's instruction. Output
the FULL revised markdown of the document. Preserve the structure and
tone; apply ONLY what the instruction asks for; do not volunteer other
rewrites.

INSTRUCTION:
{instruction}

CURRENT DOC:
{current}

Output only the revised markdown body. No fences, no meta-commentary."""


def create_doc(
        client: Any,
        title: str,
        content_or_brief: str,
        folder: str = "",
        is_brief: bool = True,
        on_progress: Optional[Callable[[str], None]] = None
        ) -> Dict[str, Any]:
    """Create a new Lark doc.

    folder: optional Drive folder token for where the doc should land.
      When omitted, the doc lands in the configured outputs folder
      (config key corpus.outputs_folder); Drive root only as a last
      resort.

    is_brief=True (default): treat content_or_brief as the user's
      DESCRIPTION of what they want — LLM generates the full markdown
      body. is_brief=False: treat content_or_brief as the final
      markdown body and use it verbatim.

    Returns {ok, doc_id, doc_url, body_block_id, title}."""
    if is_brief:
        if on_progress:
            on_progress("✍️ Drafting the document content…")
        from noto_research import _claude
        prompt = _CONTENT_GEN_PROMPT.format(
            title=title, brief=content_or_brief)
        try:
            body = (_claude(prompt, timeout=120, web=False) or "").strip()
        except Exception as e:
            return {"ok": False, "error": f"content generation failed: {e}"}
        if not body:
            return {"ok": False, "error":
                    "content generator returned empty text"}
    else:
        body = (content_or_brief or "").strip()
        if not body:
            return {"ok": False, "error":
                    "no content supplied for the doc"}

    if on_progress:
        on_progress("📄 Saving to Lark…")
    target = (folder or "").strip()
    if target:
        landed_in = "the requested folder"
    else:
        # No folder given — use the configured outputs folder if set.
        # Falls back to Drive root only as last resort (and we log a
        # warning so it's visible the config isn't set).
        try:
            from config import load_config
            # config slot lives under corpus: (sibling of drive_root)
            target = (load_config().get("corpus", {})
                      .get("outputs_folder", "") or "").strip()
        except Exception:
            target = ""
        if not target:
            print("[skills.general_doc] WARNING: corpus.outputs_folder "
                  "is empty — doc lands in Drive root. Set an outputs "
                  "folder token in lolabot.yaml.",
                  file=sys.stderr, flush=True)
        landed_in = ("outputs folder" if target
                     else "Drive root (no outputs_folder set)")
    try:
        from lark_doc_writer import create_lark_doc_with_meta
        d = create_lark_doc_with_meta(title, body,
                                      folder_token=target or None)
        doc = {"doc_id": d["document_id"], "url": d["url"],
               "block_id": d["block_id"]}
    except Exception as e:
        return {"ok": False, "error": f"doc creation failed: {e}",
                "draft": body}
    return {"ok": True, "doc_id": doc["doc_id"], "doc_url": doc["url"],
            "body_block_id": doc.get("block_id", ""),
            "title": title, "draft": body,
            "landed_in": landed_in}


def edit_doc(
        client: Any, doc_id: str, instruction: str,
        on_progress: Optional[Callable[[str], None]] = None
        ) -> Dict[str, Any]:
    """Fetch the live doc, apply the instruction via LLM, write back.

    Returns {ok, doc_id, doc_url, new_draft, prior_chars}."""
    from skills.doc_tools import fetch_doc_markdown, update_doc_in_place
    if on_progress:
        on_progress("📖 Reading the current document…")
    try:
        current = fetch_doc_markdown(client, doc_id)
    except Exception as e:
        return {"ok": False, "error": f"couldn't read doc: {e}"}
    if not current.strip():
        return {"ok": False, "error": "doc is empty — nothing to edit"}

    if on_progress:
        on_progress("✏️ Revising…")
    from noto_research import _claude
    prompt = _EDIT_PROMPT.format(instruction=instruction,
                                 current=current[:60000])
    try:
        new_md = (_claude(prompt, timeout=180, web=False) or "").strip()
    except Exception as e:
        return {"ok": False, "error": f"edit pass failed: {e}"}
    if not new_md:
        return {"ok": False, "error":
                "edit returned empty — doc was not changed"}

    if on_progress:
        on_progress("📄 Writing back to Lark…")
    try:
        update_doc_in_place(client, doc_id, new_md, body_block_id=None)
    except Exception as e:
        return {"ok": False, "error": f"doc update failed: {e}",
                "new_draft": new_md}
    # Feedback capture (workflow=doc_edit). Best-effort.
    try:
        from feedback_capture import (capture_event,
                                       SOURCE_BOT_EDIT,
                                       WORKFLOW_DOC_EDIT)
        capture_event(workflow=WORKFLOW_DOC_EDIT,
                      source=SOURCE_BOT_EDIT, doc_id=doc_id,
                      before_md=current, after_md=new_md,
                      instruction=instruction)
    except Exception:
        pass

    return {"ok": True, "doc_id": doc_id, "doc_url": "",
            "new_draft": new_md, "prior_chars": len(current)}
