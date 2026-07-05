"""
Research skill — Q&A over the company corpus + entity graph + vector
RAG. Thin wrapper around noto_research.research() so the agent can
dispatch it like any other skill.

Preserves streaming: on_progress + on_token are passed through, so the
streaming card UX is identical to calling research() directly.
"""

import os
import sys
from typing import Any, Callable, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def answer_question(
        question: str,
        history: Optional[List[Any]] = None,
        recruiter_context: str = "",
        on_progress: Optional[Callable[[str], None]] = None,
        on_token: Optional[Callable[[str], None]] = None) -> str:
    """Answer a question from the company corpus. Returns the synthesized
    markdown answer; the bot's card is updated live via on_token (if
    provided) so the user sees the answer stream in."""
    from noto_research import research as _research
    return _research(question, history=history or [],
                     on_progress=on_progress, on_token=on_token,
                     recruiter_context=recruiter_context)
