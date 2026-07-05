#!/usr/bin/env python3
"""
Noto Admin Panel v2 — server module.

Design doc: docs/admin-panel-v2.md. Everything the panel does on the
server side lives here: routing, sessions, magic-link auth, the JSON
API, and static-asset serving. lark_bot.py integrates with a 3-line
path-prefix delegation to handle(); during development this module runs
standalone (`python tools/admin_panel.py serve --port 8089`) and never
touches the production process.

Hard rules (see CLAUDE.md):
  - No Lark object deletion anywhere. The only Lark API call in this
    module is LarkClient.send_text (magic-link DMs).
  - Every mutation delegates to an existing store function so its
    invariants (CAS, audit columns, authority gates) are preserved.
  - Recruiter-memory content is super_admin-gated server-side.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_home, get_path, load_config  # noqa: E402
from sqlite_utils import harden  # noqa: E402

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "admin_static")

SESSION_COOKIE = "noto_admin"
SESSION_TTL_S = 30 * 86400          # 30-day sessions
MAGIC_TTL_S = 15 * 60               # magic links live 15 minutes
MAGIC_COOLDOWN_S = 60               # per-open_id request throttle

_magic_last_sent: Dict[str, float] = {}   # open_id -> last request ts
_magic_lock = threading.Lock()

Response = Tuple[int, List[Tuple[str, str]], bytes]


# ---------------------------------------------------------------------------
# admin.db
# ---------------------------------------------------------------------------

def _db_path() -> str:
    return os.path.join(get_home(), "indexes", "admin.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS panel_users (
    open_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    role        TEXT NOT NULL DEFAULT 'member'
                CHECK (role IN ('member','super_admin')),
    added_by    TEXT NOT NULL DEFAULT '',
    added_at    REAL NOT NULL,
    disabled    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash    TEXT PRIMARY KEY,
    open_id       TEXT NOT NULL,
    role_at_login TEXT NOT NULL,
    via           TEXT NOT NULL DEFAULT 'magic_link',
    csrf_token    TEXT NOT NULL,
    user_agent    TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    revoked       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS magic_tokens (
    token_hash  TEXT PRIMARY KEY,
    open_id     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    used_at     REAL
);
CREATE TABLE IF NOT EXISTS panel_audit (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL NOT NULL,
    actor_open_id  TEXT NOT NULL,
    actor_name     TEXT NOT NULL DEFAULT '',
    action         TEXT NOT NULL,
    target         TEXT NOT NULL DEFAULT '',
    payload_json   TEXT NOT NULL DEFAULT '{}',
    result_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON panel_audit(ts);
CREATE INDEX IF NOT EXISTS idx_sessions_open_id ON sessions(open_id);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    harden(conn)
    conn.executescript(_SCHEMA)
    _seed_super_admins(conn)
    return conn


def _seed_super_admins(conn: sqlite3.Connection) -> None:
    """First-run seed: every operators.yaml super_admin becomes a panel
    super_admin. Never overwrites rows the operator has since managed."""
    if conn.execute("SELECT COUNT(*) FROM panel_users").fetchone()[0]:
        return
    try:
        import yaml
        ops_path = os.path.join(get_home(), "memory", "operators.yaml")
        with open(ops_path) as f:
            ops = yaml.safe_load(f) or {}
        now = time.time()
        for slug, rec in ops.items():
            if isinstance(rec, dict) and rec.get("super_admin"):
                conn.execute(
                    "INSERT OR IGNORE INTO panel_users "
                    "(open_id, name, role, added_by, added_at) "
                    "VALUES (?,?,?,?,?)",
                    (rec.get("open_id", ""), rec.get("name", slug),
                     "super_admin", "(seed)", now))
        conn.commit()
    except Exception as e:
        print(f"[admin_panel] super_admin seed skipped: {e}")


def _audit(actor_oid: str, actor_name: str, action: str, target: str = "",
           payload: Any = None, result: Any = None) -> None:
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO panel_audit (ts, actor_open_id, actor_name, "
                "action, target, payload_json, result_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (time.time(), actor_oid, actor_name, action, target,
                 json.dumps(payload or {}, default=str),
                 json.dumps(result or {}, default=str)))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[admin_panel] audit write failed: {e}")


# ---------------------------------------------------------------------------
# Secrets + tokens
# ---------------------------------------------------------------------------

def _session_secret() -> bytes:
    """Process-stable HMAC key for token hashing. Generated once, 0600,
    git-ignored (brain/ already holds git-ignored secrets)."""
    path = os.path.join(get_home(), "brain", "admin_session.secret")
    try:
        with open(path, "rb") as f:
            data = f.read().strip()
        if len(data) >= 32:
            return data
    except FileNotFoundError:
        pass
    data = secrets.token_hex(32).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return data


def _hash_token(raw: str) -> str:
    return hmac.new(_session_secret(), raw.encode(), hashlib.sha256).hexdigest()


def _shared_key() -> str:
    """credentials.yaml -> dashboard.key (same fallback secret as v1)."""
    try:
        import yaml
        with open(get_path("credentials")) as f:
            data = yaml.safe_load(f) or {}
        return str((data.get("dashboard") or {}).get("key", "") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

class Principal:
    def __init__(self, open_id: str, name: str, role: str, via: str,
                 csrf_token: str):
        self.open_id = open_id
        self.name = name
        self.role = role                    # 'member' | 'super_admin'
        self.via = via
        self.csrf_token = csrf_token

    @property
    def is_super_admin(self) -> bool:
        return self.role == "super_admin"


def _cookie_token(headers: Dict[str, str]) -> str:
    raw = headers.get("cookie", "")
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == SESSION_COOKIE:
            return v.strip()
    return ""


def _current_principal(headers: Dict[str, str]) -> Optional[Principal]:
    token = _cookie_token(headers)
    if not token:
        return None
    now = time.time()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE token_hash=? AND revoked=0 "
            "AND expires_at > ?", (_hash_token(token), now)).fetchone()
        if not row:
            return None
        # Role is re-resolved per request so a demotion/disable applies
        # immediately; shared-key sessions stay super_admin by definition.
        if row["via"] == "shared_key":
            role, name = "super_admin", "(shared key)"
        else:
            u = conn.execute(
                "SELECT name, role, disabled FROM panel_users "
                "WHERE open_id=?", (row["open_id"],)).fetchone()
            if not u or u["disabled"]:
                return None
            role, name = u["role"], u["name"]
        conn.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?",
                     (now, row["token_hash"]))
        conn.commit()
        return Principal(row["open_id"], name, role, row["via"],
                         row["csrf_token"])
    finally:
        conn.close()


def _mint_session(open_id: str, role: str, via: str,
                  user_agent: str) -> Tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = time.time()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO sessions (token_hash, open_id, role_at_login, via, "
            "csrf_token, user_agent, created_at, expires_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (_hash_token(raw), open_id, role, via, csrf,
             user_agent[:200], now, now + SESSION_TTL_S, now))
        conn.commit()
    finally:
        conn.close()
    return raw, csrf


def _set_cookie_header(token: str, host: str) -> Tuple[str, str]:
    secure = "" if host.startswith(("localhost", "127.0.0.1")) else " Secure;"
    return ("Set-Cookie",
            f"{SESSION_COOKIE}={token}; HttpOnly;{secure} SameSite=Lax; "
            f"Path=/admin; Max-Age={SESSION_TTL_S}")


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _json_resp(status: int, obj: Any,
               extra_headers: Optional[List[Tuple[str, str]]] = None) -> Response:
    body = json.dumps(obj, default=str).encode()
    headers = [("Content-Type", "application/json; charset=utf-8"),
               ("Cache-Control", "no-store")]
    headers += extra_headers or []
    return status, headers, body


def _err(status: int, msg: str) -> Response:
    return _json_resp(status, {"ok": False, "error": msg})


def _html_resp(status: int, body: str,
               extra_headers: Optional[List[Tuple[str, str]]] = None) -> Response:
    headers = [("Content-Type", "text/html; charset=utf-8"),
               ("Cache-Control", "no-store")]
    headers += extra_headers or []
    return status, headers, body.encode()


def _parse_body(body: bytes) -> Dict[str, Any]:
    try:
        obj = json.loads(body.decode() or "{}")
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Static assets
# ---------------------------------------------------------------------------

_MIME = {".html": "text/html; charset=utf-8",
         ".css": "text/css; charset=utf-8",
         ".js": "application/javascript; charset=utf-8",
         ".svg": "image/svg+xml"}


def _serve_static(name: str) -> Response:
    # Whitelist: flat filenames only, no traversal.
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        return _err(404, "not found")
    path = os.path.join(STATIC_DIR, name)
    if not os.path.isfile(path):
        return _err(404, "not found")
    with open(path, "rb") as f:
        data = f.read()
    ext = os.path.splitext(name)[1]
    return 200, [("Content-Type", _MIME.get(ext, "application/octet-stream")),
                 ("Cache-Control", "no-cache")], data


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

def _base_url(headers: Dict[str, str]) -> str:
    """Where magic links point: the host the user is actually browsing
    (request Host header, port and all — funnel on 443, tailscale-serve
    on :8443, localhost in dev). Falls back to the configured Funnel
    host if the header is somehow absent."""
    cfg = load_config()
    funnel = ((cfg.get("lark") or {}).get("funnel_host") or "").strip()
    host = headers.get("host", "") or funnel
    scheme = "http" if host.startswith(("localhost", "127.0.0.1")) else "https"
    return f"{scheme}://{host}"


def send_magic_link(open_id: str, base_url: str = "") -> str:
    """Mint a one-time login token for an ENABLED panel user and DM them
    the link. Returns 'sent' | 'not_a_user' | 'cooldown' | 'send_failed'.
    Shared by the login page and the bot's /login DM command; the caller
    decides how much of that outcome to reveal."""
    if not re.fullmatch(r"ou_[0-9a-f]{16,64}", open_id or ""):
        return "not_a_user"
    with _magic_lock:
        if time.time() - _magic_last_sent.get(open_id, 0) < MAGIC_COOLDOWN_S:
            return "cooldown"
        _magic_last_sent[open_id] = time.time()
    conn = _connect()
    try:
        u = conn.execute(
            "SELECT name FROM panel_users WHERE open_id=? AND disabled=0",
            (open_id,)).fetchone()
        if not u:
            return "not_a_user"
        raw = secrets.token_urlsafe(32)
        now = time.time()
        conn.execute(
            "INSERT INTO magic_tokens (token_hash, open_id, created_at, "
            "expires_at) VALUES (?,?,?,?)",
            (_hash_token(raw), open_id, now, now + MAGIC_TTL_S))
        conn.commit()
    finally:
        conn.close()
    if not base_url:
        cfg = load_config()
        funnel = ((cfg.get("lark") or {}).get("funnel_host") or "").strip()
        base_url = f"https://{funnel}" if funnel else ""
    url = f"{base_url}/admin/auth/confirm?t={raw}"
    try:
        from lark_client import LarkClient
        LarkClient().send_text(
            open_id,
            "Noto admin panel login link (valid 15 minutes, single use):\n"
            f"{url}\n\nIf you didn't request this, ignore it.",
            receive_id_type="open_id")
    except Exception as e:
        print(f"[admin_panel] magic-link DM failed: {e}")
        return "send_failed"
    _audit(open_id, u["name"], "auth.magic_link_requested")
    return "sent"


def _auth_request_link(headers: Dict[str, str], body: bytes) -> Response:
    data = _parse_body(body)
    open_id = str(data.get("open_id", "")).strip()
    outcome = send_magic_link(open_id, base_url=_base_url(headers))
    if outcome == "send_failed":
        return _err(502, "could not send the login DM — check bot "
                         "credentials / network")
    # 'sent' / 'not_a_user' / 'cooldown' are deliberately identical —
    # no user enumeration through this endpoint.
    return _json_resp(200, {
        "ok": True,
        "message": "If that account has panel access, a login link was "
                   "sent to it as a Lark DM."})


def _auth_confirm(headers: Dict[str, str], query: Dict[str, str]) -> Response:
    raw = query.get("t", "")
    if not raw:
        return _html_resp(400, "<h3>Missing token.</h3>")
    now = time.time()
    conn = _connect()
    try:
        # Single-use CAS: only the first confirm wins.
        cur = conn.execute(
            "UPDATE magic_tokens SET used_at=? "
            "WHERE token_hash=? AND used_at IS NULL AND expires_at > ?",
            (now, _hash_token(raw), now))
        conn.commit()
        if cur.rowcount != 1:
            return _html_resp(410, "<h3>This login link has expired or was "
                                   "already used.</h3><p>Request a new one "
                                   "from the login page.</p>")
        row = conn.execute(
            "SELECT open_id FROM magic_tokens WHERE token_hash=?",
            (_hash_token(raw),)).fetchone()
        u = conn.execute(
            "SELECT name, role FROM panel_users WHERE open_id=? AND "
            "disabled=0", (row["open_id"],)).fetchone()
        if not u:
            return _html_resp(403, "<h3>Panel access has been removed.</h3>")
    finally:
        conn.close()
    token, _csrf = _mint_session(row["open_id"], u["role"], "magic_link",
                                 headers.get("user-agent", ""))
    _audit(row["open_id"], u["name"], "auth.login", "", {"via": "magic_link"})
    return _html_resp(
        302, "",
        extra_headers=[_set_cookie_header(token, headers.get("host", "")),
                       ("Location", "/admin")])


def _auth_key(headers: Dict[str, str], body: bytes) -> Response:
    configured = _shared_key()
    if not configured:
        return _err(503, "shared-key login not configured")
    supplied = str(_parse_body(body).get("key", ""))
    if not hmac.compare_digest(supplied, configured):
        time.sleep(0.5)             # cheap brute-force damper
        return _err(401, "invalid key")
    token, _csrf = _mint_session("(shared-key)", "super_admin", "shared_key",
                                 headers.get("user-agent", ""))
    _audit("(shared-key)", "(shared key)", "auth.login", "",
           {"via": "shared_key"})
    return _json_resp(200, {"ok": True},
                      [_set_cookie_header(token, headers.get("host", ""))])


def _auth_logout(headers: Dict[str, str]) -> Response:
    token = _cookie_token(headers)
    if token:
        conn = _connect()
        try:
            conn.execute("UPDATE sessions SET revoked=1 WHERE token_hash=?",
                         (_hash_token(token),))
            conn.commit()
        finally:
            conn.close()
    return _json_resp(200, {"ok": True},
                      [("Set-Cookie",
                        f"{SESSION_COOKIE}=; Path=/admin; Max-Age=0")])


def _me(p: Principal) -> Response:
    return _json_resp(200, {
        "ok": True,
        "open_id": p.open_id,
        "name": p.name,
        "role": p.role,
        "via": p.via,
        "csrf": p.csrf_token,
    })


# ---------------------------------------------------------------------------
# Inbox counts (Phase 1: live badges on the shell)
# ---------------------------------------------------------------------------

def _count(db_file: str, sql: str) -> Optional[int]:
    path = os.path.join(get_home(), "indexes", db_file)
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(path)
        harden(conn)
        try:
            return int(conn.execute(sql).fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return None


def _inbox_counts(p: Principal) -> Response:
    return _json_resp(200, {
        "ok": True,
        "rules": _count("feedback.db",
                        "SELECT COUNT(*) FROM recommended_rules "
                        "WHERE status='pending'"),
        "lessons": _count("feedback.db",
                          "SELECT COUNT(*) FROM derived_lessons "
                          "WHERE status='pending'") or 0,
        "synth_running": _synth_state["running"],
        "feedback": _count("feedback.db",
                           "SELECT COUNT(*) FROM feedback "
                           "WHERE status='unresolved'"),
        "nuggets": _count("chat_nuggets.db",
                          "SELECT COUNT(*) FROM chat_nuggets "
                          "WHERE status='pending'"),
        "open_polls": _count("pipeline.db",
                             "SELECT COUNT(*) FROM pipeline_polls "
                             "WHERE status='open'"),
        "nuggets_unembedded": _count(
            "chat_nuggets.db",
            "SELECT COUNT(*) FROM chat_nuggets "
            "WHERE status='active' AND embedded_at IS NULL"),
        "nuggets_unchecked": _count(
            "chat_nuggets.db",
            "SELECT COUNT(*) FROM chat_nuggets "
            "WHERE status='pending' AND context_note=''"),
        "embed_running": _embed_state["running"],
    })


# ---------------------------------------------------------------------------
# Inbox: recommended rules (feedback_cluster) — Phase 2
# ---------------------------------------------------------------------------

def _feedback_db() -> str:
    return os.path.join(get_home(), "indexes", "feedback.db")


def _rules_list(p: Principal, query: Dict[str, str]) -> Response:
    status = query.get("status", "pending")
    workflow = query.get("workflow", "")
    limit = min(int(query.get("limit", "200") or 200), 500)
    if not os.path.exists(_feedback_db()):
        return _json_resp(200, {"ok": True, "rules": [], "workflows": []})
    conn = sqlite3.connect(_feedback_db())
    conn.row_factory = sqlite3.Row
    harden(conn)
    try:
        wh, args = [], []
        if status and status != "all":
            wh.append("status=?"); args.append(status)
        if workflow:
            wh.append("workflow=?"); args.append(workflow)
        sql = ("SELECT * FROM recommended_rules"
               + (" WHERE " + " AND ".join(wh) if wh else "")
               + " ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,"
                 " CASE priority WHEN 'high' THEN 0 ELSE 1 END,"
                 " recommended_at DESC LIMIT ?")
        rows = [dict(r) for r in conn.execute(sql, args + [limit])]
        workflows = [r[0] for r in conn.execute(
            "SELECT DISTINCT workflow FROM recommended_rules "
            "ORDER BY workflow")]
    finally:
        conn.close()
    return _json_resp(200, {"ok": True, "rules": rows,
                            "workflows": workflows})


def _rule_approve(p: Principal, rule_id: int, body: bytes) -> Response:
    import feedback_cluster
    data = _parse_body(body)
    res = feedback_cluster.approve(
        rule_id, p.open_id, reviewer_name=p.name,
        edited_text=str(data.get("edited_text", "") or ""),
        reviewer_note=str(data.get("note", "") or ""))
    _audit(p.open_id, p.name, "rules.approve", str(rule_id), data, res)
    return _json_resp(200 if res.get("ok") else 409, res)


def _rule_reject(p: Principal, rule_id: int, body: bytes) -> Response:
    import feedback_cluster
    data = _parse_body(body)
    res = feedback_cluster.reject(
        rule_id, p.open_id, reviewer_name=p.name,
        reason=str(data.get("reason", "") or ""))
    _audit(p.open_id, p.name, "rules.reject", str(rule_id), data, res)
    return _json_resp(200 if res.get("ok") else 409, res)


# ---------------------------------------------------------------------------
# Inbox: derived lessons — the synthesis layer (feedback_synthesis.py).
# Raw feedback is evidence; Noto derives lessons with reasoning + an
# audit trail, and THOSE are what the operator approves/rejects.
# ---------------------------------------------------------------------------

_synth_lock = threading.Lock()
_synth_thread: Optional[threading.Thread] = None
_synth_state: Dict[str, Any] = {"running": False, "last_done": 0.0,
                                "last_result": None}


def _synth_worker() -> None:
    import feedback_synthesis
    _synth_state["running"] = True
    try:
        _synth_state["last_result"] = feedback_synthesis.synthesize(
            verbose=False)
    except Exception as e:
        _synth_state["last_result"] = {"ok": False, "error": str(e)[:300]}
        print(f"[admin_panel] synthesis failed: {e}")
    _synth_state["running"] = False
    _synth_state["last_done"] = time.time()


def _lessons_list(p: Principal, query: Dict[str, str]) -> Response:
    import feedback_synthesis
    lessons = feedback_synthesis.list_lessons(
        status=query.get("status", "pending"),
        scope=query.get("scope") or None,
        limit=min(int(query.get("limit", "200") or 200), 500))
    st = feedback_synthesis.stats()
    return _json_resp(200, {"ok": True, "lessons": lessons, "stats": st,
                            "synth": dict(_synth_state)})


def _lesson_approve(p: Principal, lid: int, body: bytes) -> Response:
    import feedback_synthesis
    data = _parse_body(body)
    res = feedback_synthesis.approve_lesson(
        lid, reviewer_open_id=p.open_id, reviewer_name=p.name,
        edited_text=str(data.get("edited_text", "") or ""),
        reviewer_note=str(data.get("note", "") or ""))
    _audit(p.open_id, p.name, "lessons.approve", str(lid),
           {k: v for k, v in data.items() if k != "_csrf"}, res)
    return _json_resp(200 if res.get("ok") else 409, res)


def _lesson_reject(p: Principal, lid: int, body: bytes) -> Response:
    import feedback_synthesis
    data = _parse_body(body)
    res = feedback_synthesis.reject_lesson(
        lid, reviewer_open_id=p.open_id, reviewer_name=p.name,
        reason=str(data.get("reason", "") or ""))
    _audit(p.open_id, p.name, "lessons.reject", str(lid),
           {k: v for k, v in data.items() if k != "_csrf"}, res)
    return _json_resp(200 if res.get("ok") else 409, res)


def _lessons_synthesize(p: Principal, body: bytes) -> Response:
    global _synth_thread
    with _synth_lock:
        if _synth_thread is not None and _synth_thread.is_alive():
            return _err(409, "a synthesis pass is already running")
        _synth_thread = threading.Thread(target=_synth_worker, daemon=True,
                                         name="admin-panel-synthesis")
        _synth_thread.start()
    _audit(p.open_id, p.name, "lessons.synthesize_started")
    return _json_resp(200, {"ok": True, "message":
                            "synthesis started — Noto is reading the "
                            "feedback; refresh in ~a minute"})


def _rule_evidence(p: Principal, rule_id: int) -> Response:
    """Supporting feedback_events for a recommended rule — the audit
    trail behind the doc-edit-diff pipeline."""
    if not os.path.exists(_feedback_db()):
        return _err(404, "no feedback db")
    conn = sqlite3.connect(_feedback_db())
    conn.row_factory = sqlite3.Row
    harden(conn)
    try:
        r = conn.execute("SELECT supporting_event_ids FROM "
                         "recommended_rules WHERE id=?",
                         (rule_id,)).fetchone()
        if not r:
            return _err(404, "no such rule")
        try:
            ids = json.loads(r["supporting_event_ids"] or "[]")
        except Exception:
            ids = []
        events = []
        if ids:
            q = ",".join("?" * len(ids))
            events = [dict(e) for e in conn.execute(
                f"SELECT id, workflow, source, doc_url, candidate, "
                f"recruiter_name, authority, instruction, "
                f"before_md, after_md, "
                f"change_type, confidence, captured_at "
                f"FROM feedback_events WHERE id IN ({q})", ids)]
    finally:
        conn.close()
    # The reviewer needs to see WHAT CHANGED, not the head of two long
    # documents — compute a compact unified diff per event and ship
    # that instead of the raw versions.
    import difflib
    for ev in events:
        before = (ev.pop("before_md", "") or "")[:60000]
        after = (ev.pop("after_md", "") or "")[:60000]
        if not before and after:
            ev["diff"] = ("(document created in this edit — opening "
                          "excerpt)\n" + after[:1200])
            continue
        if before and not after:
            ev["diff"] = "(document content removed in this edit)"
            continue
        lines = list(difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile="before", tofile="after", lineterm="", n=2))
        ev["diff"] = "\n".join(lines[2:])[:8000] \
            if len(lines) > 2 else "(no textual change detected)"
    return _json_resp(200, {"ok": True, "events": events})


# ---------------------------------------------------------------------------
# Inbox: raw feedback triage — Phase 2
# ---------------------------------------------------------------------------

def _feedback_list(p: Principal, query: Dict[str, str]) -> Response:
    from feedback_store import FeedbackStore
    status = query.get("status", "unresolved")
    limit = min(int(query.get("limit", "200") or 200), 500)
    s = FeedbackStore()
    try:
        rows = s.list(status=None if status == "all" else status,
                      workflow=query.get("workflow") or None,
                      kind=query.get("kind") or None,
                      limit=limit)
        stats = s.stats()
        # Lifecycle: which lesson consumed each feedback item. Superseded
        # lessons first so a live lesson overwrites the mapping.
        lesson_of: Dict[int, Dict[str, Any]] = {}
        try:
            lrows = s.conn.execute(
                "SELECT id, status, supporting_feedback_ids FROM "
                "derived_lessons ORDER BY CASE status "
                "WHEN 'superseded' THEN 0 ELSE 1 END, id").fetchall()
            for lr in lrows:
                try:
                    fids = json.loads(lr["supporting_feedback_ids"] or "[]")
                except Exception:
                    continue
                for fid in fids:
                    lesson_of[int(fid)] = {"lesson_id": lr["id"],
                                           "lesson_status": lr["status"]}
        except sqlite3.OperationalError:
            pass    # derived_lessons not created yet
    finally:
        s.close()
    for r in rows:
        link = lesson_of.get(int(r["id"]))
        r["lesson_id"] = link["lesson_id"] if link else None
        r["lesson_status"] = link["lesson_status"] if link else None
    return _json_resp(200, {"ok": True, "feedback": rows,
                            "workflows": sorted(stats["by_workflow"])})


def _feedback_accept(p: Principal, fid: int, body: bytes) -> Response:
    from feedback_store import accept_with_routing
    data = _parse_body(body)
    kind = data.get("kind") or None
    if kind is not None and kind not in ("rule", "engineering", "both"):
        return _err(400, "kind must be rule / engineering / both")
    res = accept_with_routing(fid, note=str(data.get("note", "") or ""),
                              kind=kind, actor_name=p.name)
    _audit(p.open_id, p.name, "feedback.accept", str(fid), data, res)
    return _json_resp(200 if res.get("ok") else 409, res)


def _feedback_reject(p: Principal, fid: int, body: bytes) -> Response:
    from feedback_store import reject_feedback
    data = _parse_body(body)
    res = reject_feedback(fid, note=str(data.get("note", "") or ""))
    _audit(p.open_id, p.name, "feedback.reject", str(fid), data, res)
    return _json_resp(200 if res.get("ok") else 409, res)


def _feedback_reclassify(p: Principal, fid: int, body: bytes) -> Response:
    from feedback_store import FeedbackStore
    kind = str(_parse_body(body).get("kind", ""))
    if kind not in ("rule", "engineering", "both", "unsure"):
        return _err(400, "bad kind")
    s = FeedbackStore()
    try:
        ok = s.reclassify(fid, kind)
    finally:
        s.close()
    _audit(p.open_id, p.name, "feedback.reclassify", str(fid),
           {"kind": kind}, {"ok": ok})
    return _json_resp(200 if ok else 404, {"ok": ok})


# ---------------------------------------------------------------------------
# Inbox: chat nuggets — Phase 3
# Embedding runs on a coalescing background worker: approve flips status
# immediately (embed=False) and requests ONE sweep; a batch of N approvals
# still costs one embed_active() pass.
# ---------------------------------------------------------------------------

_embed_req = threading.Event()
_embed_lock = threading.Lock()
_embed_thread: Optional[threading.Thread] = None
_embed_state: Dict[str, Any] = {"running": False, "last_done": 0.0,
                                "last_result": None}


def _embed_worker() -> None:
    import chat_nuggets
    while _embed_req.is_set():
        _embed_req.clear()
        _embed_state["running"] = True
        try:
            # Approval pipeline first (contextualize → embed), then
            # pre-check a batch of pending nuggets so verdicts are
            # already on the rows when the reviewer opens the queue.
            ctx = chat_nuggets.contextualize_unembedded(verbose=False)
            emb = chat_nuggets.embed_active(verbose=False)
            pre = chat_nuggets.contextualize_pending(verbose=False)
            _embed_state["last_result"] = {**ctx, **emb, **pre}
            if pre.get("prechecked"):
                _embed_req.set()   # more pending may remain — loop
        except Exception as e:
            _embed_state["last_result"] = {"error": str(e)[:200]}
            print(f"[admin_panel] embed sweep failed: {e}")
        _embed_state["running"] = False
        _embed_state["last_done"] = time.time()


def _request_embed_sweep() -> None:
    global _embed_thread
    _embed_req.set()
    with _embed_lock:
        if _embed_thread is None or not _embed_thread.is_alive():
            _embed_thread = threading.Thread(
                target=_embed_worker, daemon=True,
                name="admin-panel-embed")
            _embed_thread.start()


def _nuggets_list(p: Principal, query: Dict[str, str]) -> Response:
    status = query.get("status", "pending")
    chat = query.get("chat", "")
    limit = min(int(query.get("limit", "200") or 200), 500)
    path = os.path.join(get_home(), "indexes", "chat_nuggets.db")
    if not os.path.exists(path):
        return _json_resp(200, {"ok": True, "nuggets": [], "chats": []})
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    harden(conn)
    try:
        wh, args = [], []
        if status and status != "all":
            wh.append("status=?"); args.append(status)
        if chat:
            wh.append("chat_name=?"); args.append(chat)
        sql = ("SELECT * FROM chat_nuggets"
               + (" WHERE " + " AND ".join(wh) if wh else "")
               + " ORDER BY created_at DESC LIMIT ?")
        rows = [dict(r) for r in conn.execute(sql, args + [limit])]
        chats = [r[0] for r in conn.execute(
            "SELECT DISTINCT chat_name FROM chat_nuggets "
            "WHERE chat_name != '' ORDER BY chat_name")]
    finally:
        conn.close()
    # Auto pre-check: if any pending nugget lacks its corpus verdict,
    # kick the coalescing worker so verdicts fill in while the reviewer
    # reads (operator-requested default, 2026-07-02).
    if any(r["status"] == "pending" and not (r.get("context_note") or "")
           for r in rows):
        _request_embed_sweep()
    return _json_resp(200, {"ok": True, "nuggets": rows, "chats": chats,
                            "embed": dict(_embed_state)})


def _nugget_approve(p: Principal, nid: int, body: bytes) -> Response:
    import chat_nuggets
    data = _parse_body(body)
    res = chat_nuggets.approve(
        nid, reviewer_open_id=p.open_id, reviewer_name=p.name,
        edited_question=str(data.get("edited_question", "") or ""),
        edited_answer=str(data.get("edited_answer", "") or ""),
        embed=False,
        reviewer_note=str(data.get("note", "") or ""))
    if res.get("ok"):
        _request_embed_sweep()
    _audit(p.open_id, p.name, "nuggets.approve", str(nid),
           {k: v for k, v in data.items() if k != "_csrf"}, res)
    return _json_resp(200 if res.get("ok") else 409, res)


def _nugget_contextualize(p: Principal, nid: int, body: bytes) -> Response:
    """On-demand corpus cross-check for a PENDING nugget — lets the
    reviewer see the durability verdict + corpus context BEFORE deciding.
    Synchronous (one LLM call, tens of seconds); the UI shows a spinner."""
    import chat_nuggets
    res = chat_nuggets.contextualize(nid, verbose=False)
    _audit(p.open_id, p.name, "nuggets.contextualize", str(nid), {}, res)
    return _json_resp(200 if res.get("ok") else 502, res)


def _nugget_dismiss(p: Principal, nid: int, body: bytes) -> Response:
    import chat_nuggets
    data = _parse_body(body)
    res = chat_nuggets.dismiss(
        nid, reviewer_open_id=p.open_id, reviewer_name=p.name,
        reason=str(data.get("reason", "") or ""))
    _audit(p.open_id, p.name, "nuggets.dismiss", str(nid),
           {k: v for k, v in data.items() if k != "_csrf"}, res)
    return _json_resp(200 if res.get("ok") else 409, res)


# ---------------------------------------------------------------------------
# Pipeline history — Phase 4 (read-only)
# ---------------------------------------------------------------------------

def _pipeline_db() -> str:
    return os.path.join(get_home(), "indexes", "pipeline.db")


def _pipeline_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_pipeline_db())
    conn.row_factory = sqlite3.Row
    harden(conn)
    return conn


def _pipeline_list(p: Principal, query: Dict[str, str]) -> Response:
    if not os.path.exists(_pipeline_db()):
        return _json_resp(200, {"ok": True, "rows": [], "facets": {}})
    limit = min(int(query.get("limit", "100") or 100), 300)
    before_ms = int(query.get("before", "0") or 0)   # pagination cursor
    wh, args = [], []
    if query.get("verdict"):
        wh.append("e.extraction_verdict = ?"); args.append(query["verdict"])
    if query.get("candidate"):
        wh.append("p.candidate_name LIKE ?")
        args.append(f"%{query['candidate']}%")
    if query.get("firm"):
        wh.append("p.firm LIKE ?"); args.append(f"%{query['firm']}%")
    if query.get("resolved_by"):
        wh.append("p.resolved_by_name = ?"); args.append(query["resolved_by"])
    if query.get("has_poll") == "1":
        wh.append("p.id IS NOT NULL")
    if before_ms:
        wh.append("e.internal_date_ms < ?"); args.append(before_ms)
    conn = _pipeline_conn()
    try:
        # pipeline_status_changes is created lazily by pipeline_apply — a
        # fresh install may not have it yet; join NULLs in that case.
        has_changes = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='pipeline_status_changes'").fetchone())
        change_cols = (
            "c.old_status, c.new_status, c.applied_by_name, c.applied_at, "
            "c.bitable_record_id, c.summary_doc_id "
            if has_changes else
            "NULL AS old_status, NULL AS new_status, "
            "NULL AS applied_by_name, NULL AS applied_at, "
            "NULL AS bitable_record_id, NULL AS summary_doc_id ")
        change_join = ("LEFT JOIN pipeline_status_changes c ON c.poll_id = p.id "
                       if has_changes else "")
        sql = (
            "SELECT e.message_id, e.from_email, e.from_name, e.subject, "
            "substr(e.body_plain, 1, 200) AS body_head, "
            "e.internal_date_ms, e.extraction_verdict, "
            "p.id AS poll_id, p.poll_type, p.candidate_name, p.firm, "
            "p.proposed_status, p.status AS poll_status, "
            "p.resolved_by_name, p.resolved_at, "
            + change_cols +
            "FROM emails e "
            "LEFT JOIN pipeline_polls p ON p.source_message_id = e.message_id "
            + change_join
            + ("WHERE " + " AND ".join(wh) if wh else "")
            + " ORDER BY e.internal_date_ms DESC LIMIT ?")
        rows = [dict(r) for r in conn.execute(sql, args + [limit])]
        verdicts = [r[0] for r in conn.execute(
            "SELECT DISTINCT extraction_verdict FROM emails "
            "WHERE extraction_verdict IS NOT NULL ORDER BY 1")]
        resolvers = [r[0] for r in conn.execute(
            "SELECT DISTINCT resolved_by_name FROM pipeline_polls "
            "WHERE resolved_by_name != '' ORDER BY 1")]
        counts = {r[0] or "(unextracted)": r[1] for r in conn.execute(
            "SELECT extraction_verdict, COUNT(*) FROM emails "
            "GROUP BY extraction_verdict")}
    finally:
        conn.close()
    return _json_resp(200, {
        "ok": True, "rows": rows,
        "facets": {"verdict": verdicts, "resolved_by": resolvers},
        "verdict_counts": counts,
        "next_before": rows[-1]["internal_date_ms"] if len(rows) == limit else None,
    })


def _pipeline_polls_list(p: Principal, query: Dict[str, str]) -> Response:
    import pipeline_store
    polls = pipeline_store.list_open_polls()
    now = time.time()
    for pl in polls:
        try:
            pl["expires_in_s"] = max(0, int((pl.get("expires_at_ts") or 0)
                                            - now)) or None
        except Exception:
            pl["expires_in_s"] = None
    return _json_resp(200, {"ok": True, "polls": polls})


def _poll_resolve(p: Principal, poll_id: int, action: str,
                  body: bytes) -> Response:
    """Approve/reject a pipeline poll from the panel — the exact same
    contract as the Lark card click handler: admin gate → first-wins
    CAS → apply (approve only) → stash apply_result → update the card
    in Pipeline Management best-effort."""
    from feedback_capture import is_admin
    if p.via != "shared_key" and not is_admin(p.open_id):
        return _err(403, "pipeline changes need admin tier "
                         "(operators.yaml) — same gate as the Lark card")
    import pipeline_store as ps
    import pipeline_apply
    import pipeline_card
    from datetime import datetime, timezone
    poll = ps.get_poll(poll_id)
    if not poll:
        return _err(404, f"no poll #{poll_id}")
    new_status = "approved" if action == "approve" else "rejected"
    won = ps.resolve_poll(poll_id, new_status, p.open_id, p.name)
    if not won:
        cur = ps.get_poll(poll_id) or {}
        return _err(409, f"already {cur.get('status')} by "
                         f"{cur.get('resolved_by_name') or 'someone'}")
    apply_result = None
    if new_status == "approved":
        try:
            apply_result = pipeline_apply.apply_poll(
                poll_id, approver_oid=p.open_id, approver_name=p.name)
        except Exception as e:
            apply_result = {"ok": False, "error": str(e)[:200]}
        db = ps._connect()
        try:
            db.execute("UPDATE pipeline_polls SET apply_result=? "
                       "WHERE id=?", (json.dumps(apply_result), poll_id))
            db.commit()
        finally:
            db.close()
    try:
        pipeline_card.update_to_resolved(
            card_message_id=poll.get("card_message_id") or "",
            poll_id=poll_id, summary_md=poll.get("summary_md", ""),
            status=new_status, resolver_name=p.name,
            apply_result=apply_result,
            resolved_at=datetime.now(timezone.utc).strftime("%H:%M UTC"))
    except Exception as e:
        print(f"[admin_panel] poll card update failed: {e}")
    _audit(p.open_id, p.name, f"pipeline.poll_{action}", str(poll_id),
           {}, {"apply_result": apply_result})
    return _json_resp(200, {"ok": True, "poll_id": poll_id,
                            "status": new_status,
                            "apply_result": apply_result})


def _pipeline_email(p: Principal, query: Dict[str, str]) -> Response:
    mid = query.get("id", "")
    if not mid:
        return _err(400, "missing id")
    conn = _pipeline_conn()
    try:
        e = conn.execute(
            "SELECT message_id, smtp_message_id, thread_id, "
            "internal_date_ms, from_email, from_name, to_emails, subject, "
            "substr(body_plain, 1, 20000) AS body_plain, fetched_at, "
            "extracted_at, extraction_verdict, extraction_json "
            "FROM emails WHERE message_id=?", (mid,)).fetchone()
        if not e:
            return _err(404, "no such email")
        email = dict(e)
        polls = [dict(r) for r in conn.execute(
            "SELECT * FROM pipeline_polls WHERE source_message_id=? "
            "ORDER BY id", (mid,))]
        try:
            changes = [dict(r) for r in conn.execute(
                "SELECT * FROM pipeline_status_changes "
                "WHERE source_message_id=? ORDER BY id", (mid,))]
        except sqlite3.OperationalError:
            changes = []
    finally:
        conn.close()
    for k in ("extraction_json",):
        try:
            email[k] = json.loads(email.get(k) or "null")
        except Exception:
            pass
    for pl in polls:
        for k in ("proposed_event", "resolution_payload", "apply_result"):
            try:
                pl[k] = json.loads(pl.get(k) or "null")
            except Exception:
                pass
    for c in changes:
        try:
            c["notes"] = json.loads(c.get("notes") or "null")
        except Exception:
            pass
    return _json_resp(200, {"ok": True, "email": email, "polls": polls,
                            "changes": changes})


# ---------------------------------------------------------------------------
# Candidate directory — Phase 5 (read-only)
# Backbone = lark/candidate_folder_index.json (folder presence is the
# "real engagement" signal used across the pipeline); enriched from
# submissions.db, candidate_artifacts.db, pipeline.db and candidates.db
# (Bitable mirror — may be empty until the Base is provisioned).
# ---------------------------------------------------------------------------

_cfi_cache: Dict[str, Any] = {"mtime": 0.0, "data": {}}


def _folder_index() -> Dict[str, Any]:
    path = os.path.join(get_home(), "lark", "candidate_folder_index.json")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}
    if mt != _cfi_cache["mtime"]:
        try:
            with open(path) as f:
                _cfi_cache["data"] = (json.load(f) or {}).get("candidates", {})
            _cfi_cache["mtime"] = mt
        except Exception:
            return _cfi_cache["data"] or {}
    return _cfi_cache["data"]


def _tenant_url() -> str:
    cfg = load_config()
    return str((cfg.get("lark") or {}).get(
        "tenant_url", "")).rstrip("/")


def _idx_db(name: str) -> Optional[sqlite3.Connection]:
    path = os.path.join(get_home(), "indexes", name)
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    harden(conn)
    return conn


def _candidates_list(p: Principal, query: Dict[str, str]) -> Response:
    q = (query.get("q") or "").strip().lower()
    limit = min(int(query.get("limit", "60") or 60), 200)
    cands = _folder_index()

    # Aggregates, one query per store.
    subs: Dict[str, Tuple[int, Any]] = {}
    conn = _idx_db("submissions.db")
    if conn:
        try:
            for r in conn.execute(
                    "SELECT lower(candidate_name) k, COUNT(*) n, "
                    "MAX(doc_modify_time) m FROM submissions GROUP BY k"):
                subs[r["k"]] = (r["n"], r["m"])
        finally:
            conn.close()
    arts: Dict[str, List[str]] = {}
    conn = _idx_db("candidate_artifacts.db")
    if conn:
        try:
            for r in conn.execute(
                    "SELECT candidate_key, artifact_type FROM artifacts"):
                arts.setdefault(r["candidate_key"], []).append(
                    r["artifact_type"])
        finally:
            conn.close()
    polls: Dict[str, Any] = {}
    conn = _idx_db("pipeline.db")
    if conn:
        try:
            for r in conn.execute(
                    "SELECT COALESCE(NULLIF(candidate_key,''), "
                    "lower(candidate_name)) k, MAX(created_at) m, COUNT(*) n "
                    "FROM pipeline_polls GROUP BY k"):
                polls[r["k"]] = (r["n"], r["m"])
        finally:
            conn.close()
    status: Dict[str, str] = {}
    conn = _idx_db("candidates.db")
    if conn:
        try:
            for r in conn.execute("SELECT lower(name) k, status "
                                  "FROM candidates"):
                status[r["k"]] = r["status"]
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()

    rows = []
    for key, rec in cands.items():
        if q and q not in key and q not in (rec.get("practice") or "").lower():
            continue
        n_subs, last_sub = subs.get(key, (0, None))
        n_polls, last_poll = polls.get(key, (0, None))
        # also try dashed key used by pipeline (seung-jin-lee)
        if not n_polls:
            n_polls, last_poll = polls.get(key.replace(" ", "-"), (0, None))
        rows.append({
            "key": key,
            "name": rec.get("name") or key.title(),
            "practice": rec.get("practice") or "",
            "status": status.get(key),
            "artifacts": sorted(set(arts.get(key, []))),
            "submissions": n_subs,
            "pipeline_events": n_polls,
            "last_activity": max(filter(None, [last_sub, last_poll]),
                                 default=None),
            "indexed_at": rec.get("indexed_at"),
        })
    rows.sort(key=lambda r: (r["last_activity"] or "", r["submissions"]),
              reverse=True)
    return _json_resp(200, {"ok": True, "total": len(rows),
                            "rows": rows[:limit]})


_bitable_cache: Dict[str, Any] = {"at": 0.0, "data": None}


def _fetch_bitable_rows() -> Dict[str, Any]:
    """Fetch + shape the CRM table; updates the cache. Raises on API
    failure."""
    cfg = load_config()
    lk = cfg.get("lark") or {}
    app_token = lk.get("bitable_app_token", "")
    table_id = lk.get("bitable_table_pipeline", "")
    if not (app_token and table_id):
        raise RuntimeError("bitable not configured")
    from lark_client import LarkClient
    recs = LarkClient().bitable_list_records(app_token, table_id)
    # Real submission dates: latest submission-doc creation per candidate
    # (submissions.db). The Bitable has no "submitted at" field — its
    # Last Modified Date is the fallback (any edit bumps it).
    sub_ts: Dict[str, float] = {}
    conn = _idx_db("submissions.db")
    if conn:
        try:
            for nm, ts in conn.execute(
                    "SELECT lower(candidate_name), "
                    "MAX(COALESCE(doc_create_time, doc_modify_time)) "
                    "FROM submissions GROUP BY 1"):
                try:
                    v = float(ts)
                    sub_ts[nm] = v / 1000 if v > 1e12 else v
                except (TypeError, ValueError):
                    pass
        finally:
            conn.close()
    rows = []
    counts: Dict[str, int] = {}
    for r in recs:
        f = r.get("fields") or {}
        name = f.get("Name")
        if isinstance(name, list):     # rich-text cells come as lists
            name = "".join(x.get("text", "") if isinstance(x, dict)
                           else str(x) for x in name)
        name = (name or "").strip()
        statuses = f.get("Status") or []
        if isinstance(statuses, str):
            statuses = [statuses]
        primary = statuses[0] if statuses else "(no status)"
        counts[primary] = counts.get(primary, 0) + 1
        link = f.get("Profile Link")
        url = (link or {}).get("link") if isinstance(link, dict) else None
        rows.append({
            "record_id": r.get("record_id"),
            "name": name,
            "statuses": statuses,
            "primary": primary,
            "profile_url": url,
            "modified_ms": f.get("Last Modified Date"),
            "last_submission_ts": sub_ts.get(name.lower()),
        })
    rows.sort(key=lambda x: x.get("modified_ms") or 0, reverse=True)
    now = time.time()
    data = {"ok": True, "total": len(rows), "counts": counts,
            "rows": rows, "fetched_at": now}
    _bitable_cache.update(at=now, data=data)
    return data


def _bitable_refresh_async() -> None:
    """Refresh the CRM cache off the request thread."""
    def _run():
        try:
            _fetch_bitable_rows()
        except Exception as e:
            print(f"[admin_panel] bitable refresh failed: {e}")
    threading.Thread(target=_run, daemon=True,
                     name="admin-panel-bitable").start()


def _candidates_bitable(p: Principal, query: Dict[str, str]) -> Response:
    """Live CRM view of the candidate-pipeline Bitable (the source of
    truth for Status). ~1.5k records over a paginated API takes ~30s, so:
    fresh cache (<120s) serves directly; stale cache serves immediately
    and refreshes in the background; only a cold start ever waits."""
    now = time.time()
    if _bitable_cache["data"] and not query.get("fresh"):
        if now - _bitable_cache["at"] >= 120:
            _bitable_refresh_async()
        return _json_resp(200, _bitable_cache["data"])
    try:
        return _json_resp(200, _fetch_bitable_rows())
    except Exception as e:
        return _err(502, f"bitable read failed: {str(e)[:200]}")


def _candidate_detail(p: Principal, query: Dict[str, str]) -> Response:
    key = (query.get("key") or "").strip().lower()
    cands = _folder_index()
    rec = cands.get(key)
    if not rec:
        return _err(404, "no such candidate in the folder index")
    tenant = _tenant_url()
    out: Dict[str, Any] = {
        "ok": True,
        "key": key,
        "name": rec.get("name") or key.title(),
        "practice": rec.get("practice") or "",
        "folder_name": rec.get("folder_name") or "",
        "folder_url": (f"{tenant}/drive/folder/{rec['folder_token']}"
                       if rec.get("folder_token") else None),
        "subfolders": {
            n: f"{tenant}/drive/folder/{tok}"
            for n, tok in (rec.get("subfolders") or {}).items()},
    }

    conn = _idx_db("candidates.db")
    out["bitable"] = None
    if conn:
        try:
            r = conn.execute("SELECT * FROM candidates WHERE lower(name)=?",
                             (key,)).fetchone()
            if r:
                out["bitable"] = dict(r)
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()

    out["artifacts"] = []
    conn = _idx_db("candidate_artifacts.db")
    if conn:
        try:
            out["artifacts"] = [dict(r) for r in conn.execute(
                "SELECT artifact_type, doc_url, doc_title, version, "
                "last_updated_at, last_updated_by_name, adopted_from_drive, "
                "sync_state FROM artifacts WHERE candidate_key=? "
                "ORDER BY artifact_type", (key,))]
        finally:
            conn.close()

    out["submissions"] = []
    conn = _idx_db("submissions.db")
    if conn:
        try:
            out["submissions"] = [
                {**dict(r),
                 "doc_url": f"{tenant}/docx/{r['doc_token']}"
                            if r["doc_token"] else None}
                for r in conn.execute(
                    "SELECT doc_token, target_firm, target_office, "
                    "seniority_bucket, summary, doc_modify_time "
                    "FROM submissions WHERE lower(candidate_name)=? "
                    "ORDER BY doc_modify_time DESC LIMIT 30", (key,))]
        finally:
            conn.close()

    out["pipeline"] = []
    out["emails"] = []
    conn = _idx_db("pipeline.db")
    if conn:
        try:
            dashed = key.replace(" ", "-")
            out["pipeline"] = [dict(r) for r in conn.execute(
                "SELECT id, poll_type, firm, proposed_status, status, "
                "resolved_by_name, resolved_at, created_at "
                "FROM pipeline_polls WHERE candidate_key IN (?,?) "
                "OR lower(candidate_name)=? "
                "ORDER BY created_at DESC LIMIT 20", (key, dashed, key))]
            name = out["name"]
            out["emails"] = [dict(r) for r in conn.execute(
                "SELECT message_id, subject, from_name, from_email, "
                "internal_date_ms, extraction_verdict "
                "FROM emails WHERE subject LIKE ? OR body_plain LIKE ? "
                "ORDER BY internal_date_ms DESC LIMIT 10",
                (f"%{name}%", f"%{name}%"))]
        finally:
            conn.close()
    return _json_resp(200, out)


# ---------------------------------------------------------------------------
# Batch — loops the SAME singular handlers so every per-item invariant
# (CAS, audit columns, routing) holds; returns per-item outcomes.
# ---------------------------------------------------------------------------

_BATCH_ACTIONS = {
    "rules.approve": lambda p, i, params: _rule_approve(p, i, params),
    "rules.reject": lambda p, i, params: _rule_reject(p, i, params),
    "lessons.approve": lambda p, i, params: _lesson_approve(p, i, params),
    "lessons.reject": lambda p, i, params: _lesson_reject(p, i, params),
    "feedback.reject": lambda p, i, params: _feedback_reject(p, i, params),
    "nuggets.approve": lambda p, i, params: _nugget_approve(p, i, params),
    "nuggets.dismiss": lambda p, i, params: _nugget_dismiss(p, i, params),
}


def _batch(p: Principal, body: bytes) -> Response:
    data = _parse_body(body)
    action = str(data.get("action", ""))
    ids = data.get("ids") or []
    fn = _BATCH_ACTIONS.get(action)
    if not fn:
        return _err(400, f"unknown batch action {action!r}")
    if not isinstance(ids, list) or not ids or len(ids) > 100:
        return _err(400, "ids must be a list of 1..100 integers")
    params = json.dumps(data.get("params") or {}).encode()
    results = []
    for raw_id in ids:
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError):
            results.append({"id": raw_id, "ok": False, "error": "bad id"})
            continue
        status, _hdrs, out = fn(p, item_id, params)
        payload = json.loads(out.decode() or "{}")
        payload["id"] = item_id
        payload.setdefault("ok", status < 400)
        results.append(payload)
    n_ok = sum(1 for r in results if r.get("ok"))
    return _json_resp(200, {"ok": True, "done": n_ok,
                            "failed": len(results) - n_ok,
                            "results": results})


# ---------------------------------------------------------------------------
# System health + ops — Phase 6
# Health is read-only and safe anywhere. Ops (resync / restart / tunnel)
# are super_admin-gated AND refuse to run unless the panel is serving
# from the real production home — a dev sandbox can never fire them.
# ---------------------------------------------------------------------------

_TAILSCALE = shutil.which("tailscale") or \
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
_ops_lock = threading.Lock()
_ops_state: Dict[str, Any] = {"resync_started_at": None,
                              "restart_requested_at": None}


def _run_cmd(cmd: List[str], timeout: int = 6) -> Tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, str(e)


def _health(p: Principal, query: Dict[str, str]) -> Response:
    home = get_home()
    out: Dict[str, Any] = {"ok": True, "now": time.time()}

    # OAuth — both identities
    oauth: Dict[str, Any] = {}
    try:
        import lark_oauth
        for ident in ("operator", "noah"):
            try:
                oauth[ident] = lark_oauth.status(ident) or {}
            except Exception as e:
                oauth[ident] = {"error": str(e)[:120]}
    except Exception as e:
        oauth["error"] = str(e)[:120]
    out["oauth"] = oauth
    alert = os.path.join(home, "lark", "oauth_alert.txt")
    out["oauth_alert"] = None
    if os.path.exists(alert):
        try:
            out["oauth_alert"] = open(alert).read()[:600]
        except Exception:
            out["oauth_alert"] = "(unreadable alert file)"

    # Bot process
    stats_path = os.path.join(home, "lark", "bot_stats.json")
    bot: Dict[str, Any] = {}
    try:
        with open(stats_path) as f:
            bot = json.load(f) or {}
    except Exception:
        pass
    rc, pids = _run_cmd(["pgrep", "-f", "lark_bot.py serve"])
    bot["pid"] = pids.split()[0] if rc == 0 and pids.split() else None
    b_rc, branch = _run_cmd(["git", "-C", _real_home(), "rev-parse",
                             "--abbrev-ref", "HEAD"])
    c_rc, commit = _run_cmd(["git", "-C", _real_home(), "rev-parse",
                             "--short", "HEAD"])
    bot["branch"] = branch.strip() if b_rc == 0 else "?"
    bot["commit"] = commit.strip() if c_rc == 0 else "?"
    out["bot"] = bot

    # launchd jobs
    rc, ls = _run_cmd(["launchctl", "list"])
    jobs = []
    if rc == 0:
        for line in ls.splitlines():
            if "com.noto." in line:
                parts = line.split()
                if len(parts) >= 3:
                    jobs.append({"pid": None if parts[0] == "-" else parts[0],
                                 "last_exit": parts[1], "label": parts[2]})
    out["launchd"] = sorted(jobs, key=lambda j: j["label"])

    # Tailscale Funnel
    rc, fs = _run_cmd([_TAILSCALE, "funnel", "status"])
    out["funnel"] = {"ok": rc == 0 and "127.0.0.1" in fs,
                     "raw": fs.strip()[:1500]}

    # Resync + keepalive logs
    resync_log = os.path.join(home, "lark", "resync.log")
    resync: Dict[str, Any] = {"log_mtime": None, "last_done": None,
                              "tail": []}
    try:
        resync["log_mtime"] = os.path.getmtime(resync_log)
        with open(resync_log, "rb") as f:
            f.seek(max(0, os.path.getsize(resync_log) - 16000))
            lines = f.read().decode(errors="replace").splitlines()
        resync["tail"] = lines[-25:]
        for ln in reversed(lines):
            if "resync done" in ln:
                resync["last_done"] = ln
                break
    except OSError:
        pass
    rc, _pid = _run_cmd(["pgrep", "-f", "noto-resync.sh"])
    resync["running"] = rc == 0
    resync["started_via_panel_at"] = _ops_state["resync_started_at"]
    out["resync"] = resync
    keep = os.path.join(home, "lark", "oauth-keepalive.log")
    out["keepalive_log_mtime"] = _mtime_or_none(keep)

    # Index freshness
    idx_dir = os.path.join(home, "indexes")
    freshness = []
    try:
        for name in sorted(os.listdir(idx_dir)):
            if not name.endswith((".db", ".mv2")):
                continue
            path = os.path.join(idx_dir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            freshness.append({"name": name, "mtime": st.st_mtime,
                              "size": st.st_size})
    except OSError:
        pass
    out["indexes"] = freshness
    out["ops_enabled"] = _ops_allowed()
    return _json_resp(200, out)


def _mtime_or_none(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _ops_allowed() -> bool:
    return os.path.realpath(get_home()) == os.path.realpath(_real_home())


def _ops_guard(p: Principal) -> Optional[Response]:
    if not p.is_super_admin:
        return _err(403, "super_admin only")
    if not _ops_allowed():
        return _err(409, "ops are disabled outside the production home "
                         "(dev sandbox)")
    return None


def _ops_resync(p: Principal, body: bytes) -> Response:
    guard = _ops_guard(p)
    if guard:
        return guard
    with _ops_lock:
        rc, _ = _run_cmd(["pgrep", "-f", "noto-resync.sh"])
        if rc == 0:
            return _err(409, "a resync is already running")
        home = _real_home()
        log = open(os.path.join(home, "lark", "resync.log"), "ab")
        subprocess.Popen(
            ["bash", os.path.join(home, "tools", "noto-resync.sh")],
            stdout=log, stderr=subprocess.STDOUT,
            cwd=home, start_new_session=True,
            env={**os.environ, "LOLABOT_HOME": home})
        _ops_state["resync_started_at"] = time.time()
    _audit(p.open_id, p.name, "ops.resync_started")
    return _json_resp(200, {"ok": True, "message": "resync started — "
                            "watch the log tail on this page"})


def _ops_restart_bot(p: Principal, body: bytes) -> Response:
    guard = _ops_guard(p)
    if guard:
        return guard
    if str(_parse_body(body).get("confirm", "")) != "RESTART":
        return _err(400, 'type RESTART to confirm — this drops all '
                         'in-flight webhook work')
    rc, pids = _run_cmd(["pgrep", "-f", "lark_bot.py serve"])
    pid = pids.split()[0] if rc == 0 and pids.split() else ""
    home = _real_home()
    # A detached helper does the kill + relaunch so the HTTP response
    # (possibly served BY the bot being restarted) gets out first.
    script = (
        f"sleep 1; "
        f"{f'kill {pid}; ' if pid else ''}"
        f"for i in $(seq 1 20); do pgrep -f 'lark_bot.py serve' "
        f">/dev/null || break; sleep 0.5; done; "
        f"cd {home}; unset LOLABOT_HOME; "
        f"nohup bash tools/lark-bot-run.sh "
        f">> lark/bot-restart.log 2>&1 &")
    subprocess.Popen(["bash", "-c", script], start_new_session=True,
                     cwd=home)
    _ops_state["restart_requested_at"] = time.time()
    _audit(p.open_id, p.name, "ops.restart_bot", pid or "(not running)")
    return _json_resp(200, {"ok": True, "killed_pid": pid or None,
                            "message": "restart dispatched — the panel "
                            "will reconnect when the bot is back"})


def _ops_tunnel(p: Principal, body: bytes) -> Response:
    guard = _ops_guard(p)
    if guard:
        return guard
    cfg = load_config()
    listen = str((cfg.get("lark") or {}).get("webhook_listen",
                                             "127.0.0.1:8088"))
    port = listen.split(":")[-1]
    rc, outp = _run_cmd([_TAILSCALE, "funnel", "--bg", "--https=443",
                         f"127.0.0.1:{port}"], timeout=15)
    _audit(p.open_id, p.name, "ops.tunnel_reassert", "",
           {"port": port}, {"rc": rc, "out": outp[:300]})
    if rc != 0:
        return _err(502, f"tailscale funnel failed: {outp[:200]}")
    return _json_resp(200, {"ok": True, "out": outp[:400]})


# ---------------------------------------------------------------------------
# Recruiter usage analytics — Phase 7
# Reads via UsageStore.get() (the process-wide singleton — its rule) plus
# read-only SQL for latency and per-user daily series. Recruiter-memory
# content is super_admin-only, enforced here server-side.
# ---------------------------------------------------------------------------

def _usage_extra(days: int) -> Tuple[Dict[str, Any], Dict[str, List[int]]]:
    """avg latency per user + per-user daily message counts (sparklines)."""
    path = os.path.join(get_home(), "indexes", "usage.db")
    lat: Dict[str, Any] = {}
    spark: Dict[str, List[int]] = {}
    if not os.path.exists(path):
        return lat, spark
    cutoff = time.time() - days * 86400
    conn = sqlite3.connect(path)
    harden(conn)
    try:
        for oid, avg_ms, n in conn.execute(
                "SELECT user_open_id, AVG(duration_ms), COUNT(*) FROM tokens "
                "WHERE ts >= ? AND user_open_id IS NOT NULL "
                "AND duration_ms > 0 GROUP BY user_open_id", (cutoff,)):
            lat[oid] = {"avg_ms": avg_ms, "n": n}
        day0 = time.time() - 14 * 86400
        rows = conn.execute(
            "SELECT user_open_id, CAST((ts - ?) / 86400 AS INT) d, COUNT(*) "
            "FROM messages WHERE ts >= ? GROUP BY user_open_id, d",
            (day0, day0)).fetchall()
        for oid, d, n in rows:
            spark.setdefault(oid, [0] * 15)
            if 0 <= d <= 14:
                spark[oid][d] = n
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    return lat, spark


def _chat_roster(days: int) -> Dict[str, Dict[str, Any]]:
    """The FULL team roster from the synced chat corpus — everyone who
    talks in company chats, not just people who've addressed the bot.
    Bot + service accounts excluded."""
    path = os.path.join(get_home(), "indexes", "chat_messages.db")
    roster: Dict[str, Dict[str, Any]] = {}
    if not os.path.exists(path):
        return roster
    now_ms = time.time() * 1000
    cut7 = now_ms - days * 86400_000
    cut30 = now_ms - 30 * 86400_000
    conn = sqlite3.connect(path)
    harden(conn)
    try:
        for oid, nm, m7, m30, last in conn.execute(
                "SELECT sender_open_id, MAX(sender_name), "
                "SUM(created_at_ms >= ?), SUM(created_at_ms >= ?), "
                "MAX(created_at_ms) FROM chat_messages "
                "WHERE sender_open_id != '' "
                "AND COALESCE(sender_authority,'') != 'bot' "
                "GROUP BY sender_open_id", (cut7, cut30)):
            roster[oid] = {"open_id": oid, "name": nm or "",
                           "chat_msgs_7d": int(m7 or 0),
                           "chat_msgs_30d": int(m30 or 0),
                           "chat_last_ms": last}
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    return roster


def _recruiters_overview(p: Principal, query: Dict[str, str]) -> Response:
    days = min(int(query.get("days", "7") or 7), 90)
    try:
        from usage_store import UsageStore
        s = UsageStore.get()
        users = s.users_summary(window_days=days)
        kpis = s.headline_kpis(window_days=days)
        wf = s.workflows_breakdown(window_days=days)
        daily = s.messages_by_day(days=14)
    except Exception as e:
        return _err(500, f"usage store unavailable: {e}")
    lat, spark = _usage_extra(days)
    real = [u for u in users
            if not str(u.get("open_id", "")).startswith(("ou_test",
                                                         "ou_smoke"))]
    for u in real:
        u["latency"] = lat.get(u["open_id"])
        u["spark"] = spark.get(u["open_id"], [])
    # Merge the chat-corpus roster: the whole team, with chat activity —
    # bot-usage stats attach where they exist. (Previously the page only
    # showed people who had addressed the bot, which made it meaningless
    # as a team view.)
    roster = _chat_roster(days)
    by_oid = {u["open_id"]: u for u in real}
    for oid, rec in roster.items():
        u = by_oid.get(oid)
        if u:
            u["chat_msgs_7d"] = rec["chat_msgs_7d"]
            u["chat_msgs_30d"] = rec["chat_msgs_30d"]
            u["chat_last_ms"] = rec["chat_last_ms"]
            if rec["name"] and not (u.get("display_name") or "").strip():
                u["display_name"] = rec["name"]
        else:
            real.append({"open_id": oid, "display_name": rec["name"],
                         "msgs_window": 0, "msgs_30d": 0,
                         "chat_msgs_7d": rec["chat_msgs_7d"],
                         "chat_msgs_30d": rec["chat_msgs_30d"],
                         "chat_last_ms": rec["chat_last_ms"],
                         "spark": [], "latency": None})
    real.sort(key=lambda u: (-(u.get("msgs_window") or 0),
                             -(u.get("chat_msgs_7d") or 0)))
    lat_all = [v["avg_ms"] for v in lat.values() if v.get("avg_ms")]
    try:
        actions = s.actions_breakdown(window_days=30)
        actions_by_user = s.actions_by_user(window_days=30)
    except Exception:
        actions, actions_by_user = [], []
    return _json_resp(200, {
        "ok": True, "days": days, "users": real, "kpis": kpis,
        "workflows": wf, "daily": daily,
        "actions": actions, "actions_by_user": actions_by_user,
        "avg_latency_ms": (sum(lat_all) / len(lat_all)) if lat_all else None,
    })


def _recruiter_detail(p: Principal, query: Dict[str, str]) -> Response:
    oid = (query.get("oid") or "").strip()
    if not re.fullmatch(r"ou_[0-9a-f]{16,64}", oid):
        return _err(400, "bad open_id")
    days = min(int(query.get("days", "30") or 30), 90)
    try:
        from usage_store import UsageStore
        s = UsageStore.get()
        user = s.get_user(oid)
        wf = s.workflows_breakdown(window_days=days, user_open_id=oid)
        daily = s.messages_by_day(days=30, user_open_id=oid)
    except Exception as e:
        return _err(500, f"usage store unavailable: {e}")
    lat, _ = _usage_extra(days)
    try:
        actions = s.actions_for_user(oid, window_days=30)
    except Exception:
        actions = []
    out: Dict[str, Any] = {"ok": True, "user": user, "workflows": wf,
                           "daily": daily, "latency": lat.get(oid),
                           "actions": actions, "days": days}
    # Recruiter memory — DM-derived personal context. super_admin ONLY.
    if p.is_super_admin:
        try:
            from recruiter_memory import list_facts, tail_write_log
            out["memory"] = {
                "facts": list_facts(oid),
                "write_log": tail_write_log(oid, n=15),
            }
        except Exception as e:
            out["memory"] = {"error": str(e)[:120]}
    return _json_resp(200, out)


# ---------------------------------------------------------------------------
# Admin workspace endpoints (super_admin)
# ---------------------------------------------------------------------------

def _admin_users_list(p: Principal) -> Response:
    conn = _connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT open_id, name, role, added_by, added_at, disabled "
            "FROM panel_users ORDER BY added_at").fetchall()]
    finally:
        conn.close()
    return _json_resp(200, {"ok": True, "users": rows})


def _admin_users_upsert(p: Principal, body: bytes) -> Response:
    data = _parse_body(body)
    open_id = str(data.get("open_id", "")).strip()
    name = str(data.get("name", "")).strip()
    role = str(data.get("role", "member"))
    disabled = 1 if data.get("disabled") else 0
    if not re.fullmatch(r"ou_[0-9a-f]{16,64}", open_id):
        return _err(400, "open_id must look like ou_<hex>")
    if role not in ("member", "super_admin"):
        return _err(400, "role must be member or super_admin")
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT open_id, name FROM panel_users WHERE open_id=?",
            (open_id,)).fetchone()
        if existing:
            # Safety: a super_admin cannot demote/disable themself, and the
            # last enabled super_admin can never be demoted or disabled.
            if (role != "super_admin" or disabled) and _is_last_super_admin(
                    conn, open_id):
                return _err(409, "cannot demote or disable the last "
                                 "enabled super_admin")
            if open_id == p.open_id and (role != p.role or disabled):
                return _err(409, "you cannot change or disable your own "
                                 "account")
            conn.execute(
                "UPDATE panel_users SET name=?, role=?, disabled=? "
                "WHERE open_id=?", (name or existing["name"], role,
                                    disabled, open_id))
            action = "admin.user_updated"
        else:
            conn.execute(
                "INSERT INTO panel_users (open_id, name, role, added_by, "
                "added_at, disabled) VALUES (?,?,?,?,?,?)",
                (open_id, name, role, p.open_id, time.time(), disabled))
            action = "admin.user_added"
        if disabled:
            conn.execute("UPDATE sessions SET revoked=1 WHERE open_id=?",
                         (open_id,))
        conn.commit()
    finally:
        conn.close()
    _audit(p.open_id, p.name, action, open_id,
           {"name": name, "role": role, "disabled": disabled})
    return _json_resp(200, {"ok": True})


def _is_last_super_admin(conn: sqlite3.Connection, open_id: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM panel_users "
        "WHERE role='super_admin' AND disabled=0 AND open_id != ?",
        (open_id,)).fetchone()
    me = conn.execute(
        "SELECT 1 FROM panel_users WHERE open_id=? AND role='super_admin' "
        "AND disabled=0", (open_id,)).fetchone()
    return bool(me) and row[0] == 0


def _admin_sessions_list(p: Principal) -> Response:
    conn = _connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT substr(token_hash,1,12) AS id, open_id, role_at_login, "
            "via, user_agent, created_at, last_seen_at, expires_at "
            "FROM sessions WHERE revoked=0 AND expires_at > ? "
            "ORDER BY last_seen_at DESC", (time.time(),)).fetchall()]
    finally:
        conn.close()
    return _json_resp(200, {"ok": True, "sessions": rows})


def _admin_sessions_revoke(p: Principal, body: bytes) -> Response:
    sid = str(_parse_body(body).get("id", ""))
    if not re.fullmatch(r"[0-9a-f]{12}", sid):
        return _err(400, "bad session id")
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE sessions SET revoked=1 WHERE substr(token_hash,1,12)=?",
            (sid,))
        conn.commit()
        n = cur.rowcount
    finally:
        conn.close()
    _audit(p.open_id, p.name, "admin.session_revoked", sid)
    return _json_resp(200, {"ok": True, "revoked": n})


def _admin_audit_list(p: Principal, query: Dict[str, str]) -> Response:
    limit = min(int(query.get("limit", "100") or 100), 500)
    conn = _connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT ts, actor_open_id, actor_name, action, target, "
            "payload_json FROM panel_audit ORDER BY ts DESC LIMIT ?",
            (limit,)).fetchall()]
    finally:
        conn.close()
    return _json_resp(200, {"ok": True, "audit": rows})


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

# (method, exact-path or regex) -> handler. Handlers receive a context dict.
# auth levels: None (public), 'member', 'super_admin'.

def handle(method: str, path: str, query: Dict[str, str],
           headers: Dict[str, str], body: bytes) -> Response:
    """Single entry point. lark_bot.py delegates every /admin* request
    here; the dev server (below) does the same."""
    headers = {k.lower(): v for k, v in headers.items()}
    method = method.upper()
    path = path.rstrip("/") or "/admin"

    if method == "OPTIONS":
        # Same-origin app; no CORS surface on purpose.
        return 204, [("Allow", "GET, POST, OPTIONS")], b""

    # --- public routes ----------------------------------------------------
    if method == "GET" and path == "/admin":
        return _serve_static("index.html")
    if method == "GET" and path.startswith("/admin/static/"):
        return _serve_static(path.rsplit("/", 1)[-1])
    if method == "GET" and path == "/admin/auth/confirm":
        return _auth_confirm(headers, query)
    if method == "POST" and path == "/admin/api/auth/request-link":
        return _auth_request_link(headers, body)
    if method == "POST" and path == "/admin/api/auth/key":
        return _auth_key(headers, body)

    # --- everything below needs a session ----------------------------------
    p = _current_principal(headers)
    if not p:
        return _err(401, "not signed in")

    if method == "POST":
        # CSRF: all mutations demand the session's csrf token — normally in
        # a header; sendBeacon flushes (pagehide) can't set headers, so a
        # `_csrf` body field is accepted as the equivalent.
        supplied = headers.get("x-noto-csrf", "") \
            or str(_parse_body(body).get("_csrf", ""))
        if not hmac.compare_digest(supplied, p.csrf_token):
            return _err(403, "missing or stale CSRF token (reload the page)")

    if method == "POST" and path == "/admin/api/auth/logout":
        return _auth_logout(headers)
    if method == "GET" and path == "/admin/api/me":
        return _me(p)
    if method == "GET" and path == "/admin/api/inbox/counts":
        return _inbox_counts(p)

    # --- inbox queues (Phase 2) --------------------------------------------
    if method == "GET" and path == "/admin/api/rules":
        return _rules_list(p, query)
    if method == "GET" and path == "/admin/api/feedback":
        return _feedback_list(p, query)
    if method == "POST" and path == "/admin/api/batch":
        return _batch(p, body)
    m = re.fullmatch(r"/admin/api/rules/(\d+)/(approve|reject)", path)
    if m and method == "POST":
        fn = _rule_approve if m.group(2) == "approve" else _rule_reject
        return fn(p, int(m.group(1)), body)
    # NOTE: no direct feedback-accept route. Raw feedback becomes a rule
    # only through an approved derived lesson (feedback_synthesis).
    m = re.fullmatch(r"/admin/api/feedback/(\d+)/(reject|reclassify)", path)
    if m and method == "POST":
        fid = int(m.group(1))
        return {"reject": _feedback_reject,
                "reclassify": _feedback_reclassify}[m.group(2)](p, fid, body)
    if method == "GET" and path == "/admin/api/lessons":
        return _lessons_list(p, query)
    if method == "POST" and path == "/admin/api/lessons/synthesize":
        return _lessons_synthesize(p, body)
    m = re.fullmatch(r"/admin/api/lessons/(\d+)/(approve|reject)", path)
    if m and method == "POST":
        fn = _lesson_approve if m.group(2) == "approve" else _lesson_reject
        return fn(p, int(m.group(1)), body)
    m = re.fullmatch(r"/admin/api/rules/(\d+)/evidence", path)
    if m and method == "GET":
        return _rule_evidence(p, int(m.group(1)))
    if method == "GET" and path == "/admin/api/nuggets":
        return _nuggets_list(p, query)
    if method == "GET" and path == "/admin/api/pipeline":
        return _pipeline_list(p, query)
    if method == "GET" and path == "/admin/api/pipeline/email":
        return _pipeline_email(p, query)
    if method == "GET" and path == "/admin/api/candidates":
        return _candidates_list(p, query)
    if method == "GET" and path == "/admin/api/candidates/bitable":
        return _candidates_bitable(p, query)
    if method == "GET" and path == "/admin/api/candidates/detail":
        return _candidate_detail(p, query)
    if method == "GET" and path == "/admin/api/health":
        return _health(p, query)
    if method == "GET" and path == "/admin/api/recruiters":
        return _recruiters_overview(p, query)
    if method == "GET" and path == "/admin/api/recruiters/detail":
        return _recruiter_detail(p, query)
    if method == "POST" and path == "/admin/api/ops/resync":
        return _ops_resync(p, body)
    if method == "POST" and path == "/admin/api/ops/restart-bot":
        return _ops_restart_bot(p, body)
    if method == "POST" and path == "/admin/api/ops/tunnel":
        return _ops_tunnel(p, body)
    if method == "GET" and path == "/admin/api/pipeline/polls":
        return _pipeline_polls_list(p, query)
    m = re.fullmatch(r"/admin/api/pipeline/poll/(\d+)/(approve|reject)", path)
    if m and method == "POST":
        return _poll_resolve(p, int(m.group(1)), m.group(2), body)
    m = re.fullmatch(r"/admin/api/nuggets/(\d+)/(approve|dismiss|contextualize)",
                     path)
    if m and method == "POST":
        return {"approve": _nugget_approve,
                "dismiss": _nugget_dismiss,
                "contextualize": _nugget_contextualize}[m.group(2)](
                    p, int(m.group(1)), body)

    # --- super_admin routes -------------------------------------------------
    sa_routes = {
        ("GET", "/admin/api/admin/users"):
            lambda: _admin_users_list(p),
        ("POST", "/admin/api/admin/users"):
            lambda: _admin_users_upsert(p, body),
        ("GET", "/admin/api/admin/sessions"):
            lambda: _admin_sessions_list(p),
        ("POST", "/admin/api/admin/sessions/revoke"):
            lambda: _admin_sessions_revoke(p, body),
        ("GET", "/admin/api/admin/audit"):
            lambda: _admin_audit_list(p, query),
    }
    fn = sa_routes.get((method, path))
    if fn:
        if not p.is_super_admin:
            return _err(403, "super_admin only")
        return fn()

    return _err(404, "no such endpoint")


# ---------------------------------------------------------------------------
# Dev server — standalone, NEVER the production process
# ---------------------------------------------------------------------------

def _real_home() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def serve(port: int, allow_prod: bool = False) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    if os.path.realpath(get_home()) == os.path.realpath(_real_home()) \
            and not allow_prod:
        print("REFUSING to start: LOLABOT_HOME resolves to the production "
              "home. Point it at a sandbox (tools/admin_dev_sandbox.sh) or "
              "pass --allow-prod if you really mean it.")
        sys.exit(2)

    class DevHandler(BaseHTTPRequestHandler):
        server_version = "NotoAdminDev"

        def _dispatch(self, body: bytes = b"") -> None:
            u = urlparse(self.path)
            if not (u.path == "/admin" or u.path.startswith("/admin/")):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"admin panel dev server: /admin only")
                return
            query = {k: v[0] for k, v in parse_qs(u.query).items()}
            status, hdrs, out = handle(
                self.command, u.path, query, dict(self.headers), body)
            self.send_response(status)
            for k, v in hdrs:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):            # noqa: N802
            self._dispatch()

        def do_POST(self):           # noqa: N802
            n = int(self.headers.get("Content-Length", 0) or 0)
            self._dispatch(self.rfile.read(n) if n else b"")

        def do_OPTIONS(self):        # noqa: N802
            self._dispatch()

        def log_message(self, fmt, *args):
            print(f"[dev] {self.address_string()} {fmt % args}")

    httpd = ThreadingHTTPServer(("127.0.0.1", port), DevHandler)
    print(f"admin panel dev server: http://127.0.0.1:{port}/admin "
          f"(home={get_home()})")
    httpd.serve_forever()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Noto admin panel")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="standalone dev server")
    sp.add_argument("--port", type=int, default=8089)
    sp.add_argument("--allow-prod", action="store_true")
    args = ap.parse_args()
    if args.cmd == "serve":
        serve(args.port, allow_prod=args.allow_prod)
