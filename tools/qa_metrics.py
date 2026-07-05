#!/usr/bin/env python3
"""
Q&A metrics (Phase 5) — the objective health view of the bot.

Mines brain/qa-playbook.md (routing decisions + operator feedback) and
the company memory index (recipe count) to report:
  - total interactions, route distribution
  - feedback 👍/👎 counts and ratio
  - top routes lacking feedback (candidate areas to improve)
  - learned recipe count

Feeds the periodic review job (Phase 5) and the eval gate (Phase 3g):
a drop in 👍 ratio or recall regression should block promoting a sync.

CLI:
  python tools/qa_metrics.py            # human summary
  python tools/qa_metrics.py --json
  python tools/qa_metrics.py selftest
"""

import json
import os
import re
import sys
from collections import Counter
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_home  # noqa: E402

PLAYBOOK = os.path.join(get_home(), "brain", "qa-playbook.md")


def compute(playbook: str = PLAYBOOK) -> Dict[str, Any]:
    routes = Counter()
    fb = Counter()
    n = 0
    if os.path.exists(playbook):
        with open(playbook) as f:
            for ln in f:
                m = re.match(r"##\s+\S+\s+·\s+route:(\S+)", ln.strip())
                if m:
                    n += 1
                    routes[m.group(1)] += 1
                fm = re.match(r"-\s*feedback:\s*([+\-👍👎])", ln.strip())
                if fm:
                    s = fm.group(1)
                    fb["up" if s in ("+", "👍") else "down"] += 1

    recipes = 0
    try:
        from memory_indexer import MemoryIndex
        mi = MemoryIndex()
        try:
            recipes = len(mi.find("recipe", limit=200,
                                  memory_type="pattern"))
        finally:
            mi.close()
    except Exception:
        pass

    total_fb = fb["up"] + fb["down"]
    return {
        "interactions": n,
        "route_distribution": dict(routes),
        "feedback": {"up": fb["up"], "down": fb["down"],
                     "ratio": round(fb["up"] / total_fb, 3)
                     if total_fb else None},
        "feedback_coverage": round(total_fb / n, 3) if n else 0.0,
        "learned_recipes": recipes,
        "routes_without_feedback": [r for r in routes
                                    if r not in ("aggregate",)] if not
        total_fb else [],
    }


def render(m: Dict[str, Any]) -> str:
    lines = [
        "=== Noto Q&A Metrics ===",
        f"interactions       : {m['interactions']}",
        f"route distribution : {m['route_distribution']}",
        f"feedback           : 👍 {m['feedback']['up']}  "
        f"👎 {m['feedback']['down']}  "
        f"ratio={m['feedback']['ratio']}",
        f"feedback coverage  : {m['feedback_coverage']}",
        f"learned recipes    : {m['learned_recipes']}",
    ]
    if m["interactions"] == 0:
        lines.append("(no interactions yet — metrics populate once the "
                      "bot is live)")
    return "\n".join(lines)


def _selftest() -> int:
    ok = True
    import tempfile
    sample = (
        "header\n---\n"
        "## 2026-05-20T01:00:00 · route:document\n"
        "- question: how do we onboard\n- route: document (semantic)\n"
        "## 2026-05-20T01:01:00 · route:aggregate\n"
        "- question: how many docs indexed\n- route: aggregate (sql)\n"
        "- feedback: + good answer\n"
        "## 2026-05-20T01:02:00 · route:document\n"
        "- question: pto policy\n- route: document (semantic)\n"
        "- feedback: 👎 wrong doc\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(sample)
        path = f.name
    m = compute(path)
    os.unlink(path)

    if m["interactions"] == 3:
        print("PASS: interaction count")
    else:
        print(f"FAIL: interactions {m['interactions']}"); ok = False
    if m["route_distribution"].get("document") == 2 \
            and m["route_distribution"].get("aggregate") == 1:
        print("PASS: route distribution")
    else:
        print(f"FAIL: routes {m['route_distribution']}"); ok = False
    if m["feedback"]["up"] == 1 and m["feedback"]["down"] == 1 \
            and m["feedback"]["ratio"] == 0.5:
        print("PASS: feedback parse (+/- and 👍/👎)")
    else:
        print(f"FAIL: feedback {m['feedback']}"); ok = False

    print(render(m))
    print("\nselftest:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Q&A metrics — Noto")
    p.add_argument("--json", action="store_true")
    p.add_argument("cmd", nargs="?")
    a = p.parse_args()
    if a.cmd == "selftest":
        return _selftest()
    m = compute()
    print(json.dumps(m, indent=2) if a.json else render(m))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
