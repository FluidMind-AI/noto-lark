"""
Doc primitives — update / fetch Lark docs. The thinnest possible
wrappers around lark_client so skills compose them without touching
the SDK directly.

These are pure 'do one thing' tools: they don't decide WHEN to call
themselves, they just do their thing when called.
"""

import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def update_doc_in_place(client: Any, doc_id: str, content_md: str,
                        body_block_id: Optional[str] = None) -> None:
    """Overwrite an existing Lark doc's body with new markdown — the v2,
    v3, … updates of the SAME doc. body_block_id is optional;
    update_text_doc resolves it from the doc when None."""
    client.update_text_doc(doc_id, content_md,
                           body_block_id=body_block_id or None)


def fetch_doc_markdown(client: Any, doc_id: str) -> str:
    """Read the live Lark doc back as markdown — users may have edited
    it directly since the bot last wrote, so this is what an 'edit'
    should base its v2 on, not a cached copy."""
    from lark_sync import render_blocks_markdown
    blocks = client.get_docx_blocks(doc_id)
    return render_blocks_markdown(blocks)
