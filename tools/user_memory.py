#!/usr/bin/env python3
"""
Per-user persistent memory for the Noto Lark bot.

Each user who DMs the bot gets their own directory at
`memory/user-memory/<open_id>/` containing one `MEMORY.md` index plus
N fact files (frontmatter + body), mirroring the operator personal-
memory pattern (a MEMORY.md index plus one file per fact). The bot reads this into prompts ONLY for DMs (chat_type ==
"p2p") and writes to it ONLY from DMs. Group chats never touch this
memory — eliminates any chance of one user's preferences
leaking into a group answer others can see.

Four independent gates enforce segregation:
  1. `path_for()`     — only function that maps open_id -> path;
                         refuses non-DM context, malformed open_ids,
                         and any path that escapes the namespace.
  2. Caller-side      — _worker checks chat_type == "p2p" before
                         calling any read/write helper here.
  3. `maybe_remember` — re-checks the chat_type kwarg before doing
                         any LLM work.
  4. `assert_recruiter_memory_isolated()` — runs at bot startup,
                         refuses to start if the on-disk tree
                         contains anything that isn't a properly-
                         shaped open_id directory, or any symlinks.

This file mirrors `tools/feedback_store.py`'s module-level shape
(plain functions, no Store class — there's no DB connection state
to manage, just files on disk).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_home  # noqa: E402


# ---------------------------------------------------------------------------
# Path safety — the chokepoint
# ---------------------------------------------------------------------------

# Lark open_ids look like "ou_" + ~32 hex chars. Be slightly lenient on
# length (Lark hasn't promised a fixed width) but strict on shape: anything
# else is a hostile input attempting to traverse.
_OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9_-]{16,80}$")


def _recruiters_root() -> str:
    return os.path.join(get_home(), "memory", "user-memory")


def path_for(open_id: str, chat_type: str = "p2p") -> str:
    """Resolve `memory/user-memory/<open_id>/`. Fail-closed on every
    threat surface — this is the chokepoint, NOT a place to be lenient.

    Raises ValueError if:
      - chat_type != "p2p"  (memory is DM-only by hard rule)
      - open_id doesn't match _OPEN_ID_RE
      - the resolved realpath escapes memory/user-memory/

    Returns the absolute directory path. Does NOT create the directory."""
    if chat_type != "p2p":
        raise ValueError(
            f"user memory is DM-only (chat_type={chat_type!r})")
    if not open_id or not _OPEN_ID_RE.match(open_id):
        raise ValueError(
            f"refusing malformed open_id: {open_id!r}")
    root = _recruiters_root()
    candidate = os.path.join(root, open_id)
    real_root = os.path.realpath(root)
    real_cand = os.path.realpath(candidate)
    if real_cand != os.path.join(real_root, open_id):
        # Symlink escape attempt or path-trickery — fail loud.
        raise ValueError(
            f"refusing path that escapes user-memory root: {real_cand}")
    return candidate


def _ensure_dir(open_id: str, chat_type: str) -> str:
    p = path_for(open_id, chat_type)
    os.makedirs(p, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Fact files — frontmatter (mirrors operator personal-memory format)
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = _SLUG_RE.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:60] or "untitled"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _serialize_fact(fact: Dict[str, Any]) -> str:
    """Render a fact dict as a markdown file (frontmatter + body).
    Frontmatter intentionally written by hand (not via PyYAML) so this
    module has zero new third-party deps — the bot already depends on
    yaml for credentials, but keeping user_memory dep-free makes
    it trivially portable + selftestable."""
    md = fact.get("metadata") or {}
    lines = ["---"]
    lines.append(f"name: {fact.get('name', '')}")
    # description may contain colons/quotes — wrap in a quoted scalar
    desc = (fact.get("description") or "").replace('"', '\\"')
    lines.append(f'description: "{desc}"')
    lines.append("metadata:")
    for k in ("type", "origin", "created_at", "updated_at",
              "reinforcement_count", "source_message_id"):
        if k in md:
            v = md[k]
            if isinstance(v, str):
                v = v.replace('"', '\\"')
                lines.append(f'  {k}: "{v}"')
            else:
                lines.append(f"  {k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append((fact.get("body") or "").strip() + "\n")
    return "\n".join(lines)


def _parse_fact(path: str) -> Optional[Dict[str, Any]]:
    """Parse a fact file. Returns None for files without frontmatter
    so audit-all can flag them; tolerates value parsing edge cases.

    Hand-rolled rather than YAML because frontmatter shape is fixed
    and predictable (we author it ourselves in _serialize_fact)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    front = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    fact: Dict[str, Any] = {"metadata": {}, "body": body.strip()}
    section = fact
    for raw in front.splitlines():
        if not raw.strip():
            continue
        if raw.startswith("  "):
            # nested under last `metadata:`
            k, _, v = raw[2:].partition(":")
            section[k.strip()] = _coerce(v.strip())
        else:
            k, _, v = raw.partition(":")
            key = k.strip()
            val = v.strip()
            if key == "metadata":
                section = fact["metadata"]
            else:
                fact[key] = _coerce(val)
                section = fact
    return fact


def _coerce(v: str) -> Any:
    """Parse a frontmatter value. Strips wrapping quotes, falls back
    to string if not numeric/bool."""
    s = v.strip()
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1].replace('\\"', '"')
    if s.isdigit():
        return int(s)
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    return s


# ---------------------------------------------------------------------------
# Index file (MEMORY.md) — derived, rebuilt after every write
# ---------------------------------------------------------------------------

def _index_path(dir_path: str) -> str:
    return os.path.join(dir_path, "MEMORY.md")


def rebuild_index(open_id: str, chat_type: str = "p2p") -> None:
    """Regenerate MEMORY.md from the current fact files. Derived state
    — never authored by hand, so it can never drift."""
    d = path_for(open_id, chat_type)
    if not os.path.isdir(d):
        return
    entries = []
    for fname in sorted(os.listdir(d)):
        if not fname.endswith(".md") or fname == "MEMORY.md":
            continue
        fact = _parse_fact(os.path.join(d, fname))
        if not fact:
            continue
        slug = fname[:-3]
        desc = fact.get("description", "") or ""
        entries.append(f"- [{slug}]({slug}.md) — {desc}")
    body = "# Memory Index\n\n" + ("\n".join(entries) if entries
                                    else "(empty)") + "\n"
    with open(_index_path(d), "w", encoding="utf-8") as f:
        f.write(body)


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------

def load_index(open_id: str, chat_type: str = "p2p") -> str:
    """Return the raw MEMORY.md text for prompt injection. Empty
    string if no memory exists yet."""
    try:
        d = path_for(open_id, chat_type)
    except ValueError:
        return ""
    p = _index_path(d)
    if not os.path.exists(p):
        return ""
    try:
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def list_facts(open_id: str) -> List[Dict[str, Any]]:
    """Return every fact for this user as a list of dicts.
    Skips the index file. Skips files without parseable frontmatter
    (audit-all surfaces those separately)."""
    try:
        d = path_for(open_id, "p2p")
    except ValueError:
        return []
    if not os.path.isdir(d):
        return []
    out = []
    for fname in sorted(os.listdir(d)):
        if not fname.endswith(".md") or fname == "MEMORY.md":
            continue
        fact = _parse_fact(os.path.join(d, fname))
        if fact:
            fact["slug"] = fname[:-3]
            out.append(fact)
    return out


def get_fact(open_id: str, slug: str) -> Optional[Dict[str, Any]]:
    try:
        d = path_for(open_id, "p2p")
    except ValueError:
        return None
    p = os.path.join(d, _slugify(slug) + ".md")
    if not os.path.exists(p):
        return None
    return _parse_fact(p)


def _score_fact(fact: Dict[str, Any], tokens: List[str]) -> float:
    """Keyword overlap score: 3 in name, 2 in description, 1 in body,
    plus a small recency tie-break. Used by load_relevant_facts."""
    n = (fact.get("name", "") or "").lower()
    d = (fact.get("description", "") or "").lower()
    b = (fact.get("body", "") or "").lower()
    score = 0.0
    for t in tokens:
        if not t:
            continue
        if t in n:
            score += 3
        if t in d:
            score += 2
        if t in b:
            score += 1
    # Recency bonus: newer = slightly higher (up to +1)
    upd = (fact.get("metadata") or {}).get("updated_at", "")
    if isinstance(upd, str) and upd:
        try:
            dt = datetime.fromisoformat(upd.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - dt).days
            score += max(0.0, 1.0 - age_days / 90.0)
        except (TypeError, ValueError):
            pass
    return score


_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "shall", "should", "can", "could", "may", "might",
    "must", "i", "you", "he", "she", "it", "we", "they", "what",
    "which", "who", "whom", "whose", "this", "that", "these", "those",
    "of", "in", "on", "at", "to", "for", "with", "by", "from", "as",
    "if", "any", "some", "no", "not", "me", "my", "your", "his", "her",
    "its", "our", "their",
}


def _tokenize(query: str) -> List[str]:
    raw = re.findall(r"[a-z0-9]+", (query or "").lower())
    return [t for t in raw if len(t) >= 3 and t not in _STOPWORDS]


def load_relevant_facts(open_id: str, query: str,
                        chat_type: str = "p2p",
                        max_n: int = 3) -> List[Dict[str, Any]]:
    facts = list_facts(open_id)
    if not facts:
        return []
    tokens = _tokenize(query)
    scored = [(_score_fact(f, tokens), f) for f in facts]
    # Always include facts with any positive score; if none, fall back
    # to the most-recently-updated facts so generic queries ("hi") still
    # get useful context.
    positives = [f for s, f in scored if s > 0]
    if positives:
        positives.sort(key=lambda f: -_score_fact(f, tokens))
        return positives[:max_n]
    # Recency fallback ONLY for short/generic queries ("hi", "thanks").
    # For a substantive question that matched nothing, injecting the 3
    # most-recent facts biased answers toward an unrelated topic the
    # user happened to touch last (2026-07 grounding review #4).
    if len(tokens) > 4:
        return []
    facts.sort(key=lambda f: (f.get("metadata") or {}).get(
        "updated_at", ""), reverse=True)
    return facts[:max_n]


def context_for_prompt(open_id: str, query: str,
                       chat_type: str = "p2p") -> str:
    """Return the prompt-ready block for `{user_context}`. Format:
    the MEMORY.md index header + bodies of the top-N relevant facts.
    Empty string if non-DM or no memory exists."""
    try:
        path_for(open_id, chat_type)
    except ValueError:
        return ""
    idx = load_index(open_id, chat_type)
    if not idx:
        return ""
    facts = load_relevant_facts(open_id, query, chat_type, max_n=3)
    parts = [idx]
    if facts:
        parts.append("\n---\n")
        for f in facts:
            parts.append(f"\n## {f.get('name','')}")
            md = f.get("metadata") or {}
            t = md.get("type", "")
            if t:
                parts.append(f"_{t}_\n")
            parts.append(f.get("body", "").strip() + "\n")
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Write log + tombstones
# ---------------------------------------------------------------------------

def _write_log_path(dir_path: str) -> str:
    return os.path.join(dir_path, ".write_log.jsonl")


def _tombstones_path(dir_path: str) -> str:
    return os.path.join(dir_path, ".tombstones.jsonl")


def _append_log(dir_path: str, action: str, slug: str,
                workflow: str = "", source_excerpt: str = "") -> None:
    line = json.dumps({
        "ts": _now_iso(),
        "action": action,
        "slug": slug,
        "trigger_workflow": workflow,
        "source_excerpt": (source_excerpt or "")[:120],
    })
    try:
        with open(_write_log_path(dir_path), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[user_memory] WARN write_log failed: {e}",
              file=sys.stderr, flush=True)


def _load_recent_tombstones(dir_path: str,
                            within_days: int = 30) -> List[Dict[str, Any]]:
    """Return tombstones written within the last `within_days`.
    Used by maybe_remember to suppress re-learning recently deleted
    facts."""
    p = _tombstones_path(dir_path)
    if not os.path.exists(p):
        return []
    cutoff = time.time() - within_days * 86400
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts_s = rec.get("ts", "")
                try:
                    dt = datetime.fromisoformat(ts_s.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt.timestamp() >= cutoff:
                        out.append(rec)
                except (TypeError, ValueError):
                    continue
    except OSError:
        pass
    return out


def _append_tombstone(dir_path: str, slug: str, description: str) -> None:
    line = json.dumps({
        "ts": _now_iso(), "slug": slug, "description": description,
    })
    try:
        with open(_tombstones_path(dir_path), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[user_memory] WARN tombstone failed: {e}",
              file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Confidential-info post-filter
# ---------------------------------------------------------------------------

# Any of these patterns in a proposed fact body causes the write to be
# silently dropped (with a stderr log line). Better to forget a useful
# fact than store confidential personal data in plaintext.
# Currency coverage is deliberately international: $ (US/SG/HK
# /AU), £, €, plus the ISO codes USD/SGD/HKD/GBP/EUR/AUD/JPY/RMB/CNY
# users actually type when discussing compensation. Code-review
# finding (PR #4) — the previous $-only pattern missed every non-US
# currency.
_CURRENCY_SYMBOLS = r"\$|£|€|¥|HK\$|S\$|A\$"
_CURRENCY_CODES = (r"USD|SGD|HKD|GBP|EUR|AUD|JPY|CNY|RMB|CHF|CAD|"
                   r"NZD|MYR|THB|IDR|PHP|VND|INR|KRW|TWD")
_CONFIDENTIAL_PATTERNS = [
    # "$215,628" / "£300k" / "SGD 850,000" — comp-shaped numbers near
    # any currency marker, in either order. Tight enough that ordinary
    # "1,000 candidates" doesn't false-positive (no currency context).
    re.compile(
        rf"(?:{_CURRENCY_SYMBOLS})\s?\d{{1,3}}(?:[,.]?\d{{3}})+(?:\.\d+)?"
        rf"|\d{{1,3}}(?:[,.]?\d{{3}})+\s?(?:{_CURRENCY_SYMBOLS})"
        rf"|\b(?:{_CURRENCY_CODES})\s?\d{{2,3}}(?:[,.]?\d{{3}})+(?:\.\d+)?\b"
        rf"|\b\d{{1,3}}(?:[,.]?\d{{3}})+\s?(?:{_CURRENCY_CODES})\b"
        # "300k" / "850k" / "1.2m" preceded by currency marker
        rf"|(?:{_CURRENCY_SYMBOLS})\s?\d{{1,4}}(?:\.\d+)?\s?[kKmM]\b"
        rf"|\b(?:{_CURRENCY_CODES})\s?\d{{1,4}}(?:\.\d+)?\s?[kKmM]\b",
        re.I,
    ),
    re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"),    # phone-shaped
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),  # email
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                # SSN-shaped
    # very long bodies suggest a résumé was pasted
    # — checked separately on length
]


def _is_confidential_shaped(body: str) -> bool:
    if not body:
        return False
    if len(body) > 1200:
        return True
    for pat in _CONFIDENTIAL_PATTERNS:
        if pat.search(body):
            return True
    return False


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------

def write_fact(open_id: str, fact: Dict[str, Any],
               chat_type: str = "p2p",
               workflow: str = "",
               source_excerpt: str = "") -> Optional[str]:
    """Write or reinforce a fact. Returns the slug written (or None if
    skipped). Slug-collision = reinforcement, not duplicate file:
    bumps reinforcement_count, refreshes updated_at, appends the new
    body to the existing body separated by '\n\n---\n\n'.

    Confidential-shape filter applies — bodies containing comp-shaped,
    contact-shaped, or résumé-shaped content are dropped."""
    body = (fact.get("body") or "").strip()
    if not body:
        return None
    if _is_confidential_shaped(body):
        print(f"[user_memory] dropped — confidential-shaped content "
              f"in proposed fact for {open_id} "
              f"(name={fact.get('name','?')!r})",
              file=sys.stderr, flush=True)
        return None

    d = _ensure_dir(open_id, chat_type)
    slug = _slugify(fact.get("name") or "")
    path = os.path.join(d, slug + ".md")
    now = _now_iso()

    if os.path.exists(path):
        # Reinforcement update — same slug means same concept.
        existing = _parse_fact(path) or {}
        md = existing.get("metadata") or {}
        md["updated_at"] = now
        md["reinforcement_count"] = int(md.get("reinforcement_count", 1)) + 1
        # Append the new body as a reinforcement turn — never silently
        # overwrite. Operator can compress via CLI edit later.
        merged_body = (
            (existing.get("body", "") or "").rstrip()
            + "\n\n---\n\n" + body
        )
        out = {
            "name": existing.get("name") or fact.get("name") or slug,
            "description": (existing.get("description")
                            or fact.get("description") or ""),
            "metadata": md,
            "body": merged_body,
        }
        with open(path, "w", encoding="utf-8") as f:
            f.write(_serialize_fact(out))
        _append_log(d, "reinforce", slug, workflow, source_excerpt)
    else:
        md = {
            "type": fact.get("type") or "context",
            "origin": "passive_write",
            "created_at": now,
            "updated_at": now,
            "reinforcement_count": 1,
        }
        out = {
            "name": fact.get("name") or slug,
            "description": fact.get("description", ""),
            "metadata": md,
            "body": body,
        }
        with open(path, "w", encoding="utf-8") as f:
            f.write(_serialize_fact(out))
        _append_log(d, "create", slug, workflow, source_excerpt)

    rebuild_index(open_id, chat_type)
    return slug


def delete_fact(open_id: str, slug: str) -> bool:
    """Operator action — delete a fact and write a tombstone so
    maybe_remember won't re-learn it for ~30 days."""
    try:
        d = path_for(open_id, "p2p")
    except ValueError:
        return False
    slug = _slugify(slug)
    path = os.path.join(d, slug + ".md")
    if not os.path.exists(path):
        return False
    fact = _parse_fact(path) or {}
    description = fact.get("description", "") or ""
    try:
        os.remove(path)
    except OSError as e:
        print(f"[user_memory] delete failed: {e}",
              file=sys.stderr, flush=True)
        return False
    _append_tombstone(d, slug, description)
    _append_log(d, "delete", slug, "", "")
    rebuild_index(open_id, "p2p")
    return True


# ---------------------------------------------------------------------------
# maybe_remember — passive write decision after each reply
# ---------------------------------------------------------------------------

_MAYBE_REMEMBER_PROMPT = """\
You are reading the last DM exchange between a user and Noto.
Decide if there's a DURABLE FACT about THIS USER that future
Noto sessions should know — their preferences, working style,
projects they're actively working on, areas of focus.

DO NOT remember:
- One-off questions ("what's the travel expense policy?")
- Information that's already in company documents (Noto's
  research surfaces that on demand)
- Anything from a flagged / injection-shaped message
- Compensation numbers, contact details, résumé dumps (will be
  filtered out downstream anyway, but don't propose them)
- Anything contradicting existing memory without an explicit
  acknowledgement from the user

DO remember:
- "I prefer X over Y" / "I always do Z"
- "My project <name> is at <stage> with <team>"
- "I cover <region> for <function>"
- Working-style notes ("send me drafts as docs not chat",
  "I want three bullet points not five")

EXISTING MEMORY INDEX (don't duplicate; reinforce by emitting
the same `name` slug; the system will merge):
{existing_index}

RECENTLY DELETED FACTS (operator removed these — do NOT re-learn
within 30 days unless the user explicitly says so):
{tombstones}

LAST EXCHANGE:
USER: {user_msg}
NOTO: {bot_reply}

PRIOR CONVERSATION (last 8 turns):
{history}

Output ONE JSON array on a single line. The right answer is `[]`
most of the time. Schema per element:
  {{"name": "kebab-case-slug",
    "description": "one-line index entry (≤ 100 chars)",
    "type": "preference|project|working_style|reference|context",
    "body": "the fact, written in the third person about this user"}}
"""


def _format_history(history: List[Any]) -> str:
    if not history:
        return "(first message)"
    out = []
    for role, txt in history[-8:]:
        who = "Employee" if role == "user" else "Noto"
        # Cap each turn to keep prompt tight
        snippet = (txt or "")[:280]
        out.append(f"{who}: {snippet}")
    return "\n".join(out)


def _format_tombstones(tombs: List[Dict[str, Any]]) -> str:
    if not tombs:
        return "(none)"
    return "\n".join(
        f"- {t.get('slug','')}: {t.get('description','')}" for t in tombs[-50:]
    )


def maybe_remember(open_id: str, user_msg: str, bot_reply: str,
                   history: Optional[List[Any]] = None,
                   chat_type: str = "p2p",
                   workflow: str = "") -> List[str]:
    """Passive write decision. Calls Claude once, parses defensively,
    writes proposed facts through `write_fact` (which applies
    confidential filter + reinforcement). Returns list of slugs
    that were written (or reinforced). Empty list is the right
    answer most of the time.

    Best-effort: any exception is caught and logged but never
    propagated to the caller — a memory write failure must never
    affect the user-facing reply."""
    if chat_type != "p2p":
        return []
    if not open_id or not user_msg:
        return []

    try:
        # Existing memory + tombstones build the LLM's prompt context.
        existing_index = load_index(open_id, chat_type) or "(empty)"
        try:
            d = path_for(open_id, chat_type)
            tombs = (_load_recent_tombstones(d) if os.path.isdir(d) else [])
        except ValueError:
            return []

        prompt = _MAYBE_REMEMBER_PROMPT.format(
            existing_index=existing_index,
            tombstones=_format_tombstones(tombs),
            user_msg=user_msg[:1500],
            bot_reply=(bot_reply or "")[:1500],
            history=_format_history(history or []),
        )

        # Lazy import — noto_research is heavy; only load when needed.
        from noto_research import _claude  # type: ignore
        raw = _claude(prompt, timeout=30, web=False)
        if not raw:
            return []

        # Defensive JSON parse — mirror noto_research.classify_message
        proposed: List[Dict[str, Any]] = []
        try:
            proposed = json.loads(raw)
            if not isinstance(proposed, list):
                proposed = []
        except Exception:
            m = re.search(r"\[[^\]]*\]", raw, re.DOTALL)
            if m:
                try:
                    j = json.loads(m.group(0))
                    if isinstance(j, list):
                        proposed = j
                except Exception:
                    pass

        if not proposed:
            return []

        # Suppress anything that matches a recent tombstone.
        tomb_slugs = {t.get("slug", "") for t in tombs}

        written: List[str] = []
        for fact in proposed:
            if not isinstance(fact, dict):
                continue
            name = fact.get("name") or ""
            slug = _slugify(name)
            if slug in tomb_slugs:
                print(f"[user_memory] tombstone suppressed: "
                      f"{open_id} {slug}", file=sys.stderr, flush=True)
                continue
            result = write_fact(
                open_id, fact,
                chat_type=chat_type,
                workflow=workflow,
                source_excerpt=user_msg,
            )
            if result:
                written.append(result)
        return written
    except Exception as e:
        print(f"[user_memory] maybe_remember error: {e}",
              file=sys.stderr, flush=True)
        return []


# ---------------------------------------------------------------------------
# Summary helpers (for the dashboard)
# ---------------------------------------------------------------------------

def summary(open_id: str) -> Dict[str, Any]:
    """Lightweight stats for the dashboard overview table."""
    try:
        d = path_for(open_id, "p2p")
    except ValueError:
        return {"fact_count": 0, "last_write_ts": None, "preview": []}
    facts = list_facts(open_id)
    last_ts = None
    if os.path.isdir(d):
        wp = _write_log_path(d)
        if os.path.exists(wp):
            try:
                last_ts = os.path.getmtime(wp)
            except OSError:
                pass
    preview = [
        {"slug": f.get("slug", ""),
         "description": f.get("description", "")}
        for f in facts[:5]
    ]
    return {
        "fact_count": len(facts),
        "last_write_ts": last_ts,
        "preview": preview,
    }


def all_recruiters_with_memory() -> List[str]:
    """Return open_ids that currently have at least one fact on disk.
    Used by the dashboard overview."""
    root = _recruiters_root()
    if not os.path.isdir(root):
        return []
    out = []
    for entry in sorted(os.listdir(root)):
        full = os.path.join(root, entry)
        if not os.path.isdir(full):
            continue
        if not _OPEN_ID_RE.match(entry):
            continue
        # Has at least one fact file (ignore MEMORY.md / write log)?
        has_fact = any(
            f.endswith(".md") and f != "MEMORY.md"
            for f in os.listdir(full)
        )
        if has_fact:
            out.append(entry)
    return out


def tail_write_log(open_id: str, n: int = 20) -> List[Dict[str, Any]]:
    try:
        d = path_for(open_id, "p2p")
    except ValueError:
        return []
    p = _write_log_path(d)
    if not os.path.exists(p):
        return []
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        pass
    return out[-n:]


# ---------------------------------------------------------------------------
# Startup invariant — refuses to serve in a leaky state
# ---------------------------------------------------------------------------

def assert_recruiter_memory_isolated() -> None:
    """Walk memory/user-memory/ and fail loud if it contains anything
    that isn't a properly-shaped open_id subdir, or any symlinks
    anywhere in the tree.

    Mirrors `assert_company_only()` at lark_bot.py:42 and
    `assert_no_lark_delete()` at lark_client.py:76 — same idiom: a
    hard safety invariant checked at startup so a misconfigured tree
    never gets served."""
    root = _recruiters_root()
    if not os.path.isdir(root):
        return    # nothing to validate yet; fine
    real_root = os.path.realpath(root)
    for entry in os.listdir(root):
        full = os.path.join(root, entry)
        # No symlinks at the top level.
        if os.path.islink(full):
            raise RuntimeError(
                f"USER MEMORY SAFETY: symlink at top-level: "
                f"{full} — refusing to start.")
        # Only proper open_id-shaped subdirs allowed.
        if not os.path.isdir(full):
            raise RuntimeError(
                f"USER MEMORY SAFETY: non-directory at top-level: "
                f"{full} — refusing to start.")
        if not _OPEN_ID_RE.match(entry):
            raise RuntimeError(
                f"USER MEMORY SAFETY: top-level entry doesn't match "
                f"open_id shape: {entry!r} — refusing to start.")
        # Walk subdir: no symlinks anywhere.
        for sub_root, dirs, files in os.walk(full):
            for name in dirs + files:
                p = os.path.join(sub_root, name)
                if os.path.islink(p):
                    raise RuntimeError(
                        f"USER MEMORY SAFETY: symlink inside "
                        f"user dir: {p} — refusing to start.")
            # Subdir realpath must stay under root.
            real_sub = os.path.realpath(sub_root)
            if not real_sub.startswith(real_root + os.sep) \
                    and real_sub != real_root + os.sep + entry \
                    and real_sub != os.path.join(real_root, entry):
                raise RuntimeError(
                    f"USER MEMORY SAFETY: directory escapes root: "
                    f"{real_sub} — refusing to start.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _selftest() -> int:
    """Round-trip + path-safety + isolation checks. Runs in CI."""
    import tempfile
    import shutil

    fails = 0
    def _t(label, ok):
        nonlocal fails
        if not ok:
            print(f"FAIL: {label}")
            fails += 1
        else:
            print(f"PASS: {label}")

    # Use a tmp HOME so we don't touch the real user-memory dir
    tmp = tempfile.mkdtemp(prefix="rmtest-")
    try:
        os.environ["LOLABOT_HOME"] = tmp
        # Force config reload
        import importlib, config as _cfg
        importlib.reload(_cfg)
        from config import get_home as _gh
        _t("tmp home wired", _gh() == tmp)

        # Re-import this module so _recruiters_root sees the new home
        import user_memory as _um
        importlib.reload(_um)
        from user_memory import (
            path_for as _pf, write_fact as _wf,
            list_facts as _lf, get_fact as _gf,
            context_for_prompt as _cfp, delete_fact as _df,
            assert_recruiter_memory_isolated as _ari,
            _serialize_fact, _parse_fact, _slugify, _is_confidential_shaped,
        )

        # T1: path_for refuses non-DM
        try:
            _pf("ou_legit_user_for_testxx", "group_chat")
            _t("non-DM rejected", False)
        except ValueError:
            _t("non-DM rejected", True)

        # T2: path_for refuses malformed open_id
        for bad in ["../../etc/passwd", "not-an-open-id", "", "ou_", "ou_../x"]:
            try:
                _pf(bad, "p2p")
                _t(f"malformed open_id rejected: {bad!r}", False)
            except ValueError:
                _t(f"malformed open_id rejected: {bad!r}", True)

        # T3: valid input returns expected path
        ALICE = "ou_alicealicealiceaaa"
        p = _pf(ALICE, "p2p")
        _t("valid path under user-memory/", os.path.realpath(p).startswith(
            os.path.realpath(os.path.join(tmp, "memory", "user-memory"))))

        # T4: write + list round-trip
        slug = _wf(ALICE, {
            "name": "Weekly report focus",
            "description": "Prefers concise weekly metrics reports for the SG office",
            "type": "preference",
            "body": "Alice focuses on the weekly metrics report for the SG office.",
        }, chat_type="p2p", workflow="q_and_a", source_excerpt="...")
        _t("write_fact returns slug", slug == "weekly-report-focus")
        facts = _lf(ALICE)
        _t("list_facts returns 1 entry", len(facts) == 1)
        f = _gf(ALICE, slug)
        _t("get_fact reads it back",
            f and "Alice focuses" in (f.get("body") or ""))

        # T5: cross-user reads return nothing
        BOB = "ou_bobbobbobbobbobbob"
        _t("cross-user list returns []", _lf(BOB) == [])
        _t("cross-user context returns ''",
            _cfp(BOB, "alice's question", "p2p") == "")

        # T6: slug collision → reinforcement
        _wf(ALICE, {
            "name": "Weekly report focus",
            "description": "Same fact reinforced",
            "type": "preference",
            "body": "Confirmed again: SG weekly report focus.",
        }, chat_type="p2p")
        f2 = _gf(ALICE, slug)
        rc = (f2.get("metadata") or {}).get("reinforcement_count")
        _t(f"reinforcement_count == 2 (got {rc})", rc == 2)
        _t("still only one file",
            len([x for x in os.listdir(_pf(ALICE, "p2p"))
                 if x.endswith(".md") and x != "MEMORY.md"]) == 1)

        # T7: confidential filter — currency coverage matters for
        # international teams (review finding on PR #4).
        _t("conf: USD pattern flagged",
            _is_confidential_shaped("Earning $215,628 last year."))
        _t("conf: SGD ISO code flagged",
            _is_confidential_shaped("Salary of SGD 850,000."))
        _t("conf: HK$ symbol flagged",
            _is_confidential_shaped("Base HK$1,200,000 plus bonus."))
        _t("conf: £ short-form 'k' flagged",
            _is_confidential_shaped("Asking £450k base."))
        _t("conf: EUR symbol flagged",
            _is_confidential_shaped("Compensation €600,000 all-in."))
        _t("conf: email flagged",
            _is_confidential_shaped("Contact: foo@bar.com"))
        _t("conf: short clean body NOT flagged",
            not _is_confidential_shaped("Prefers concise weekly reports."))
        _t("conf: plain count NOT flagged ('1,000 documents')",
            not _is_confidential_shaped("Reviewed 1,000 documents today."))
        conf_slug = _wf(ALICE, {
            "name": "salary-info",
            "description": "Pay confidential test",
            "type": "context",
            "body": "John Smith earned $215,628 base last year.",
        }, chat_type="p2p")
        _t("confidential fact dropped at write_fact", conf_slug is None)

        # T8: delete + tombstone
        _t("delete_fact returns True", _df(ALICE, slug) is True)
        _t("fact gone after delete", _gf(ALICE, slug) is None)
        tomb_path = os.path.join(_pf(ALICE, "p2p"), ".tombstones.jsonl")
        _t("tombstone file written", os.path.exists(tomb_path))

        # T9: assert_recruiter_memory_isolated — happy path
        _ari()
        _t("isolation assertion passes on clean tree", True)

        # T10: isolation — bad top-level file
        bad_file = os.path.join(tmp, "memory", "user-memory", "random.txt")
        with open(bad_file, "w") as f: f.write("nope")
        try:
            _ari()
            _t("isolation rejects non-dir at top-level", False)
        except RuntimeError:
            _t("isolation rejects non-dir at top-level", True)
        os.remove(bad_file)

        # T11: isolation — symlink rejected
        sym = os.path.join(tmp, "memory", "user-memory", "ou_symlinksymlinksymlinkk")
        os.symlink("/etc", sym)
        try:
            _ari()
            _t("isolation rejects top-level symlink", False)
        except RuntimeError:
            _t("isolation rejects top-level symlink", True)
        os.unlink(sym)

    finally:
        del os.environ["LOLABOT_HOME"]
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'selftest: ALL PASS' if not fails else f'{fails} FAILED'}")
    return 0 if not fails else 1


def _audit_all() -> int:
    """Walk every user dir, flag orphans / index mismatches /
    parse failures. Exit nonzero on any inconsistency."""
    root = _recruiters_root()
    if not os.path.isdir(root):
        print("(no memory/user-memory/ yet — nothing to audit)")
        return 0
    fails = 0
    for entry in sorted(os.listdir(root)):
        full = os.path.join(root, entry)
        if not os.path.isdir(full) or not _OPEN_ID_RE.match(entry):
            print(f"INVALID top-level entry: {entry}")
            fails += 1
            continue
        # Parse each fact file
        files = [f for f in os.listdir(full)
                 if f.endswith(".md") and f != "MEMORY.md"]
        for f in files:
            p = os.path.join(full, f)
            fact = _parse_fact(p)
            if not fact:
                print(f"PARSE FAIL: {entry}/{f}")
                fails += 1
                continue
            if not fact.get("name"):
                print(f"MISSING name in frontmatter: {entry}/{f}")
                fails += 1
        # Index references match files?
        idx_p = _index_path(full)
        if os.path.exists(idx_p):
            with open(idx_p, encoding="utf-8") as f:
                idx = f.read()
            for slug in re.findall(r"- \[([\w-]+)\]", idx):
                if not os.path.exists(os.path.join(full, slug + ".md")):
                    print(f"INDEX REFERENCES MISSING FILE: {entry}/{slug}.md")
                    fails += 1
        print(f"  {entry}: {len(files)} facts")
    print(f"\n{'audit: clean' if not fails else f'{fails} ISSUES'}")
    return 0 if not fails else 1


def _main(argv: List[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 0
    cmd = argv[0]
    if cmd == "selftest":
        return _selftest()
    if cmd == "audit-all":
        return _audit_all()
    if cmd == "list" and len(argv) >= 2:
        for f in list_facts(argv[1]):
            print(f"  [{f.get('slug')}] {f.get('description','')}")
        return 0
    if cmd == "show" and len(argv) >= 3:
        f = get_fact(argv[1], argv[2])
        if not f:
            print("(not found)"); return 1
        print(_serialize_fact(f))
        return 0
    if cmd == "delete" and len(argv) >= 3:
        ok = delete_fact(argv[1], argv[2])
        print("deleted" if ok else "not found")
        return 0 if ok else 1
    print(f"unknown: {cmd}", file=sys.stderr)
    print("usage: list <open_id> | show <open_id> <slug> | "
          "delete <open_id> <slug> | audit-all | selftest",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
