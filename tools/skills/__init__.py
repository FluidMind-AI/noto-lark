"""
Noto skills — first-class units of work the agent can compose.

Each module in this package exposes pure-ish functions with clean
signatures (inputs → results). Skills DON'T decide when to run
(that's the agent), DON'T own the streaming card UX (that's the
agent), and DON'T touch routing (that's the agent). They do the work.

Tools (lower-level primitives like update_doc_in_place) live in
tools/skills/doc_tools.py and are shared across skills.
"""
