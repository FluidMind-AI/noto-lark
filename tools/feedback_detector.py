#!/usr/bin/env python3
"""
Feedback detection — decide whether a user message contains lasting
feedback the operator should review later.

Two layers:
  - Heuristic regex for obvious "lasting rule" cues. Cheap, runs on
    every inbound user message.
  - LLM micro-classifier for ambiguous cases — runs only in HIGH-SIGNAL
    contexts (e.g. inside an active drafting session where most
    user follow-ups ARE feedback). Avoids the cost of running the LLM
    on every chat message.

The bot's _worker calls is_likely_feedback(...) and if True writes a
row to feedback_store.

CLI: python tools/feedback_detector.py selftest
"""

import os
import re
import sys
from typing import Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# Patterns that are reliably "this is a lasting rule, not just a one-off
# edit". The user is teaching the bot something for the future.
_LASTING_PATTERNS = (
    r"\b(?:never|don'?t (?:ever|include|use|do|write|mention)|"
    r"should (?:not|never)|shouldn'?t|do not (?:include|use|do|write|mention)|"
    r"avoid (?:doing|using|including|mentioning)|"
    r"stop (?:doing|using|including|mentioning)|"
    r"in (?:future|the future|future submissions|future drafts)|"
    r"going forward|for next time|"
    r"in (?:any|all|every) (?:future )?submission|"
    r"as a rule|"
    r"always (?:do|use|include|lead|start|end|open|close|mention|"
    r"write|cite|name|reference|prefer|check|verify|confirm)|"
    r"this is (?:wrong|bad)|don'?t do that again)\b",
)
_LASTING_RE = re.compile("|".join(_LASTING_PATTERNS), re.I)


# Pure one-off edits ("shorter intro" applied to THIS draft) shouldn't
# be filed as lasting feedback. Detect them so we suppress.
_ONE_OFF_PATTERNS = (
    r"^\s*(?:shorter|longer|tighter|looser|less|more|add|remove|cut|"
    r"expand|rewrite|redo|polish|reword|swap|drop|include|exclude|"
    r"focus on|lead with|end with|open with|change|fix|update)\b",
)
_ONE_OFF_RE = re.compile("|".join(_ONE_OFF_PATTERNS), re.I)


# Markers that the feedback is CANDIDATE-SPECIFIC — i.e. about one
# specific person or document, not a general rule. We're saving lasting
# rules that apply to ALL future outputs — not one-off guidance about
# one person's document.
_CANDIDATE_SPECIFIC_RE = re.compile(
    r"\b(?:for this (?:one|candidate|guy|woman|man|person|profile)|"
    r"just for (?:him|her|them|this one)|"
    r"on this (?:one|candidate|profile)|"
    r"(?:his|her|their) (?:clerkship|matters|deals?|summary|cv|"
    r"resume|history|background|experience|practice|stint|tenure))\b",
    re.I)


def heuristic_score(text: str, candidate_name: str = "") -> float:
    """0.0 to 1.0 — confidence that this is LASTING + GENERALLY-
    APPLICABLE feedback (not a one-off edit, not candidate-specific).
    Subtractive: starts at the lasting-pattern match, minus penalties
    for one-off-edit verbs and candidate-specific signals."""
    t = (text or "").strip()
    if not t:
        return 0.0
    score = 0.7 if _LASTING_RE.search(t) else 0.0
    if _ONE_OFF_RE.match(t):
        score -= 0.4
    # quoting a rule-y verb mid-sentence boosts a bit
    if score and re.search(r"\b(?:rule|policy|principle|standard)\b",
                           t, re.I):
        score += 0.2
    # Candidate-specific demotion — "for him/her/them" / "his clerkship" etc.
    if _CANDIDATE_SPECIFIC_RE.search(t):
        score -= 0.5
    # If the active candidate's first or last name appears in the
    # text, the message is almost certainly about THAT candidate
    # specifically — not a general rule.
    if candidate_name:
        first = (candidate_name.split() or [""])[0]
        last = candidate_name.split()[-1] if " " in candidate_name else ""
        if first and re.search(r"\b" + re.escape(first) + r"\b",
                                t, re.I):
            score -= 0.5
        if (last and last != first
                and re.search(r"\b" + re.escape(last) + r"\b", t, re.I)):
            score -= 0.3
    return max(0.0, min(1.0, score))


_LLM_CLASSIFY_PROMPT = """You decide whether a user message contains LASTING FEEDBACK that's GENERALLY APPLICABLE to ALL future outputs (vs a one-off edit to this draft, vs guidance specific to one person or document).

Output ONE WORD: LASTING, ONEOFF, or CANDIDATE_SPECIFIC.

LASTING (output: LASTING) — a general rule applying to ALL future outputs:
- "Never include internal notes in documents shared outside the team."
- "Going forward, don't mention draft status in final reports."
- "We never write 'next steps' as a subheading — drop that pattern entirely."
- "Always lead with the executive summary, not the background."

ONEOFF (output: ONEOFF) — a one-off edit to THIS draft only:
- "Shorter intro."
- "Remove the line about the Q2 budget."
- "Emphasize the rollout timeline more."
- "Tighten paragraph 3."

CANDIDATE_SPECIFIC (output: CANDIDATE_SPECIFIC) — feedback about THIS specific person or document only, not a general rule:
- "Sam never wants the appendix included in his reports."
- "For this document, lead with the risk section."
- "Dana's summary is special — keep the extra detail in for her."
- "Just for him, drop the version-history line."

{candidate_hint}

USER MESSAGE:
{text}"""


def llm_classify(text: str, timeout: int = 60,
                 candidate_name: str = "") -> str:
    """Returns 'LASTING' | 'ONEOFF' | 'CANDIDATE_SPECIFIC' | 'UNKNOWN'."""
    try:
        from noto_research import _claude
        cand_hint = (f"(The active subject is {candidate_name}. "
                     "If the message refers to them by name or by pronoun, "
                     "it's CANDIDATE_SPECIFIC, not LASTING.)"
                     if candidate_name else "")
        out = _claude(_LLM_CLASSIFY_PROMPT.format(
            text=text[:1500], candidate_hint=cand_hint),
            timeout=timeout, web=False).strip().upper()
    except Exception:
        return "UNKNOWN"
    if "CANDIDATE_SPECIFIC" in out or "CANDIDATE SPECIFIC" in out:
        return "CANDIDATE_SPECIFIC"
    if "LASTING" in out:
        return "LASTING"
    if "ONEOFF" in out or "ONE-OFF" in out or "ONE OFF" in out:
        return "ONEOFF"
    return "UNKNOWN"


def is_likely_feedback(text: str,
                       use_llm: bool = False,
                       candidate_name: str = ""
                       ) -> Tuple[bool, str]:
    """Returns (is_feedback, source) where source ∈
    {'heuristic', 'llm', 'none'}.

    use_llm=True when we're in a high-signal context (active drafting
    session — most follow-ups there ARE feedback). candidate_name
    enables subject-specific demotion — we ONLY save generally-
    applicable lasting rules, not subject-specific guidance."""
    hs = heuristic_score(text, candidate_name=candidate_name)
    if hs >= 0.6:
        return True, "heuristic"
    if not use_llm:
        return False, "none"
    # Ambiguous in a high-signal context → LLM-classify (with explicit
    # subject-name context so it can distinguish general from specific).
    if 0.2 < hs < 0.6 or _has_rule_y_language(text):
        verdict = llm_classify(text, candidate_name=candidate_name)
        if verdict == "LASTING":
            return True, "llm"
    return False, "none"


_RULE_HINT = re.compile(
    r"\b(?:always|never|don'?t|shouldn'?t|rule|policy|"
    r"all (?:future |new )?submissions?|every (?:future )?submission|"
    r"never include|never mention|never write|never use)\b", re.I)


def _has_rule_y_language(text: str) -> bool:
    return bool(_RULE_HINT.search(text or ""))


# Patterns that strongly suggest the feedback needs CODE work (a new
# feature, a schema change, an integration) — NOT just a prompt rule.
# Engineering items don't auto-incorporate; they queue for dev work.
_ENGINEERING_RE = re.compile(
    r"\b(?:"
    r"add (?:a |an )?(?:button|feature|command|field|column|tool|page|"
    r"view|integration|tracker|check|filter|hook|webhook|trigger)|"
    r"build (?:a |an |the )?(?:feature|integration|tool|workflow|"
    r"system|tracker|backlog|store|index|skill)|"
    r"implement (?:a |an |the )?(?:feature|workflow|skill|"
    r"check|hook|method)|"
    r"the bot should (?:be able to|track|remember|store|send|email|"
    r"sync|push|sync with|integrate|notify|alert|warn|expose|surface|"
    r"automatically|periodically)|"
    r"we need (?:a |an )?(?:way to|button|feature|tool|skill|view|"
    r"check|tracker|backlog)|"
    r"track (?:.{1,80})(?:status|history|state|over time|"
    r"in (?:a |the )?(?:store|database|table|sheet|base|"
    r"structured way|systematic way|consistent way))|"
    r"remember whether|"
    r"raise (?:a |an )?(?:warning|alert|flag|notification|exception)|"
    r"connect (?:to|with) (?:a |an |the )?[a-z]+ (?:api|service|"
    r"system|tool|integration)|"
    r"data model|schema|new field|new column|new table|new endpoint|"
    r"integration with|"
    r"refactor|rewire|restructure|"
    r"new (?:skill|workflow|store|index|table|column|field|command)"
    r")\b", re.I)


def has_engineering_signal(text: str) -> bool:
    return bool(_ENGINEERING_RE.search(text or ""))


_KIND_PROMPT = """Classify the following feedback as one of:

  RULE         — a writing/behavior rule the model can follow at runtime
                  via a prompt injection (e.g. "never mention draft
                  status in final reports", "always cite the source").
  ENGINEERING  — needs CODE / architecture work to satisfy (a new feature,
                  field, integration, check, webhook). Cannot be solved
                  by appending to a prompt.
  BOTH         — the feedback contains BOTH a rule and an engineering ask
                  (e.g. "track document review stage in a structured way
                  AND never resend a doc a team already declined").
  UNSURE       — genuinely ambiguous.

Examples:
  RULE: "Never include internal notes in shared documents."
  RULE: "Always lead with the executive summary."
  ENGINEERING: "Add a button to clone a draft for another team."
  ENGINEERING: "Track which teams a document has been shared with in a structured way."
  ENGINEERING: "Email me a copy of every audit doc."
  BOTH: "Track review status per document, and never resend a doc to a team whose review status is 'declined'."

Output exactly ONE WORD: RULE, ENGINEERING, BOTH, or UNSURE.

FEEDBACK:
{text}"""


def llm_classify_kind(text: str, timeout: int = 60) -> str:
    """Returns 'rule' | 'engineering' | 'both' | 'unsure'."""
    try:
        from noto_research import _claude
        out = _claude(_KIND_PROMPT.format(text=text[:1500]),
                      timeout=timeout, web=False).strip().upper()
    except Exception:
        return "unsure"
    if "BOTH" in out:
        return "both"
    if "ENGINEERING" in out:
        return "engineering"
    if "RULE" in out:
        return "rule"
    return "unsure"


def classify_kind(text: str, use_llm: bool = False) -> str:
    """Decide rule vs engineering vs both vs unsure. Heuristic first;
    LLM fallback only when the heuristic is undecided AND use_llm=True."""
    has_rule = bool(_LASTING_RE.search(text or ""))
    has_eng = has_engineering_signal(text)
    if has_rule and has_eng:
        return "both"
    if has_eng:
        return "engineering"
    if has_rule:
        return "rule"
    if use_llm and (text or "").strip():
        return llm_classify_kind(text)
    return "unsure"


def _selftest() -> int:
    ok = True
    cases = [
        # (text, should-flag, where-confident)
        ("Never include internal notes for external documents.",
         True, "heuristic"),
        ("Going forward, don't mention draft status in final reports.",
         True, "heuristic"),
        ("In future drafts, always lead with the executive summary.",
         True, "heuristic"),
        ("Shorter intro.", False, None),
        ("Tighten paragraph 3.", False, None),
        ("Remove the line about the Q2 budget.", False, None),
        ("Emphasize the rollout timeline more.", False, None),
        ("This is wrong — the Singapore office doesn't handle procurement.",
         True, "heuristic"),
        ("Don't ever write 'next steps' as a subheading.",
         True, "heuristic"),
    ]
    for text, want, _src in cases:
        # use_llm=False so the test is offline / deterministic
        got, src = is_likely_feedback(text, use_llm=False)
        flag = "PASS" if got == want else "FAIL"
        if flag == "FAIL":
            ok = False
        print(f"  {flag}  feedback={got} ({src}) — {text[:65]}")

    print()
    # kind classification — heuristic only
    kind_cases = [
        ("Never include internal notes in shared documents.", "rule"),
        ("Always lead with the executive summary.", "rule"),
        ("Add a button to clone a draft for another team.", "engineering"),
        ("The bot should remember whether a report has been "
         "reviewed yet.", "engineering"),
        ("Track which teams a document has been shared with in "
         "a structured way.", "engineering"),
        ("We need a way to flag docs that are already outdated.",
         "engineering"),
        ("Track review status, and never resend a doc to a team "
         "that already declined it.", "both"),
    ]
    for text, want in kind_cases:
        got = classify_kind(text, use_llm=False)
        flag = "PASS" if got == want else "FAIL"
        if flag == "FAIL":
            ok = False
        print(f"  {flag}  kind={got} (want {want}) — {text[:65]}")

    print("\nselftest:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        return _selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
