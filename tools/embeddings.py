#!/usr/bin/env python3
"""
L3 — local semantic embeddings + vector RAG over the corpus.

Real semantic retrieval (today the doc index is lexical FTS only),
running 100% locally on the Mac mini: BAAI/bge-small-en-v1.5 via
fastembed (ONNX runtime, no torch), vectors stored in SQLite, search
by brute-force cosine in numpy. At corpus scale (~5k docs → ~30k
chunks) brute-force is sub-100ms and needs zero extra infra; if it
ever outgrows that, the chunks table migrates cleanly to sqlite-vec /
faiss.

Private by design: doc content never leaves the machine.

Each chunk carries its source (doc token / entity) so results link
back to the entity backend — the basis for graph-RAG (semantic hit →
entity → graph neighbors).

  indexes/vectors.db   chunks(id, source_kind, source_id, entity_type,
                              entity_key, heading, text, dim, vec BLOB,
                              updated_at)
"""

import glob
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_DIM = 384
_CHUNK_CHARS = 900
_CHUNK_OVERLAP = 150


def _home() -> str:
    from config import get_home
    return get_home()


def _db_path() -> str:
    return os.path.join(_home(), "indexes", "vectors.db")


# ---------------------------------------------------------------------------
# Model (lazy singleton)
# ---------------------------------------------------------------------------

_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        from fastembed import TextEmbedding
        _MODEL = TextEmbedding(_MODEL_NAME)
    return _MODEL


def embed_passages(texts: List[str]) -> np.ndarray:
    """Embed documents/passages. Returns (n, dim) float32, L2-normalized."""
    if not texts:
        return np.zeros((0, _DIM), dtype=np.float32)
    vecs = np.array(list(_model().embed(texts)), dtype=np.float32)
    return _normalize(vecs)


def embed_query(text: str) -> np.ndarray:
    """Embed a query (bge applies a search-query prefix internally via
    query_embed). Returns (dim,) float32, normalized."""
    v = np.array(list(_model().query_embed([text]))[0], dtype=np.float32)
    return _normalize(v.reshape(1, -1))[0]


def _normalize(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id           TEXT PRIMARY KEY,        -- sha1(source_id + ordinal)
    source_kind  TEXT NOT NULL,           -- wiki | drive | corpus | entity
    source_id    TEXT NOT NULL,           -- doc token / entity key
    entity_type  TEXT,                    -- candidate | firm | ... (if linked)
    entity_key   TEXT,
    heading      TEXT,
    ordinal      INTEGER,
    text         TEXT NOT NULL,
    vec          BLOB NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_kind, source_id);
CREATE INDEX IF NOT EXISTS idx_chunks_entity ON chunks(entity_type, entity_key);
"""


def _connect() -> sqlite3.Connection:
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, timeout=30.0)
    from sqlite_utils import harden
    harden(db)
    db.executescript(_SCHEMA)
    db.commit()
    return db


def _chunk(text: str) -> List[str]:
    """Paragraph-aware chunking to ~_CHUNK_CHARS with overlap."""
    text = (text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    cur = ""
    for p in paras:
        if len(cur) + len(p) + 2 <= _CHUNK_CHARS:
            cur = (cur + "\n\n" + p) if cur else p
        else:
            if cur:
                chunks.append(cur)
            if len(p) <= _CHUNK_CHARS:
                cur = p
            else:
                # hard-split an oversized paragraph
                for i in range(0, len(p), _CHUNK_CHARS - _CHUNK_OVERLAP):
                    chunks.append(p[i:i + _CHUNK_CHARS])
                cur = ""
    if cur:
        chunks.append(cur)
    return chunks


def index_document(source_kind: str, source_id: str, text: str,
                   heading: str = "",
                   entity_type: str = "", entity_key: str = "") -> int:
    """Chunk + embed + upsert a document. Replaces any prior chunks for
    this source_id. Returns the number of chunks indexed."""
    chunks = _chunk(text)
    if not chunks:
        return 0
    vecs = embed_passages(chunks)
    db = _connect()
    try:
        db.execute("DELETE FROM chunks WHERE source_kind=? AND source_id=?",
                   (source_kind, source_id))
        now = time.time()
        rows = []
        for i, (ch, v) in enumerate(zip(chunks, vecs)):
            cid = hashlib.sha1(f"{source_id}:{i}".encode()).hexdigest()
            rows.append((cid, source_kind, source_id, entity_type or None,
                         entity_key or None, heading, i, ch,
                         v.astype(np.float32).tobytes(), now))
        db.executemany(
            "INSERT OR REPLACE INTO chunks (id, source_kind, source_id, "
            "entity_type, entity_key, heading, ordinal, text, vec, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        db.commit()
    finally:
        db.close()
    return len(chunks)


# ---------------------------------------------------------------------------
# Search (brute-force cosine, in-memory cache)
# ---------------------------------------------------------------------------

_CACHE: Dict[str, Any] = {"n": -1, "mat": None, "meta": None}


def _load_matrix() -> Tuple[Optional[np.ndarray], List[Dict[str, Any]]]:
    db = _connect()
    try:
        n = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if n == _CACHE["n"] and _CACHE["mat"] is not None:
            return _CACHE["mat"], _CACHE["meta"]
        rows = db.execute(
            "SELECT source_kind, source_id, entity_type, entity_key, "
            "heading, text, vec FROM chunks").fetchall()
    finally:
        db.close()
    if not rows:
        return None, []
    meta = []
    mat = np.empty((len(rows), _DIM), dtype=np.float32)
    for i, r in enumerate(rows):
        mat[i] = np.frombuffer(r[6], dtype=np.float32)
        meta.append({"source_kind": r[0], "source_id": r[1],
                     "entity_type": r[2], "entity_key": r[3],
                     "heading": r[4], "text": r[5]})
    _CACHE.update({"n": len(rows), "mat": mat, "meta": meta})
    return mat, meta


def search(query: str, k: int = 8,
           source_kind: Optional[str] = None,
           min_sim: float = 0.0) -> List[Dict[str, Any]]:
    """Semantic search → top-k chunks with cosine score. Optionally
    restrict to one source_kind. min_sim drops weak matches — without
    a floor the top-k slots ALWAYS fill, so on an off-corpus question
    the "nearest" (still irrelevant) nuggets/entities get injected as
    authoritative context (2026-07 accuracy review, finding #1).
    Default 0.0 = original behavior."""
    mat, meta = _load_matrix()
    if mat is None:
        return []
    # exclude tombstoned (deleted-in-Lark) sources — soft-delete, see
    # lark_tombstones; data stays indexed, just hidden from results
    try:
        import lark_tombstones
        deleted = lark_tombstones.deleted_tokens()
    except Exception:
        deleted = frozenset()
    qv = embed_query(query)
    sims = mat @ qv                       # cosine (all normalized)
    idx = np.argsort(-sims)
    out: List[Dict[str, Any]] = []
    for i in idx:
        m = meta[i]
        if source_kind and m["source_kind"] != source_kind:
            continue
        if m["source_id"] in deleted:
            continue
        score = float(sims[i])
        if score < min_sim:
            break                 # idx is sorted desc — all below now
        out.append({**m, "score": score})
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------------------
# Build the index over the corpus + entity records
# ---------------------------------------------------------------------------

def build_all(verbose: bool = True, limit: Optional[int] = None) -> Dict[str, int]:
    """Index the cached Lark markdown corpus + the entity records.
    Resumable-ish: re-indexing replaces a source's chunks, so re-runs
    refresh rather than duplicate."""
    base = _home()
    docs = 0
    chunks = 0
    # 1) cached markdown: wiki / drive / corpus
    for kind in ("wiki", "drive", "corpus"):
        d = os.path.join(base, "lark", "docs", kind)
        files = sorted(glob.glob(os.path.join(d, "*.md")))
        if limit:
            files = files[:limit]
        if verbose:
            print(f"[embeddings] indexing {len(files)} {kind} docs…",
                  flush=True)
        for fp in files:
            try:
                with open(fp) as f:
                    text = f.read()
            except Exception:
                continue
            token = os.path.splitext(os.path.basename(fp))[0]
            n = index_document(kind, token, text)
            docs += 1 if n else 0
            chunks += n
            if verbose and docs % 250 == 0:
                print(f"  …{docs} docs, {chunks} chunks", flush=True)
    # 2) entity records (candidate/firm summaries → searchable + linked)
    for etype, sub in (("candidate", "candidates"), ("firm", "firms"),
                       ("submission", "submissions")):
        for fp in glob.glob(os.path.join(base, "brain", sub, "*",
                                          f"{etype}.json")) + \
                  glob.glob(os.path.join(base, "brain", sub, "*.json")):
            try:
                rec = json.load(open(fp))
            except Exception:
                continue
            blob = _entity_text(rec)
            if not blob.strip():
                continue
            n = index_document("entity", f"{etype}:{rec.get('key','')}",
                               blob, heading=rec.get("name", ""),
                               entity_type=etype,
                               entity_key=rec.get("key", ""))
            docs += 1 if n else 0
            chunks += n
    # reconcile: drop vectors for sources no longer on disk (unless we
    # only indexed a slice — a limited run isn't a full census)
    swept = 0
    if not limit:
        swept = sweep_orphans(verbose=verbose).get("removed_sources", 0)
    if verbose:
        print(f"[embeddings] done — {docs} docs, {chunks} chunks indexed, "
              f"{swept} orphan sources swept", flush=True)
    return {"docs": docs, "chunks": chunks, "orphans_swept": swept}


def sweep_orphans(verbose: bool = True) -> Dict[str, int]:
    """Drop vector chunks whose backing source no longer exists on disk —
    so a doc whose cached markdown was removed, or a deleted entity
    record, stops surfacing stale hits in search. Reconciles vectors.db
    against the live markdown cache + entity records. SAFE by
    construction: it only deletes chunks whose (kind, source_id) is
    provably absent from disk, and never deletes anything for a source
    that is still present.

    NOTE: docs DELETED in Lark are handled separately, by soft-delete —
    the drive walk tombstones the vanished token (lark_tombstones) and
    search excludes it, but its .md + vector are deliberately KEPT (never
    purged). So this sweep is only for genuinely orphaned vectors (e.g. a
    cache file removed out-of-band), not for Lark-side deletions."""
    base = _home()
    live = set()
    for kind in ("wiki", "drive", "corpus"):
        d = os.path.join(base, "lark", "docs", kind)
        for fp in glob.glob(os.path.join(d, "*.md")):
            live.add((kind, os.path.splitext(os.path.basename(fp))[0]))
    for etype, sub in (("candidate", "candidates"), ("firm", "firms"),
                       ("submission", "submissions")):
        # nested (candidate/firm: <slug>/<type>.json) AND flat
        # (others: <slug>.json) — must match build_all, or live entities
        # look orphaned and get swept
        for fp in (glob.glob(os.path.join(base, "brain", sub, "*",
                                          f"{etype}.json")) +
                   glob.glob(os.path.join(base, "brain", sub, "*.json"))):
            try:
                rec = json.load(open(fp))
            except Exception:
                continue
            live.add(("entity", f"{etype}:{rec.get('key','')}"))
    db = _connect()
    try:
        rows = db.execute(
            "SELECT DISTINCT source_kind, source_id FROM chunks").fetchall()
        gone = [(k, s) for (k, s) in rows if (k, s) not in live]
        for k, s in gone:
            db.execute("DELETE FROM chunks WHERE source_kind=? AND "
                       "source_id=?", (k, s))
        db.commit()
    finally:
        db.close()
    if verbose:
        print(f"[embeddings] orphan-sweep — removed {len(gone)} stale "
              f"sources (of {len(live)} live on disk)", flush=True)
    return {"removed_sources": len(gone), "live_sources": len(live)}


def _entity_text(rec: Dict[str, Any]) -> str:
    """Flatten an entity record into searchable text."""
    parts = [rec.get("name", ""), rec.get("summary", "")]
    p = rec.get("profile") or {}
    if p:
        parts.append(" ".join(str(v) for v in p.values()
                              if isinstance(v, (str, int, float))))
    for m in (rec.get("representative_matters") or []):
        parts.append(m.get("description", ""))
    # firm practices
    for c in (rec.get("practices") or []):
        if isinstance(c, dict):
            parts.append(f"{c.get('practice_area','')} {c.get('region','')} "
                         f"{c.get('strength','')}")
    # submission attributes (firm/practice/office/seniority + techniques)
    for k in ("target_firm", "practice", "target_office", "seniority"):
        v = rec.get(k)
        if isinstance(v, str):
            parts.append(v)
    tech = rec.get("techniques")
    if isinstance(tech, dict):
        parts.append(" ".join(str(v) for v in tech.values()
                              if isinstance(v, str)))
    return "\n".join(x for x in parts if x)


def _cmd_search(args: List[str]) -> int:
    q = " ".join(args)
    if not q:
        print("usage: search <query>", file=sys.stderr)
        return 2
    t = time.time()
    hits = search(q, k=8)
    dt = (time.time() - t) * 1000
    print(f"[{dt:.0f}ms] top {len(hits)} for {q!r}:")
    for h in hits:
        tag = (f"{h['entity_type']}:{h['entity_key']}"
               if h.get("entity_type") else
               f"{h['source_kind']}/{h['source_id'][:10]}")
        print(f"  {h['score']:.3f} [{tag}] {h['text'][:110]!r}")
    return 0


def stats() -> Dict[str, Any]:
    db = _connect()
    try:
        n = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        by_kind = dict(db.execute(
            "SELECT source_kind, COUNT(*) FROM chunks GROUP BY source_kind"
        ).fetchall())
        return {"chunks": n, "by_kind": by_kind, "model": _MODEL_NAME}
    finally:
        db.close()


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "build-all":
        lim = None
        for i, a in enumerate(rest):
            if a == "--limit" and i + 1 < len(rest):
                lim = int(rest[i + 1])
        build_all(verbose=True, limit=lim)
        return 0
    if cmd == "search":
        return _cmd_search(rest)
    if cmd == "stats":
        print(json.dumps(stats(), indent=2))
        return 0
    if cmd == "sweep":
        print(json.dumps(sweep_orphans(verbose=True), indent=2))
        return 0
    print("commands: build-all [--limit N] | search <query> | stats "
          "| sweep", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
