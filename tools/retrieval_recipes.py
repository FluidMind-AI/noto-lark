#!/usr/bin/env python3
"""
Retrieval recipes — the self-improving layer (Phase 5).

A "recipe" = a learned mapping: question pattern -> the route/sources/
filters that produced a good answer -> the canonical answer. When the
operator thumbs-up an answer, we store a recipe so similar future
questions route the same way (consistent answers to recurring company
questions, and cheaper than re-deriving the route).

Implementation reuses the existing engine with NO changes:
  - stored as a `pattern`-typed memory, tags=["retrieval-recipe"], in
    the company memory index
  - topic_key = recipe:<normalized-question-hash> so repeated good
    answers reinforce (confidence ↑) and stale ones supersede
  - decay/promotion = the existing review_after / promote machinery

This sits ON TOP of the Phase 3 router; it never replaces it.

CLI:
  python tools/retrieval_recipes.py selftest
  python tools/retrieval_recipes.py add "<question>" "<route>" "<answer>"
  python tools/retrieval_recipes.py match "<question>"
"""

import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_path  # noqa: E402


def _store_path() -> str:
    # Company-namespaced, lives beside the indexes (git-ignored).
    return os.path.join(os.path.dirname(get_path("metadata_db")),
                        "recipes.json")


def _load_store() -> Dict[str, Any]:
    p = _store_path()
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_store(d: Dict[str, Any]) -> None:
    p = _store_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, p)

_STOP = set("a an the of to in on for is are was were be do does did how "
            "what when where which who why our we us you your it that this "
            "and or as at by with about into".split())


def normalize(q: str) -> str:
    toks = re.findall(r"[a-z0-9]+", q.lower())
    return " ".join(t for t in toks if t not in _STOP and len(t) > 1)


def _key(q: str) -> str:
    return "recipe:" + hashlib.sha1(normalize(q).encode()).hexdigest()[:16]


def add_recipe(question: str, route: str, answer: str,
               sources: Optional[List[str]] = None) -> Dict[str, Any]:
    """Store/reinforce a recipe. Idempotent via topic_key."""
    key = _key(question)
    # 1) JSON sidecar = reliable match source of truth (lexical engine is
    #    AND-ish; we don't fight it for exact recipe recall).
    store = _load_store()
    rec = store.get(key, {"hits": 0})
    rec.update({
        "q_pattern": normalize(question),
        "route": route,
        "answer": answer[:1500],
        "sources": sources or [],
    })
    rec["hits"] = rec.get("hits", 0) + 1
    store[key] = rec
    _save_store(store)

    # 2) Also reinforce in the memory engine so decay/promotion
    #    (review_after, promote, confidence) applies — reuse, no fork.
    action = "created"
    try:
        from memory_indexer import MemoryIndex
        mi = MemoryIndex()
        try:
            res = mi.add_memory(
                content=(f"RECIPE\nQ_PATTERN: {normalize(question)}\n"
                         f"ROUTE: {route}\nANSWER: {answer[:600]}"),
                memory_type="pattern",
                tags=["retrieval-recipe", f"route:{route}", "company"],
                source="qa-feedback", topic_key=key,
            )
            action = res.get("action", "created")
        finally:
            mi.close()
    except Exception:
        pass
    return {"action": action, "key": key, "route": route,
            "hits": rec["hits"]}


def match_recipe(question: str, min_overlap: float = 0.7
                 ) -> Optional[Dict[str, Any]]:
    """Return the best recipe for a question, or None. Uses token overlap
    on the normalized pattern (engine is lexical-only; this is robust and
    cheap). A hit lets the router skip re-derivation."""
    qtokens = set(normalize(question).split())
    if not qtokens:
        return None
    best, best_score = None, 0.0
    for rec in _load_store().values():
        patt = set((rec.get("q_pattern") or "").split())
        if not patt:
            continue
        overlap = len(qtokens & patt) / len(qtokens | patt)  # Jaccard
        if overlap > best_score:
            best, best_score = rec, overlap
    # Guards (2026-07 accuracy review #6): (a) higher overlap bar —
    # at 0.5, a 'pay scale hong kong' question matched the SINGAPORE
    # recipe; (b) any proper-noun-ish token in the question must also
    # appear in the recipe pattern (location/entity swaps break the
    # match); (c) never serve the '(see prior answer)' placeholder.
    if best and best_score >= min_overlap:
        import re as _re
        q_proper = {w.lower() for w in _re.findall(
            r"\b[A-Z][a-z]{2,}\b", question)}
        pat_l = (best.get("q_pattern") or "").lower()
        if any(p not in pat_l for p in q_proper):
            return None
        if "(see prior answer)" in (best.get("answer") or ""):
            return None
    if best and best_score >= min_overlap:
        return {
            "matched": True,
            "overlap": round(best_score, 2),
            "route": best.get("route"),
            "answer": best.get("answer", ""),
            "q_pattern": best.get("q_pattern", ""),
            "hits": best.get("hits", 1),
        }
    return None


def _selftest() -> int:
    ok = True
    from config import get_path
    # isolate: use the real company index but a unique question so we
    # don't collide with anything; clean our key after.
    q = "what is the zzqq referral bonus policy unique12345"
    r = add_recipe(q, "document", "Referral bonus is S$2000 after 90 days.",
                   sources=["doc:policy"])
    if r["action"] in ("created", "reinforced") and r["route"] == "document":
        print(f"PASS: add_recipe ({r['action']})")
    else:
        print("FAIL: add_recipe", r); ok = False

    # reinforce (same question -> same topic_key)
    r2 = add_recipe(q, "document", "Referral bonus is S$2000 after 90 days.")
    if r2["key"] == r["key"]:
        print("PASS: recipe idempotent (stable topic_key)")
    else:
        print("FAIL: topic_key not stable"); ok = False

    m = match_recipe("zzqq referral bonus policy unique12345")
    if m and m["matched"] and m["route"] == "document":
        print(f"PASS: match_recipe (overlap={m['overlap']}, "
              f"route={m['route']})")
    else:
        print(f"FAIL: match_recipe -> {m}"); ok = False

    if match_recipe("completely unrelated airplane maintenance") is None:
        print("PASS: no false-positive match on unrelated question")
    else:
        print("FAIL: false positive match"); ok = False

    # cleanup our test recipe from the JSON sidecar
    try:
        store = _load_store()
        store.pop(_key(q), None)
        _save_store(store)
    except Exception:
        pass

    print("\nselftest:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


def main() -> int:
    import argparse, json
    p = argparse.ArgumentParser(description="Retrieval recipes — Noto")
    sub = p.add_subparsers(dest="cmd")
    a = sub.add_parser("add")
    a.add_argument("question"); a.add_argument("route")
    a.add_argument("answer")
    mt = sub.add_parser("match"); mt.add_argument("question")
    sub.add_parser("selftest")
    args = p.parse_args()
    if args.cmd == "add":
        print(json.dumps(add_recipe(args.question, args.route, args.answer)))
        return 0
    if args.cmd == "match":
        print(json.dumps(match_recipe(args.question), default=str, indent=2))
        return 0
    if args.cmd == "selftest":
        return _selftest()
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
