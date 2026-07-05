#!/usr/bin/env python3
"""
Document index — proper RAG for company docs (Phase 3c–3f).

Why not just memory_indexer fact-extraction: at company scale you need
passage-level retrieval with citations, not lossy one-liners. This module:

  3c  section-aware chunking (+ overlap) with rich metadata; parent-doc
      retrieval (search small chunks, return the coherent parent passage)
  3e  VectorBackend abstraction so the engine is swappable; a scale
      spike (`bench`) measures recall/latency/size at projected volume
  3f  `compact` rebuilds the index from the lark/docs artifacts,
      dropping superseded/tombstoned vectors (Memvid is append-only)

The default backend wraps the existing Memvid V2 MemoryIndex (no engine
fork). Swapping to LanceDB/pgvector/Qdrant later = a new VectorBackend
subclass; callers don't change.

CLI:
  python tools/doc_index.py compact          # rebuild from lark/docs/*.md
  python tools/doc_index.py search "query"
  python tools/doc_index.py bench 2000        # scale spike
  python tools/doc_index.py selftest
"""

import glob
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Protocol

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_home, load_config  # noqa: E402

CHUNK_TARGET = 900       # chars; ~200-250 tokens
CHUNK_OVERLAP = 150


# ---------------------------------------------------------------------------
# Chunking (section-aware: split on markdown headings, then size-bound)
# ---------------------------------------------------------------------------

def chunk_markdown(text: str, doc_id: str, title: str) -> List[Dict[str, Any]]:
    """Return chunks with parent metadata. Heading path is preserved so a
    retrieved chunk carries its section context (better citations)."""
    lines = text.splitlines()
    sections: List[Dict[str, Any]] = []
    cur = {"heading_path": [title], "body": []}
    heading_stack: List[str] = []

    for ln in lines:
        m = re.match(r"^(#{1,6})\s+(.*)", ln)
        if m:
            if cur["body"]:
                sections.append(cur)
            level = len(m.group(1))
            heading_stack = heading_stack[: level - 1] + [m.group(2).strip()]
            cur = {"heading_path": [title] + heading_stack, "body": []}
        else:
            cur["body"].append(ln)
    if cur["body"]:
        sections.append(cur)

    chunks: List[Dict[str, Any]] = []
    ordinal = 0
    for sec in sections:
        body = "\n".join(sec["body"]).strip()
        if not body:
            continue
        start = 0
        while start < len(body):
            piece = body[start:start + CHUNK_TARGET]
            chunks.append({
                "doc_id": doc_id,
                "title": title,
                "ordinal": ordinal,
                "heading_path": " > ".join(sec["heading_path"]),
                "text": piece.strip(),
            })
            ordinal += 1
            if start + CHUNK_TARGET >= len(body):
                break
            start += CHUNK_TARGET - CHUNK_OVERLAP
    return chunks


# ---------------------------------------------------------------------------
# VectorBackend abstraction (swap engine without touching callers)
# ---------------------------------------------------------------------------

class VectorBackend(Protocol):
    def add(self, chunk: Dict[str, Any]) -> None: ...
    def search(self, query: str, k: int) -> List[Dict[str, Any]]: ...
    def reset(self) -> None: ...
    def size(self) -> int: ...


# Question/stop words stripped before lexical search so natural-language
# questions don't dilute the match (memory_indexer is lexical-only; until
# Phase 3d hybrid+rerank lands, keyword extraction is the honest baseline).
_STOP = set("a an the of to in on for is are was were be been being do does "
            "did how what when where which who whom whose why will would can "
            "could should may might our we us you your i it its that this "
            "these those and or as at by with about into".split())


def _keywords(q: str) -> str:
    toks = re.findall(r"[A-Za-z0-9$#:_-]+", q.lower())
    kept = [t for t in toks if t not in _STOP and len(t) > 1]
    return " ".join(kept) or q


def _docs_index_path() -> str:
    """Doc RAG gets its OWN company-namespaced index (separate from the
    curated memory index) — matches lolabot.yaml retrieval.docs_index."""
    rel = (load_config().get("retrieval", {}) or {}).get(
        "docs_index", "indexes/company-docs.mv2")
    return rel if os.path.isabs(rel) else os.path.join(get_home(), rel)


class MemvidBackend:
    """Default backend: Memvid V2, doc-dedicated index, direct SDK access.

    Talks to memvid_sdk directly (not through memory_indexer, whose result
    parser is tuned for one-line curated facts). We control the stored
    record + hit shape so doc_id/heading are always recoverable. This is
    the seam: a LanceDB/pgvector/Qdrant subclass swaps in here unchanged.
    """

    def __init__(self):
        self._path = _docs_index_path()
        if "personal-archive" in self._path:  # privacy guard
            raise RuntimeError("doc index path resolved outside company ns")
        self._m = None
        self._n = 0

    def _open(self, create_if_missing: bool = True):
        if self._m is not None:
            return self._m
        import memvid_sdk
        mode = "open" if os.path.exists(self._path) else "create"
        if mode == "create" and not create_if_missing:
            return None
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._m = memvid_sdk.use("basic", self._path, mode=mode,
                                 enable_vec=False, enable_lex=True)
        return self._m

    def add(self, chunk: Dict[str, Any]) -> None:
        m = self._open()
        # doc_id + heading embedded in text so they survive retrieval and
        # are matchable by keyword; tags carry structured filters.
        text = (f"doc_id:{chunk['doc_id']} | {chunk['heading_path']}\n"
                f"{chunk['text']}")
        m.put(title=f"{chunk['title']} #{chunk['ordinal']}",
              label="doc",
              text=text,
              tags=["company", "doc", f"doc:{chunk['doc_id']}",
                    f"chunk:{chunk['ordinal']}"])
        self._n += 1

    def _raw_find(self, m, q: str, k: int):
        try:
            return m.find(q, k=k, mode="auto", min_relevancy=0.0)
        except TypeError:
            return m.find(q, k=k)

    def search(self, query: str, k: int) -> List[Dict[str, Any]]:
        """Term-union lexical retrieval. Memvid 'auto' mode is AND-ish: a
        query word missing from a doc zeros the hit. So we query the full
        keyword string AND each salient term, then union by frame, summing
        scores (a BM25-ish fusion — the lexical half of Phase 3d's hybrid).
        """
        m = self._open(create_if_missing=False)
        if m is None:
            return []
        kw = _keywords(query)
        terms = [t for t in kw.split() if len(t) > 2]
        queries = [kw] + terms
        merged: Dict[str, Dict[str, Any]] = {}
        for qi in queries:
            for h in self._raw_find(m, qi, max(k * 2, 8)).get("hits", []):
                txt = h.get("text", "") or h.get("snippet", "")
                fid = str(h.get("frame_id", txt[:40]))
                rec = merged.get(fid)
                if rec is None:
                    mm = re.match(r"doc_id:(\S+)", txt)
                    rec = {
                        "doc_id": mm.group(1) if mm
                        else (h.get("title") or "unknown"),
                        "source": f"doc:{mm.group(1)}" if mm
                        else "company-docs",
                        "content": txt,
                        "score": 0.0,
                        "tags": h.get("tags", []),
                    }
                    merged[fid] = rec
                rec["score"] += float(h.get("score", 0) or 0)
        return sorted(merged.values(), key=lambda x: -x["score"])[:max(k * 2, 8)]

    def reset(self) -> None:
        self.close()
        if os.path.exists(self._path):
            os.remove(self._path)
        self._m = None
        self._n = 0

    def size(self) -> int:
        return self._n

    def close(self) -> None:
        if self._m is not None:
            try:
                self._m.close()
            except Exception:
                pass
            self._m = None


class SqliteFtsBackend:
    """SQLite FTS5 backend — no size cap, no dependency, native BM25.

    Memvid V2's free tier caps the index at 50 MB, which a 1500+ doc
    corpus blows past. FTS5 (built into Python's sqlite3) has no such cap
    and gives proper lexical BM25 ranking — strictly better here. This is
    the planned fallback (Phase 3e); the VectorBackend abstraction means
    callers don't change.
    """

    def __init__(self):
        self._path = _docs_index_path().rsplit(".", 1)[0] + ".db"
        if "personal-archive" in self._path:
            raise RuntimeError("doc index path resolved outside company ns")
        self._conn = None

    def _open(self):
        if self._conn is not None:
            return self._conn
        import sqlite3
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._conn = sqlite3.connect(self._path, timeout=30.0)
        from sqlite_utils import harden
        harden(self._conn)
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5("
            "doc_id, title, heading, body)")
        self._conn.commit()
        return self._conn

    def add(self, chunk: Dict[str, Any]) -> None:
        c = self._open()
        c.execute("INSERT INTO docs(doc_id,title,heading,body) "
                  "VALUES(?,?,?,?)",
                  (chunk["doc_id"], chunk.get("title", ""),
                   chunk.get("heading_path", ""), chunk["text"]))

    def search(self, query: str, k: int) -> List[Dict[str, Any]]:
        c = self._open()
        toks = [t for t in re.findall(r"[A-Za-z0-9]+", _keywords(query))
                if len(t) > 1]
        if not toks:
            return []
        # FTS5 OR-match across keywords; bm25() ranks (lower = better).
        ftsq = " OR ".join(toks)
        try:
            rows = c.execute(
                "SELECT doc_id,title,heading,body,bm25(docs) FROM docs "
                "WHERE docs MATCH ? ORDER BY bm25(docs) LIMIT ?",
                (ftsq, max(k * 3, 12))).fetchall()
        except Exception:
            return []
        return [{"doc_id": r[0], "source": f"doc:{r[0]}",
                 "content": f"[{r[2]}]\n{r[3]}", "title": r[1],
                 "score": -float(r[4]), "tags": []}
                for r in rows]

    def reset(self) -> None:
        c = self._open()
        c.execute("DROP TABLE IF EXISTS docs")
        c.execute("CREATE VIRTUAL TABLE docs USING fts5("
                  "doc_id, title, heading, body)")
        c.commit()

    def size(self) -> int:
        c = self._open()
        return c.execute("SELECT COUNT(*) FROM docs").fetchone()[0]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None


def get_backend(name: str = "sqlite") -> VectorBackend:
    if name == "sqlite":
        return SqliteFtsBackend()
    if name == "memvid":
        return MemvidBackend()
    raise ValueError(f"unknown backend {name!r} (add a VectorBackend subclass)")


# ---------------------------------------------------------------------------
# Parent-document retrieval
# ---------------------------------------------------------------------------

def search_parent(query: str, k: int = 6,
                  backend: Optional[VectorBackend] = None
                  ) -> List[Dict[str, Any]]:
    """Search chunks, then group hits back to their parent doc so the
    answer gets coherent context + a citation, not isolated fragments."""
    be = backend or get_backend()
    hits = be.search(query, k * 2)
    # exclude docs tombstoned as deleted-in-Lark — keep the cached data, just
    # don't surface it as live (soft-delete; see lark_tombstones)
    try:
        import lark_tombstones
        _deleted = lark_tombstones.deleted_tokens()
    except Exception:
        _deleted = frozenset()
    by_doc: Dict[str, Dict[str, Any]] = {}
    for h in hits:
        doc_id = h.get("doc_id") or "unknown"
        if doc_id in _deleted:
            continue
        d = by_doc.setdefault(doc_id, {"doc_id": doc_id, "chunks": [],
                                       "score": 0})
        d["chunks"].append(h.get("content") or h.get("text", ""))
        d["score"] += float(h.get("score", 1)) or 1
    ranked = sorted(by_doc.values(), key=lambda x: -x["score"])[:k]
    for r in ranked:
        r["passage"] = "\n…\n".join(r["chunks"][:3])
    return ranked


# ---------------------------------------------------------------------------
# Compaction: rebuild from artifacts (drops superseded/tombstoned vectors)
# ---------------------------------------------------------------------------

def _docs_glob() -> str:
    rel = (load_config().get("retrieval", {}) or {}).get(
        "lark_cache_dir", "lark")
    base = rel if os.path.isabs(rel) else os.path.join(get_home(), rel)
    return os.path.join(base, "docs", "*", "*.md")


def compact() -> int:
    be = get_backend()
    be.reset()
    files = glob.glob(_docs_glob())
    total = 0
    for fp in files:
        with open(fp) as f:
            text = f.read()
        doc_id = os.path.splitext(os.path.basename(fp))[0]
        title = text.splitlines()[0].lstrip("# ").strip() if text else doc_id
        for ch in chunk_markdown(text, doc_id, title):
            be.add(ch)
            total += 1
    be.close()
    print(f"[compact] rebuilt {total} chunk(s) from {len(files)} artifact(s)")
    return 0


# ---------------------------------------------------------------------------
# Scale spike (3e): synthetic volume → index time, query p95, size
# ---------------------------------------------------------------------------

def bench(n_docs: int) -> int:
    import statistics
    be = get_backend()
    be.reset()
    print(f"[bench] indexing {n_docs} synthetic docs…")
    t0 = time.monotonic()
    for i in range(n_docs):
        body = (f"# Doc {i}\n\n## Overview\nRole {i} at client "
                f"C{i % 50}. Skills: python, sql, reporting ops {i}.\n\n"
                f"## Notes\nProject pipeline note number {i} "
                f"about meeting scheduling and budget band {i % 7}.\n")
        for ch in chunk_markdown(body, f"d{i}", f"Doc {i}"):
            be.add(ch)
    idx_s = time.monotonic() - t0

    lat = []
    for q in ["meeting scheduling", "budget band", "python sql",
              "client C7 role", "reporting ops"]:
        for _ in range(10):
            s = time.monotonic()
            be.search(q, 6)
            lat.append((time.monotonic() - s) * 1000)
    lat.sort()
    from config import get_path
    sz = sum(os.path.getsize(get_path(k))
             for k in ("long_term_index", "metadata_db")
             if os.path.exists(get_path(k))) / 1e6

    print(f"[bench] n_docs={n_docs} chunks={be.size()}")
    print(f"  index time: {idx_s:.1f}s ({be.size()/max(idx_s,1e-9):.0f} chunks/s)")
    print(f"  query latency ms: p50={statistics.median(lat):.1f} "
          f"p95={lat[int(len(lat)*0.95)]:.1f}")
    print(f"  index size: {sz:.1f} MB")
    print("  bar: p95<300ms and linear-ish scaling → Memvid OK; "
          "else swap VectorBackend (LanceDB/pgvector/Qdrant)")
    return 0


def _selftest() -> int:
    ok = True
    md = ("# Hiring Playbook\n\n## Screening\n" + ("Call within 24h. " * 60)
          + "\n\n## Offer\nNegotiate within band.\n")
    chunks = chunk_markdown(md, "doc1", "Hiring Playbook")
    if len(chunks) >= 2 and all(c["doc_id"] == "doc1" for c in chunks):
        print(f"PASS: chunking ({len(chunks)} chunks)")
    else:
        print("FAIL: chunking", len(chunks)); ok = False

    if any("Screening" in c["heading_path"] for c in chunks):
        print("PASS: heading path preserved in chunk metadata")
    else:
        print("FAIL: heading path"); ok = False

    big = chunks[0]
    if len(big["text"]) <= CHUNK_TARGET + 10:
        print("PASS: chunk size bounded")
    else:
        print(f"FAIL: chunk too big ({len(big['text'])})"); ok = False

    # overlap present between consecutive chunks of same section
    seca = [c for c in chunks if "Screening" in c["heading_path"]]
    if len(seca) >= 2:
        tail = seca[0]["text"][-CHUNK_OVERLAP // 2:]
        if tail.split() and any(w in seca[1]["text"]
                                for w in tail.split()[:3]):
            print("PASS: overlap between adjacent chunks")
        else:
            print("WARN: overlap not detected (non-fatal)")
    print("\nselftest:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


def getdoc(doc_id: str) -> str:
    """Return the full rendered text of one document (from its artifact)."""
    import glob as _g
    rel = (load_config().get("retrieval", {}) or {}).get(
        "lark_cache_dir", "lark")
    base = rel if os.path.isabs(rel) else os.path.join(get_home(), rel)
    matches = _g.glob(os.path.join(base, "docs", "**", f"{doc_id}.md"),
                      recursive=True)
    if not matches:
        return f"(no document with id {doc_id})"
    with open(matches[0]) as f:
        return f.read()


def search_brief(query: str, k: int = 8) -> str:
    """Agent-friendly search: one line per hit — doc_id, title, snippet."""
    be = get_backend()
    try:
        hits = be.search(query, k)
    finally:
        be.close()
    seen, lines = set(), []
    for h in hits:
        did = h.get("doc_id")
        if did in seen:
            continue
        seen.add(did)
        snippet = (h.get("content") or "").replace("\n", " ")[:200]
        lines.append(f"- doc_id={did} :: {snippet}")
        if len(seen) >= k:
            break
    return "\n".join(lines) or "(no matches)"


def main() -> int:
    import argparse, json
    p = argparse.ArgumentParser(description="Document index — Noto")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("compact")
    s = sub.add_parser("search"); s.add_argument("query")
    s.add_argument("-k", type=int, default=8)
    g = sub.add_parser("getdoc"); g.add_argument("doc_id")
    b = sub.add_parser("bench"); b.add_argument("n", type=int)
    sub.add_parser("selftest")
    a = p.parse_args()
    if a.cmd == "compact":
        return compact()
    if a.cmd == "search":
        # Brief, agent-friendly output (doc_id + snippet per line).
        print(search_brief(a.query, a.k))
        return 0
    if a.cmd == "getdoc":
        print(getdoc(a.doc_id))
        return 0
    if a.cmd == "bench":
        return bench(a.n)
    if a.cmd == "selftest":
        return _selftest()
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
