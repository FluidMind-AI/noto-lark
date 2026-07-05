#!/usr/bin/env python3
"""
Attachment reader — Foundation A of the H2 roadmap.

One entry point for "turn this Lark-hosted binary into text", whatever
its source: a Drive file or a chat-
message attachment (F3 — needs the im:resource scope; until the
operator grants it in the Console, that path returns a clear
'im_resource_scope_missing' result instead of crashing callers).

  read_bytes(blob, filename)                  -> {ok, text, ...}
  read_drive_file(file_token, filename)       -> {ok, text, ...}
  read_message_resource(message_id, file_key, filename)
                                              -> {ok, text, ...} |
                                                 {ok: False, reason:
                                                  'im_resource_scope_missing'}

Extraction itself is file_text.extract_file_text (pypdf/python-docx);
this module adds transport, size guards and uniform error shapes.
Read-only: downloads and parses, never writes to Lark.

CLI (smoke): python tools/attachment_reader.py drive <file_token> [name]
"""

import sys
import urllib.error
from typing import Any, Dict, Optional

sys.path.insert(0, __file__.rsplit("/", 1)[0])

# Refuse to pull anything bigger than this into memory (biggest real
# typical business PDFs are well under this).
MAX_BYTES = 30 * 1024 * 1024


def read_bytes(blob: bytes, filename: str) -> Dict[str, Any]:
    """Extract text from raw bytes. Uniform result shape."""
    if len(blob) > MAX_BYTES:
        return {"ok": False, "reason": "too_large",
                "size": len(blob), "filename": filename}
    from file_text import extract_file_text
    text = extract_file_text(blob, filename)
    if text is None:
        return {"ok": False, "reason": "unsupported_type",
                "filename": filename, "size": len(blob)}
    return {"ok": True, "text": text, "filename": filename,
            "size": len(blob), "chars": len(text)}


def read_drive_file(file_token: str,
                    filename: Optional[str] = None) -> Dict[str, Any]:
    """Download a Drive file and extract its text. If `filename` is
    omitted it's fetched from the file's meta (extension drives the
    extractor dispatch)."""
    try:
        from lark_client import LarkClient
        client = LarkClient()
        if not filename:
            try:
                metas = client.get_docs_meta_batch([(file_token, "file")])
                filename = (metas[0].get("title") or "") if metas else ""
            except Exception:
                filename = ""
        blob = client.download_file(file_token)
    except Exception as e:
        return {"ok": False, "reason": "download_failed",
                "file_token": file_token, "error": str(e)[:200]}
    res = read_bytes(blob, filename or "")
    res["file_token"] = file_token
    return res


def read_message_resource(message_id: str, file_key: str,
                          filename: str) -> Dict[str, Any]:
    """Download a chat-message attachment and extract its text. Needs
    the im:resource scope (operator prerequisite) — until granted, the
    API 403s and this reports it as a recognizable reason so F3 can
    queue the item instead of failing."""
    try:
        from lark_client import LarkClient
        blob = LarkClient().download_message_resource(
            message_id, file_key, "file")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"ok": False, "reason": "im_resource_scope_missing",
                    "message_id": message_id, "file_key": file_key,
                    "hint": "grant im:resource in the Lark Console "
                            "(H2 operator prerequisite), then retry"}
        return {"ok": False, "reason": "download_failed",
                "error": f"HTTP {e.code}", "message_id": message_id}
    except Exception as e:
        return {"ok": False, "reason": "download_failed",
                "error": str(e)[:200], "message_id": message_id}
    res = read_bytes(blob, filename)
    res["message_id"] = message_id
    return res


if __name__ == "__main__":
    import json as _json
    if len(sys.argv) >= 3 and sys.argv[1] == "drive":
        out = read_drive_file(sys.argv[2],
                              sys.argv[3] if len(sys.argv) > 3 else None)
        preview = (out.pop("text", "") or "")[:400]
        print(_json.dumps(out, indent=2, ensure_ascii=False))
        if preview:
            print("--- text preview ---\n" + preview)
    else:
        print(__doc__)
