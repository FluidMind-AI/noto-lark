#!/usr/bin/env python3
"""
Humanizer — anti-AI-writing rules injected into every Noto writing
surface (email drafts, submissions, target lists, doc deliverables).

Distilled from blader/humanizer (MIT, v2.9.x), itself built on
Wikipedia's "Signs of AI writing." Operator directive 2026-07-24 after
a draft read "super exaggerated, AI-like" ("squarely in our wheelhouse,"
"sounds fascinating," "add real value").

Two invariants the source skill insists on and we keep:
- NO FABRICATION: humanizing never invents or drops facts.
- The author's OWN writing samples outrank every rule here — when
  exemplars conflict with a rule, write like the exemplars.
"""

HUMANIZER_RULES = """\
WRITE LIKE A HUMAN — hard rules (the author's own writing samples
override these; nothing here may change facts):
- Kill AI-tell vocabulary: wheelhouse, fascinating, leverage, delve,
  tapestry, landscape, robust, seamless, journey, elevate, "add real
  value", "excited to", "I'd love to", "resonates", "truly", "keen".
- No inflated significance or flattery. State the thing, not how
  remarkable the thing is. One idea per sentence when possible.
- Plain verbs: "is/does/runs", never "serves as/functions as/acts as".
- No rule-of-three lists for rhythm. No negative parallelism ("not
  just X, but Y"). No false ranges ("from X to Y"). No manufactured
  punchlines or aphorisms.
- No em or en dashes. Use commas, periods, or parentheses.
- No chatbot pleasantries ("I hope this helps", "Looking forward to
  it" as a reflex closer), no sycophancy, no hedging stacks ("might
  potentially"), no signposting ("It's worth noting", "Additionally").
- Vary sentence length like people do. Short is fine. Fragments too,
  sparingly.
- If the author's samples are terse, BE TERSE. Most business email is
  two to five plain sentences."""


def humanizer_block() -> str:
    return HUMANIZER_RULES
