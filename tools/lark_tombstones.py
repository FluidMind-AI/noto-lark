#!/usr/bin/env python3
"""
Lark deletion tombstones — soft-delete for docs that vanish from Lark.

When a doc we had cached disappears from Lark (a user deleted it),
Noto does NOT purge its copy. Two reasons: Noto never destroys data, and
a partial/failed ingest walk must never be able to cause data loss. So a
vanished doc is TOMBSTONED instead — its cached content stays on disk,
we record that it's no longer live, and retrieval excludes it so it stops
surfacing as if current.

Reversible by design: if the token reappears in a later walk, the
tombstone is cleared (resurrected). Detection is ROOT-SCOPED and
coverage-gated (see `reconcile`) so docs under un-walked roots, or a walk
that didn't complete, never get mistaken for deletions.

This module only writes to a local index (indexes/lark_tombstones.db) and
the cache — it issues no Lark calls and no Lark-object deletes, so it is
outside assert_no_lark_delete's concern.

  indexes/lark_tombstones.db
    tombstones(token PK, kind, folder_path, last_seen_at, deleted_at,
               detect_count)
"""

import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Set

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _home() -> str:
    from config import get_home
    return get_home()


def _db_path() -> str:
    return os.path.join(_home(), "indexes", "lark_tombstones.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tombstones (
    token        TEXT PRIMARY KEY,
    kind         TEXT,            -- drive | wiki
    folder_path  TEXT,            -- where it used to live (last known)
    last_seen_at REAL,            -- best-effort: when last seen live
    deleted_at   REAL NOT NULL,   -- when first detected missing
    detect_count INTEGER NOT NULL DEFAULT 1
);
"""


def _connect() -> sqlite3.Connection:
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, timeout=30.0)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(_SCHEMA)
    db.commit()
    return db


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def mark_deleted(token: str, kind: str = "", folder_path: str = "") -> None:
    """Tombstone a token. New tombstone → records deleted_at=now. Already
    tombstoned → bumps detect_count (consecutive walks it's been gone)."""
    db = _connect()
    try:
        row = db.execute("SELECT detect_count FROM tombstones WHERE token=?",
                         (token,)).fetchone()
        if row:
            db.execute("UPDATE tombstones SET detect_count=detect_count+1, "
                       "kind=COALESCE(NULLIF(?,''),kind), "
                       "folder_path=COALESCE(NULLIF(?,''),folder_path) "
                       "WHERE token=?", (kind, folder_path, token))
        else:
            db.execute("INSERT INTO tombstones (token, kind, folder_path, "
                       "last_seen_at, deleted_at, detect_count) "
                       "VALUES (?,?,?,?,?,1)",
                       (token, kind, folder_path, time.time(), time.time()))
        db.commit()
    finally:
        db.close()
    _CACHE["at"] = 0.0          # invalidate read cache after a mutation


def mark_live(token: str) -> bool:
    """Resurrect a token (it reappeared). Returns True if it had been
    tombstoned."""
    db = _connect()
    try:
        cur = db.execute("DELETE FROM tombstones WHERE token=?", (token,))
        db.commit()
        _CACHE["at"] = 0.0      # invalidate read cache after a mutation
        return cur.rowcount > 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

_CACHE: Dict[str, Any] = {"at": 0.0, "set": frozenset()}
_TTL = 30.0


def deleted_tokens(use_cache: bool = True) -> Set[str]:
    """The set of tombstoned tokens — for retrieval exclusion. Cached for
    a few seconds so the search hot path doesn't hit SQLite every call."""
    now = time.time()
    if use_cache and (now - _CACHE["at"]) < _TTL:
        return _CACHE["set"]
    try:
        db = _connect()
        try:
            s = frozenset(r[0] for r in
                          db.execute("SELECT token FROM tombstones"))
        finally:
            db.close()
    except Exception:
        s = frozenset()
    _CACHE.update({"at": now, "set": s})
    return s


def is_deleted(token: str) -> bool:
    return token in deleted_tokens()


def list_deleted(limit: int = 1000) -> List[Dict[str, Any]]:
    db = _connect()
    try:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(
            "SELECT * FROM tombstones ORDER BY deleted_at DESC LIMIT ?",
            (limit,))]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Reconcile — the safe core
# ---------------------------------------------------------------------------

def reconcile(prior_map: Dict[str, str], encountered: Set[str],
              kind: str = "drive", min_coverage: float = 0.8,
              verbose: bool = True) -> Dict[str, Any]:
    """Compare what we KNEW lived under a root (prior_map: token→path, from
    the last ingest folder map) against what THIS walk encountered
    (`encountered`: the tokens it actually reached, successes AND
    fetch-failures — a doc that errored still exists).

      newly missing  = prior − encountered   → tombstone
      reappeared     = encountered ∩ tombstoned → resurrect

    SAFETY GATE: if the walk encountered fewer than `min_coverage` of the
    prior tokens, it almost certainly didn't complete (a folder listing
    failed, auth dropped, etc.) — we ABORT detection rather than tombstone
    a swath of still-live docs. Tombstones are reversible, but it's better
    not to create a false storm in the first place."""
    res = {"tombstoned": 0, "resurrected": 0, "skipped": False,
           "reason": ""}
    prior = set(prior_map or {})
    if not prior:
        res.update({"skipped": True, "reason": "no prior map (first walk)"})
        if verbose:
            print(f"[tombstones] {kind}: no prior map — nothing to reconcile")
        return res

    coverage = len(encountered & prior) / len(prior)
    if coverage < min_coverage:
        res.update({"skipped": True,
                    "reason": f"walk coverage {coverage:.0%} < "
                              f"{min_coverage:.0%} — likely incomplete; "
                              f"skipping to avoid false tombstones"})
        if verbose:
            print(f"[tombstones] {kind}: {res['reason']}", flush=True)
        return res

    newly_missing = prior - encountered
    for tok in newly_missing:
        mark_deleted(tok, kind=kind, folder_path=prior_map.get(tok, ""))
    res["tombstoned"] = len(newly_missing)

    # resurrection: any encountered token that was tombstoned is live again
    tombstoned_now = deleted_tokens(use_cache=False)
    for tok in (encountered & tombstoned_now):
        if mark_live(tok):
            res["resurrected"] += 1

    if verbose:
        print(f"[tombstones] {kind}: tombstoned {res['tombstoned']} newly "
              f"missing, resurrected {res['resurrected']} "
              f"(walk coverage {coverage:.0%})", flush=True)
    return res


# ---------------------------------------------------------------------------
# CLI + selftest
# ---------------------------------------------------------------------------

def _selftest() -> int:
    import tempfile
    ok = True
    # isolate to a temp db
    global _db_path
    tmp = tempfile.mkdtemp()
    orig = _db_path
    _db_path = lambda: os.path.join(tmp, "tomb.db")  # noqa: E731
    try:
        # first walk: no prior → skipped
        r = reconcile({}, {"a", "b"}, verbose=False)
        ok &= r["skipped"]; print(("PASS" if r["skipped"] else "FAIL"),
                                  "- first walk (no prior) skips")

        # b vanished, the rest present (90% coverage ≥ gate) → b tombstoned
        prior = {k: f"M&A/{k}" for k in "abcdefghij"}      # 10 docs
        present = set(prior) - {"b"}                        # 9/10 = 90%
        r = reconcile(prior, present, min_coverage=0.8, verbose=False)
        good = r["tombstoned"] == 1 and is_deleted("b") and not is_deleted("a")
        ok &= good; print(("PASS" if good else "FAIL"),
                          "- vanished doc tombstoned, live ones untouched")

        # b reappears → resurrected
        r = reconcile(prior, set(prior), min_coverage=0.8, verbose=False)
        good = r["resurrected"] == 1 and not is_deleted("b")
        ok &= good; print(("PASS" if good else "FAIL"),
                          "- reappeared doc resurrected")

        # partial walk (coverage < gate) → skip, no false tombstones
        deleted_tokens(use_cache=False)
        r = reconcile(prior, {"a", "b"}, min_coverage=0.8, verbose=False)
        good = r["skipped"] and not is_deleted("c")
        ok &= good; print(("PASS" if good else "FAIL"),
                          "- low-coverage walk skips (no false tombstones)")
    finally:
        _db_path = orig
    print("\nselftest:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 0
    cmd = argv[0]
    if cmd == "selftest":
        return _selftest()
    if cmd == "list":
        rows = list_deleted()
        print(f"{len(rows)} tombstoned doc(s):")
        for r in rows:
            import datetime as _dt
            when = _dt.datetime.fromtimestamp(r["deleted_at"]).strftime(
                "%Y-%m-%d")
            print(f"  {r['token']}  [{r['kind']}]  gone since {when}  "
                  f"(seen missing {r['detect_count']}x)  {r['folder_path']}")
        return 0
    if cmd == "stats":
        import json
        print(json.dumps({"tombstoned": len(deleted_tokens(use_cache=False))},
                         indent=2))
        return 0
    print("commands: list | stats | selftest", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
