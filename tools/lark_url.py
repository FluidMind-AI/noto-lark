#!/usr/bin/env python3
"""
Lark URL resolver — Foundation D of the H2 roadmap.

Reverse of "we made a doc, here's the link": take any Lark URL a
user pastes in chat and resolve it to the underlying API object.
F6 (doc-by-link edits) routes on this; today's `_DOC_URL_RE` in
noto_agent/lark_bot only understands `/docx/` links, so wiki pages,
Bases and sheets are invisible to the agent.

  resolve(url)        -> {"ok", "kind", "token", "url_kind", ...}
  extract_links(text) -> [resolve(...) for every Lark URL in the text]

`kind` is the API object type you can act on:
    docx | sheets | base | folder | file | mindnote | doc (legacy)
`token` is the token usable against that object's API. For /wiki/
links the node is resolved via wiki.v2 get_node (needs the operator
user token), so `token` is the wrapped object's obj_token and the raw
node lives in `wiki_node`. Base URLs also carry table_id / view_id
when present in the query string.

Read-only module: parsing + one wiki-node GET. No writes.
"""

import re
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, __file__.rsplit("/", 1)[0])

# Any Lark object URL: /docx/, /wiki/, /base/, /sheets/, legacy /docs/,
# /drive/folder/, /file/, /mindnotes/. Token charset is base62-ish.
LARK_URL_RE = re.compile(
    r"https?://[^\s)>'\"\]]+/"
    r"(docx|wiki|base|sheets|docs|mindnotes|file|drive/folder)"
    r"/([A-Za-z0-9]+)[^\s)>'\"\]]*")

# URL path segment -> API object kind (for the non-wiki, no-API cases)
_PATH_KIND = {
    "docx": "docx",
    "sheets": "sheets",
    "base": "base",
    "docs": "doc",          # legacy doc format — readable, not editable
    "mindnotes": "mindnote",
    "file": "file",
    "drive/folder": "folder",
}

# wiki node obj_type -> API object kind
_OBJ_KIND = {
    "docx": "docx",
    "doc": "doc",
    "sheet": "sheets",
    "bitable": "base",
    "mindnote": "mindnote",
    "file": "file",
}


def resolve(url: str) -> Dict[str, Any]:
    """Resolve one Lark URL to its API object. Never raises."""
    m = LARK_URL_RE.search(url or "")
    if not m:
        return {"ok": False, "reason": "not_a_lark_object_url",
                "url": (url or "")[:200]}
    url_kind, token = m.group(1), m.group(2)
    out: Dict[str, Any] = {"ok": True, "url": m.group(0),
                           "url_kind": url_kind, "token": token}
    qs = parse_qs(urlparse(m.group(0)).query)
    if url_kind == "wiki":
        # A wiki link is a NODE token — the editable object is wrapped
        # inside it; resolve via the API.
        try:
            from lark_client import LarkClient
            node = LarkClient().get_wiki_node(token)
        except Exception as e:
            return {"ok": False, "reason": "wiki_node_resolve_failed",
                    "url_kind": "wiki", "node_token": token,
                    "error": str(e)[:200]}
        obj_type = (node or {}).get("obj_type") or ""
        out.update({
            "kind": _OBJ_KIND.get(obj_type, obj_type or "unknown"),
            "token": (node or {}).get("obj_token") or "",
            "title": (node or {}).get("title") or "",
            "wiki_node": {"node_token": token,
                          "space_id": (node or {}).get("space_id"),
                          "obj_type": obj_type},
        })
        if not out["token"]:
            out.update({"ok": False, "reason": "wiki_node_no_obj_token"})
        return out
    out["kind"] = _PATH_KIND.get(url_kind, "unknown")
    if url_kind == "base":
        if qs.get("table"):
            out["table_id"] = qs["table"][0]
        if qs.get("view"):
            out["view_id"] = qs["view"][0]
    return out


def extract_links(text: str) -> List[Dict[str, Any]]:
    """Resolve every Lark object URL in a blob of text (deduped,
    document order)."""
    seen, out = set(), []
    for m in LARK_URL_RE.finditer(text or ""):
        key = (m.group(1), m.group(2))
        if key in seen:
            continue
        seen.add(key)
        out.append(resolve(m.group(0)))
    return out


if __name__ == "__main__":
    import json as _json
    if len(sys.argv) < 2:
        print("usage: lark_url.py <url-or-text>", file=sys.stderr)
        sys.exit(2)
    blob = " ".join(sys.argv[1:])
    print(_json.dumps(extract_links(blob), indent=2, ensure_ascii=False))
