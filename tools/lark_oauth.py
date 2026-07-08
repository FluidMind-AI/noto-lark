#!/usr/bin/env python3
"""
Lark user-token OAuth (authorization code flow) — Noto Lark.

The app identity (tenant_access_token) cannot enumerate the wiki. To
ingest the whole company corpus, Noto authenticates as a dedicated Lark
service user ("Noto", alejandro@fluidmind.ai) and calls wiki/docx APIs
with that user's user_access_token — which sees everything that user
can see.

Flow:
  1. authorize_url()  -> a link; open it in a browser logged in as the
     Noto service user, approve.
  2. Lark redirects to  https://<funnel>/lark/oauth/callback?code=...
     -> lark_bot's GET handler calls exchange_code().
  3. get_user_token()  -> a valid token, auto-refreshing via the stored
     refresh_token.

Endpoints (Lark International, confirmed from open.larksuite.com docs):
  authorize: https://accounts.larksuite.com/open-apis/authen/v1/authorize
  token:     https://open.larksuite.com/open-apis/authen/v2/oauth/token

Token cache: lark/user_token.json (git-ignored).
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load_config, get_home  # noqa: E402
from lark_client import load_lark_credentials, _base_url  # noqa: E402

AUTHORIZE_URL = "https://accounts.larksuite.com/open-apis/authen/v1/authorize"
# User scopes Noto needs. offline_access -> refresh_token (lets the bot
# keep working past the 2h access_token TTL without a manual re-auth);
# docx:document and sheets:spreadsheet are full read+WRITE (create
# target-list docs / pipeline-report sheets); drive:drive is full
# read+WRITE so the bot can CREATE folders (e.g. "Noto Outputs",
# per-candidate auto-foldering) on top of moving / writing Drive
# files; wiki is read for the corpus walk. Deletion is blocked at the
# code layer by assert_no_lark_delete() — write scope ≠ delete
# capability here.
# Per-identity scope sets. The operator identity ("alejandro") drives
# the corpus walk + doc/sheet writes. The "noah" service identity adds
# mail + calendar so it can read the dedicated pipeline mailbox and
# create candidate-interview events. Identities are kept separate so a
# re-OAuth of one never blows away the other's token.
DEFAULT_SCOPES = ("offline_access wiki:wiki:readonly "
                  "docx:document drive:drive "
                  "sheets:spreadsheet "
                  "bitable:app")

NOAH_SCOPES = ("offline_access "
               "mail:user_mailbox.message:readonly "
               "calendar:calendar "
               "contact:user.email:readonly "
               # task v2: per-user reminder lists (lark_tasks.py).
               # Write scope ≠ delete capability — lark_tasks.py has
               # no removal code and is covered by the delete-scan.
               "task:task:read task:task:write "
               "task:tasklist:read task:tasklist:write")

_SCOPES_BY_IDENTITY = {
    "operator": DEFAULT_SCOPES,
    "alejandro": DEFAULT_SCOPES,    # alias
    "noah":     NOAH_SCOPES,
}


def _resolve_identity(identity: Optional[str]) -> str:
    """Normalize identity name. Empty/None -> 'operator' (back-compat)."""
    if not identity:
        return "operator"
    i = identity.strip().lower()
    if i not in _SCOPES_BY_IDENTITY:
        raise ValueError(f"unknown OAuth identity {identity!r} — "
                         f"valid: {sorted(_SCOPES_BY_IDENTITY)}")
    return "operator" if i == "alejandro" else i


def _scopes_for(identity: str) -> str:
    return _SCOPES_BY_IDENTITY[_resolve_identity(identity)]


def _token_path(identity: Optional[str] = None) -> str:
    """Per-identity token file. 'operator' uses the original
    user_token.json (back-compat: no rename, no migration). Other
    identities get user_token_<identity>.json."""
    rel = (load_config().get("lark", {}) or {}).get(
        "lark_cache_dir", None)
    base = os.path.join(get_home(), "lark")
    os.makedirs(base, exist_ok=True)
    ident = _resolve_identity(identity)
    if ident == "operator":
        return os.path.join(base, "user_token.json")
    return os.path.join(base, f"user_token_{ident}.json")


def _redirect_uri() -> str:
    cfg = load_config().get("lark", {}) or {}
    host = cfg.get("funnel_host", "")
    return f"https://{host}/lark/oauth/callback"


# ---------------------------------------------------------------------------
# Step 1 — authorization URL
# ---------------------------------------------------------------------------

def authorize_url(identity: Optional[str] = None,
                  state: Optional[str] = None) -> str:
    """Build the OAuth authorize URL for the given identity. `state`
    encodes the identity so the callback can route the resulting token
    to the right slot; if you don't pass `state`, identity is used."""
    creds = load_lark_credentials()
    if not creds["app_id"]:
        raise RuntimeError("LARK app_id missing — cannot build authorize URL")
    ident = _resolve_identity(identity)
    params = {
        "client_id": creds["app_id"],
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": _scopes_for(ident),
        "state": state or ident,
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# Token endpoint helpers
# ---------------------------------------------------------------------------

def _token_request(body: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{_base_url()}/open-apis/authen/v2/oauth/token"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OAuth token HTTP {e.code}: {e.read().decode()}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"OAuth token request failed: {e}")
    # v2 returns access_token at top level; code!=0 means error
    if payload.get("code") not in (0, None) and "access_token" not in payload:
        raise RuntimeError(f"OAuth token error: {payload}")
    return payload


def _store(payload: Dict[str, Any],
           identity: Optional[str] = None) -> Dict[str, Any]:
    now = int(time.time())
    ident = _resolve_identity(identity)
    # Diagnostic — what scopes did Lark actually grant, and did we get a
    # refresh_token? Helps spot the "offline_access not approved" case
    # (access_token works ~2h, then dies unrenewable).
    print(f"[lark_oauth] exchange identity={ident}: has_refresh="
          f"{bool(payload.get('refresh_token'))} scope={payload.get('scope')!r} "
          f"refresh_ttl_s={payload.get('refresh_token_expires_in')}",
          file=sys.stderr, flush=True)
    # Lark's refresh-grant response does NOT echo the refresh_token — so
    # preserve the existing one (and its expiry) when the payload omits it.
    prev = _load(ident) or {}
    refresh_token = payload.get("refresh_token") or prev.get(
        "refresh_token", "")
    if payload.get("refresh_token_expires_in"):
        refresh_expires_at = now + int(
            payload["refresh_token_expires_in"]) - 120
    else:
        refresh_expires_at = prev.get(
            "refresh_expires_at", now + 30 * 24 * 3600)
    rec = {
        "access_token": payload["access_token"],
        "refresh_token": refresh_token,
        "expires_at": now + int(payload.get("expires_in", 7200)) - 120,
        "refresh_expires_at": refresh_expires_at,
        "obtained_at": now,
        "identity": ident,
    }
    p = _token_path(ident)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return rec


def _load(identity: Optional[str] = None) -> Optional[Dict[str, Any]]:
    p = _token_path(identity)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Step 2 — exchange code (called by the bot's OAuth callback)
# ---------------------------------------------------------------------------

def exchange_code(code: str,
                  identity: Optional[str] = None) -> Dict[str, Any]:
    creds = load_lark_credentials()
    payload = _token_request({
        "grant_type": "authorization_code",
        "client_id": creds["app_id"],
        "client_secret": creds["app_secret"],
        "code": code,
        "redirect_uri": _redirect_uri(),
    })
    return _store(payload, identity=identity)


def _refresh(rec: Dict[str, Any],
             identity: Optional[str] = None) -> Dict[str, Any]:
    creds = load_lark_credentials()
    if not rec.get("refresh_token"):
        raise RuntimeError("no refresh_token stored — re-authorize")
    if time.time() > rec.get("refresh_expires_at", 0):
        raise RuntimeError("refresh_token expired — re-authorize via "
                           "authorize_url()")
    payload = _token_request({
        "grant_type": "refresh_token",
        "client_id": creds["app_id"],
        "client_secret": creds["app_secret"],
        "refresh_token": rec["refresh_token"],
    })
    return _store(payload, identity=identity or rec.get("identity"))


# ---------------------------------------------------------------------------
# Step 3 — get a valid user token (auto-refresh)
# ---------------------------------------------------------------------------

def get_user_token(identity: Optional[str] = None) -> str:
    ident = _resolve_identity(identity)
    rec = _load(ident)
    if not rec:
        raise RuntimeError(
            f"No {ident} user token yet. Run: python tools/lark_oauth.py "
            f"url --identity {ident} — open the link as the {ident} "
            "user and approve.")
    if time.time() < rec["expires_at"]:
        return rec["access_token"]
    rec = _refresh(rec, identity=ident)
    return rec["access_token"]


def status(identity: Optional[str] = None) -> Dict[str, Any]:
    ident = _resolve_identity(identity)
    rec = _load(ident)
    if not rec:
        return {"identity": ident, "authorized": False}
    now = int(time.time())
    return {
        "identity": ident,
        "authorized": True,
        "access_token_valid": now < rec["expires_at"],
        "access_expires_in_s": rec["expires_at"] - now,
        "refresh_valid": now < rec.get("refresh_expires_at", 0),
        "refresh_expires_in_days": round(
            (rec.get("refresh_expires_at", 0) - now) / 86400, 1),
    }


# ---------------------------------------------------------------------------
# Step 4 — keep-alive: nudge the refresh BEFORE either token expires
# ---------------------------------------------------------------------------

# When the refresh window drops below this many days, raise a loud alert
# the operator will notice. The refresh window is only 7 days on this
# tenant, so this is meaningful headroom — not paranoia.
_REFRESH_ALERT_DAYS = 2
_ALERT_FILE = os.path.join(get_home(), "lark", "oauth_alert.txt")


def _write_alert(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(_ALERT_FILE), exist_ok=True)
        is_new = not os.path.exists(_ALERT_FILE)
        with open(_ALERT_FILE, "w") as f:
            f.write(msg + "\n")
        # Push, don't just park a file: the alert file was only read by
        # the admin panel's health page — if nobody opened it inside
        # the 2-day refresh window, tokens died silently (2026-07 ops
        # review #1). DM once per alert episode (file creation).
        if is_new:
            try:
                from engineering_notify import send as _en_send
                _en_send(f"🔑 OAuth alert: {msg[:400]}")
            except Exception:
                pass
    except Exception:
        pass


def _clear_alert() -> None:
    try:
        os.remove(_ALERT_FILE)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def refresh_now(identity: Optional[str] = None) -> Dict[str, Any]:
    """Force a token refresh for one identity. Daily launchd keep-alive
    calls this per-identity so each token rolls before expiry."""
    ident = _resolve_identity(identity)
    rec = _load(ident)
    if not rec:
        msg = (f"KEEPALIVE ({ident}): no stored token — operator must run "
               f"`python tools/lark_oauth.py url --identity {ident}` once "
               "before the keep-alive can do anything.")
        print(msg, file=sys.stderr, flush=True)
        _write_alert(msg)
        raise RuntimeError(f"no stored token for {ident}")
    if not rec.get("refresh_token"):
        msg = (f"KEEPALIVE FAIL ({ident}): refresh_token is missing — "
               f"operator must re-authorize. Until then, this identity's "
               "API calls will fail.")
        print(msg, file=sys.stderr, flush=True)
        _write_alert(msg)
        raise RuntimeError(f"no refresh_token for {ident}")

    now = int(time.time())
    refresh_in_days = round((rec.get("refresh_expires_at", 0) - now) / 86400, 1)
    print(f"[keepalive {ident}] before: access_expires_in_s="
          f"{rec['expires_at'] - now} refresh_expires_in_days="
          f"{refresh_in_days}", file=sys.stderr, flush=True)

    old_refresh = rec["refresh_token"]
    try:
        new = _refresh(rec, identity=ident)
    except Exception as e:
        msg = (f"KEEPALIVE FAIL ({ident}): refresh call failed ({e}). If "
               f"refresh_expires_in_days was small, you may need to "
               f"re-authorize via the OAuth URL.")
        print(msg, file=sys.stderr, flush=True)
        _write_alert(msg)
        raise

    rolled = (new.get("refresh_token") != old_refresh
              and bool(new.get("refresh_token")))
    s = status(ident)
    print(f"[keepalive {ident}] after: refresh_rotated={rolled} "
          f"access_expires_in_s={s.get('access_expires_in_s')} "
          f"refresh_expires_in_days={s.get('refresh_expires_in_days')}",
          file=sys.stderr, flush=True)

    if (s.get("refresh_expires_in_days") or 0) < _REFRESH_ALERT_DAYS:
        msg = (f"KEEPALIVE WARN ({ident}): refresh_token expires in "
               f"{s.get('refresh_expires_in_days')} days "
               f"(rolled={rolled}). If it's not rolling, plan to "
               f"re-authorize soon via the OAuth URL.")
        print(msg, file=sys.stderr, flush=True)
        _write_alert(msg)
    else:
        _clear_alert()
    return {"rolled": rolled, **s}


def refresh_all() -> Dict[str, Any]:
    """Keep-alive entry: refresh every identity that has a stored token.
    Wraps refresh_now() per identity so one failure doesn't block the
    others. Returns {<identity>: result-or-error}."""
    out: Dict[str, Any] = {}
    for ident in ("operator", "noah"):
        if not os.path.exists(_token_path(ident)):
            continue
        try:
            out[ident] = refresh_now(ident)
        except Exception as e:
            out[ident] = {"error": str(e)}
    return out


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Lark user OAuth — Noto")
    p.add_argument("--identity", default="operator",
                   help="which identity (operator|noah). default: operator")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("url", help="print the authorization URL")
    sub.add_parser("status", help="show stored token status")
    sub.add_parser("token", help="print a valid user token (auto-refresh)")
    sub.add_parser("refresh-now", help="force a refresh now (keep-alive)")
    sub.add_parser("refresh-all", help="refresh every stored identity")
    ex = sub.add_parser("exchange", help="manually exchange a code")
    ex.add_argument("code")
    a = p.parse_args()
    ident = a.identity
    try:
        if a.cmd == "url":
            print(authorize_url(identity=ident))
            return 0
        if a.cmd == "status":
            print(json.dumps(status(ident), indent=2))
            return 0
        if a.cmd == "token":
            t = get_user_token(ident)
            print(f"user_access_token OK (identity={ident}, "
                  f"len={len(t)}, prefix={t[:6]}…)")
            return 0
        if a.cmd == "refresh-now":
            print(json.dumps(refresh_now(ident), indent=2))
            return 0
        if a.cmd == "refresh-all":
            print(json.dumps(refresh_all(), indent=2))
            return 0
        if a.cmd == "exchange":
            print(json.dumps(status(ident), indent=2)
                  if exchange_code(a.code, identity=ident) else "failed")
            return 0
        p.print_help()
        return 0
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
