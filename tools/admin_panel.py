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
        "playbook": _count("email_playbook.db",
                           "SELECT COUNT(*) FROM entries WHERE "
                           "status='active' AND (reviewed_at IS NULL "
                           "OR reviewed_at='')") or 0,
        "nuggets": _count("chat_nuggets.db",
                          "SELECT COUNT(*) FROM chat_nuggets "
                          "WHERE status='pending'"),
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
                f"recruiter_name AS user_name, authority, instruction, "
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
# Batch — loops the SAME singular handlers so every per-item invariant
# (CAS, audit columns, routing) holds; returns per-item outcomes.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Playbook — the house email-response playbook (email_playbook.py), mined
# nightly from the principals' sent mail; ACTIVE entries are canon for the
# auto-drafter. This is the review seat (operator ask 2026-07-23): keep /
# retire each entry, with full provenance — the exact email exchange the
# entry was mined from, so reviewers see HOW Noto reached the conclusion.
# ---------------------------------------------------------------------------

def _pb_conn():
    import email_playbook
    conn = email_playbook._connect()
    for ddl in ("ALTER TABLE entries ADD COLUMN reviewed_at TEXT",
                "ALTER TABLE entries ADD COLUMN reviewed_by TEXT"):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    return conn


def _playbook_list(p: Principal, query: Dict[str, str]) -> Response:
    import email_playbook
    status = query.get("status", "active")
    conn = _pb_conn()
    try:
        sql = "SELECT * FROM entries"
        where, args = [], []
        if status == "unreviewed":
            where.append("status='active' AND (reviewed_at IS NULL"
                         " OR reviewed_at='')")
        elif status != "all":
            where.append("status=?")
            args.append(status)
        if query.get("type"):
            where.append("situation_type=?")
            args.append(query["type"])
        if query.get("source"):
            where.append("source_user=?")
            args.append(query["source"])
        if query.get("q"):
            like = f"%{query['q']}%"
            where.append("(situation LIKE ? OR approach LIKE ?"
                         " OR exemplar LIKE ?)")
            args += [like, like, like]
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(min(int(query.get("limit", "100") or 100), 300))
        rows = [dict(r) for r in conn.execute(sql, args)]
        for r in rows:
            r["exemplar"] = (r.get("exemplar") or "")[:1600]
    finally:
        conn.close()
    return _json_resp(200, {"ok": True, "entries": rows,
                            "stats": email_playbook.stats()})


def _playbook_provenance(p: Principal, eid: int) -> Response:
    """HOW the entry was concluded: the mined SENT reply + the inbound it
    answered, pulled live from the source user's mail mirror."""
    conn = _pb_conn()
    try:
        e = conn.execute("SELECT * FROM entries WHERE id=?",
                         (eid,)).fetchone()
    finally:
        conn.close()
    if not e:
        return _err(404, f"no playbook entry #{eid}")
    e = dict(e)
    out = {"ok": True, "entry": e, "sent": None, "inbound": None}
    try:
        import mail_store
        mc = mail_store._connect(e["source_user"])
        s = mc.execute("SELECT thread_id, date_ms, subject, body_plain"
                       " FROM messages WHERE msg_id=?",
                       (e["source_msg_id"],)).fetchone()
        if s:
            out["sent"] = {"subject": s["subject"],
                           "body": (s["body_plain"] or "")[:3000]}
            i = mc.execute(
                "SELECT from_email, from_name, body_plain FROM messages"
                " WHERE thread_id=? AND label='INBOX' AND date_ms<?"
                " ORDER BY date_ms DESC LIMIT 1",
                (s["thread_id"], s["date_ms"] or 0)).fetchone()
            if i:
                out["inbound"] = {"from": i["from_email"],
                                  "from_name": i["from_name"],
                                  "body": (i["body_plain"] or "")[:2400]}
        mc.close()
    except Exception as ex:
        out["provenance_error"] = str(ex)[:200]
    return _json_resp(200, out)


def _playbook_set(p: Principal, eid: int, body: bytes,
                  new_status: str) -> Response:
    data = _parse_body(body)
    from datetime import datetime as _dt
    now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _pb_conn()
    try:
        cur = conn.execute(
            "UPDATE entries SET status=?, reviewed_at=?, reviewed_by=?"
            " WHERE id=?", (new_status, now, p.name or p.open_id, eid))
        conn.commit()
        ok = cur.rowcount > 0
    finally:
        conn.close()
    _audit(p.open_id, p.name, f"playbook.{new_status}", str(eid),
           {k: v for k, v in data.items() if k != "_csrf"},
           {"ok": ok})
    return _json_resp(200 if ok else 404,
                      {"ok": ok, "id": eid, "status": new_status})


def _playbook_keep(p: Principal, eid: int, body: bytes) -> Response:
    return _playbook_set(p, eid, body, "active")


def _playbook_disable(p: Principal, eid: int, body: bytes) -> Response:
    return _playbook_set(p, eid, body, "disabled")


_BATCH_ACTIONS = {
    "rules.approve": lambda p, i, params: _rule_approve(p, i, params),
    "rules.reject": lambda p, i, params: _rule_reject(p, i, params),
    "lessons.approve": lambda p, i, params: _lesson_approve(p, i, params),
    "lessons.reject": lambda p, i, params: _lesson_reject(p, i, params),
    "feedback.reject": lambda p, i, params: _feedback_reject(p, i, params),
    "nuggets.approve": lambda p, i, params: _nugget_approve(p, i, params),
    "nuggets.dismiss": lambda p, i, params: _nugget_dismiss(p, i, params),
    "playbook.keep": lambda p, i, params: _playbook_keep(p, i, params),
    "playbook.disable": lambda p, i, params: _playbook_disable(p, i, params),
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
# Usage analytics — Phase 7
# Reads via UsageStore.get() (the process-wide singleton — its rule) plus
# read-only SQL for latency and per-user daily series.
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


def _usage_overview(p: Principal, query: Dict[str, str]) -> Response:
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
    real.sort(key=lambda u: -(u.get("msgs_window") or 0))
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
    if method == "GET" and path == "/admin/api/playbook":
        return _playbook_list(p, query)
    m = re.fullmatch(r"/admin/api/playbook/(\d+)/provenance", path)
    if m and method == "GET":
        return _playbook_provenance(p, int(m.group(1)))
    m = re.fullmatch(r"/admin/api/playbook/(\d+)/(keep|disable)", path)
    if m and method == "POST":
        fn = _playbook_keep if m.group(2) == "keep" else _playbook_disable
        return fn(p, int(m.group(1)), body)
    if method == "GET" and path == "/admin/api/nuggets":
        return _nuggets_list(p, query)
    if method == "GET" and path == "/admin/api/health":
        return _health(p, query)
    if method == "GET" and path == "/admin/api/usage":
        return _usage_overview(p, query)
    if method == "POST" and path == "/admin/api/ops/resync":
        return _ops_resync(p, body)
    if method == "POST" and path == "/admin/api/ops/restart-bot":
        return _ops_restart_bot(p, body)
    if method == "POST" and path == "/admin/api/ops/tunnel":
        return _ops_tunnel(p, body)
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
