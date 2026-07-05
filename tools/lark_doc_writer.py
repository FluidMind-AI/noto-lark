#!/usr/bin/env python3
"""
Lark doc writer — turns a markdown answer into a Lark document.

Used when Noto produces a substantial deliverable (target list, firm-fit
analysis): the answer is created as a real Lark doc — in the candidate's
folder or the "Noto Outputs" folder — and the bot replies with the link.

CREATE-ONLY: this never edits or removes existing docs (consistent with
the Lark Data Safety rule). Uses the Noto user token.
"""

import os
import re
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _clean(t: str) -> str:
    """Strip markdown bold/italic/code — but PRESERVE [text](url) link
    syntax. lark_client.add_doc_blocks splits those into multiple
    TextElements (one per segment) so the URL becomes a real inline
    Lark hyperlink. Stripping links here would lose them before that
    splitter ever runs."""
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", t)
    t = re.sub(r"__(.+?)__", r"\1", t)
    t = re.sub(r"`(.+?)`", r"\1", t)
    return t.strip()


def markdown_to_blocks(md: str) -> List[Dict[str, str]]:
    """Convert a markdown answer into Lark block dicts [{kind, text}].
    Covers headings, paragraphs, bullets, ordered lists, quotes, code.
    Tables / unknown constructs degrade gracefully to text."""
    blocks: List[Dict[str, str]] = []
    lines = (md or "").splitlines()
    para: List[str] = []

    def flush():
        if para:
            text = _clean(" ".join(para).strip())
            if text:
                blocks.append({"kind": "text", "text": text})
            para.clear()

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("```"):                       # code fence
            flush()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            blocks.append({"kind": "code", "text": "\n".join(code)})
            continue
        if not s:                                     # blank -> para break
            flush()
            i += 1
            continue
        m = re.match(r"(#{1,6})\s+(.*)", s)
        if m:
            flush()
            lvl = min(len(m.group(1)), 6)
            blocks.append({"kind": f"heading{lvl}",
                           "text": _clean(m.group(2))})
            i += 1
            continue
        m = re.match(r"[-*+]\s+(.*)", s)
        if m:
            flush()
            blocks.append({"kind": "bullet", "text": _clean(m.group(1))})
            i += 1
            continue
        m = re.match(r"\d+[.)]\s+(.*)", s)
        if m:
            flush()
            blocks.append({"kind": "ordered", "text": _clean(m.group(1))})
            i += 1
            continue
        m = re.match(r">\s?(.*)", s)
        if m:
            flush()
            blocks.append({"kind": "quote", "text": _clean(m.group(1))})
            i += 1
            continue
        para.append(s)
        i += 1
    flush()
    return blocks


def _doc_url(document_id: str) -> str:
    from config import load_config
    base = ((load_config().get("lark", {}) or {})
            .get("tenant_url", "https://ajpzz5utq0e3.jp.larksuite.com"))
    return f"{base.rstrip('/')}/docx/{document_id}"


def create_lark_doc(title: str, markdown: str,
                    folder_token: str = None) -> str:
    """Create a Lark doc from a markdown answer; return its URL.

    Uses markdown-to-blocks rendering (headings, bullets, code blocks
    etc. become native Lark block types). Use this when the doc is a
    one-off deliverable that won't be updated in place.

    For deliverables that ARE updated in place (target lists, workups,
    partner firm-fits — see tools/candidate_artifacts.py), use
    create_lark_doc_with_meta() instead: it writes a single text block
    so update_text_doc() can do wholesale body replacement on later
    edits. The trade-off: no rendered markdown hierarchy in the doc."""
    from lark_client import LarkClient
    from lark_oauth import get_user_token
    blocks = markdown_to_blocks(markdown)
    client = LarkClient(user_token=get_user_token())
    doc = client.create_document(title, folder_token)
    doc_id = doc.get("document_id")
    if not doc_id:
        raise RuntimeError(f"create_document returned no document_id: {doc}")
    client.add_doc_blocks(doc_id, blocks)
    return _doc_url(doc_id)


def create_lark_doc_with_meta(title: str, content: str,
                              folder_token: str = None) -> Dict[str, Any]:
    """Create a Lark doc as a SINGLE chunked text block, returning
    {url, document_id, block_id, folder_token}. Use this for singleton
    deliverables (target lists / workups / partner firm-fits) so the
    artifact registry can later call client.update_text_doc() to
    replace the body in place on edits.

    Mirrors what submission_drafter does via client.create_text_doc()
    — submissions take the same trade-off: plain-text-in-one-block in
    exchange for clean in-place updates."""
    from lark_client import LarkClient
    from lark_oauth import get_user_token
    client = LarkClient(user_token=get_user_token())
    created = client.create_text_doc(title, content, folder_token)
    return {
        "url":          created.get("url") or _doc_url(created["document_id"]),
        "document_id":  created["document_id"],
        "block_id":     created.get("block_id", ""),
        "folder_token": folder_token or "",
    }


def _selftest() -> int:
    ok = True
    md = ("# Target List — Jane Doe\n\n"
          "Senior **M&A** partner, Singapore.\n\n"
          "## Tier 1\n"
          "- DLA Piper — flexible tier, global credit\n"
          "- Latham — energy/infra king\n\n"
          "1. First step\n2. Second step\n\n"
          "> Caveat: book size\n\n"
          "```\nstatus=draft\n```\n")
    blocks = markdown_to_blocks(md)
    kinds = [b["kind"] for b in blocks]
    checks = [
        ("heading1" in kinds, "heading1 parsed"),
        (kinds.count("heading2") == 1, "heading2 parsed"),
        (kinds.count("bullet") == 2, "bullets parsed"),
        (kinds.count("ordered") == 2, "ordered parsed"),
        ("quote" in kinds, "quote parsed"),
        ("code" in kinds, "code parsed"),
        (any(b["kind"] == "text" and "M&A" in b["text"]
             and "**" not in b["text"] for b in blocks),
         "paragraph parsed + emphasis stripped"),
    ]
    for good, label in checks:
        print(("PASS: " if good else "FAIL: ") + label)
        ok &= good
    print("\nselftest:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        return _selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
