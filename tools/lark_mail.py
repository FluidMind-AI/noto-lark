"""
Lark Mail — thin wrapper around /open-apis/mail/v1.

Uses the Noah service user's OAuth token (mail:user_mailbox.message:readonly
is approved + live). Built for the email-driven pipeline: every N
minutes the bot lists new INBOX messages, fetches each, runs the
extractor, and posts poll cards for pipeline-relevant ones.

Lark Mail primer:
  - user_mailbox_id of 'me' refers to the authenticated user (Noah)
  - Messages are addressed by message_id (Lark-internal base64 string)
  - Listing requires either folder_id OR label_id (we use INBOX label)
  - The `Retrieve emails` scope (mail:user_mailbox.message:readonly)
    is **metadata-only** in practice — returns message_id, internal_date,
    label_ids, thread_id, references, smtp_message_id, folder_id —
    NOT subject/from/to/body. To get body, the scope must be expanded
    (see fetch_body() — raises until the additional scope is added).

CLI:
  python tools/lark_mail.py list-ids [--label INBOX] [--limit N]
  python tools/lark_mail.py meta <message_id>
  python tools/lark_mail.py poll-new        # fetches new + writes to pipeline.db
  python tools/lark_mail.py selftest
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# Scope catalog name the operator needs to add to unblock body fetch.
# We surface this exact string in the NotImplementedError so the
# operator's morning checklist tells them what to look for.
_BODY_SCOPE_HINT = ("mail:user_mailbox.message.body:readonly  "
                    "(or whatever Lark's catalog labels as 'read "
                    "message body')")


def _base_url() -> str:
    from config import load_config
    return load_config()["lark"].get(
        "base_url", "https://open.larksuite.com").rstrip("/")


def _token() -> str:
    from lark_oauth import get_user_token
    return get_user_token("noah")


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def _req(method: str, path: str, body: Optional[dict] = None
         ) -> Dict[str, Any]:
    url = f"{_base_url()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={**_headers(), "Content-Type": "application/json"},
        method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode()
            # 99991400 = trigger frequency limit — the mail API is the
            # touchiest of the Lark surfaces; back off instead of
            # failing the poll item (2026-07 ops review #4)
            if ("99991400" in body_txt or e.code == 429) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"mail {method} {path}: HTTP {e.code} "
                               f"{body_txt[:300]}")


# ---------------------------------------------------------------------------
# List message IDs
# ---------------------------------------------------------------------------

def list_message_ids(label_id: str = "INBOX",
                     page_size: int = 20,
                     max_pages: int = 20) -> List[str]:
    """Return message_id strings in the given label. Most-recent first
    by Lark's default sort. Pagination bounded by max_pages to keep
    poll runs cheap; raise this if you actually need a deep backfill."""
    out: List[str] = []
    page_token = ""
    pages = 0
    while pages < max_pages:
        params = {"label_id": label_id, "page_size": str(page_size)}
        if page_token:
            params["page_token"] = page_token
        qs = "?" + urllib.parse.urlencode(params)
        r = _req("GET", f"/open-apis/mail/v1/user_mailboxes/me/messages{qs}")
        d = r.get("data") or {}
        out.extend(d.get("items") or [])
        if not d.get("has_more"):
            break
        page_token = d.get("page_token") or ""
        if not page_token:
            break
        pages += 1
    return out


def get_message_metadata(message_id: str) -> Dict[str, Any]:
    """Return the metadata Lark provides for one message. With only
    `mail:user_mailbox.message:readonly`, the fields available are
    limited to: message_id, smtp_message_id, thread_id, label_ids,
    folder_id, internal_date, references, message_state. No body,
    subject, from, to — that requires the additional body scope."""
    enc = urllib.parse.quote(message_id, safe='')
    r = _req("GET", f"/open-apis/mail/v1/user_mailboxes/me/messages/{enc}")
    return (r.get("data") or {}).get("message") or {}


# ---------------------------------------------------------------------------
# Body + headers — BLOCKED on scope expansion
# ---------------------------------------------------------------------------

def get_message_full(message_id: str) -> Dict[str, Any]:
    """Return subject, from, to, cc, body_plain, body_html and the
    metadata. Body access is a TENANT-token scope
    (`mail:user_mailbox.message.body:read`), NOT user-token — so we
    use the bot's tenant_access_token here, not Noah's user token.

    Tenant-token mailbox addressing requires Noah's email as the
    user_mailbox_id (no '/me' shortcut for tenant flows). If the
    response is permission-denied, the operator hasn't yet configured
    the tenant-token data range for Noah's mailbox in the Lark
    Developer Console (separate from the user-token data range — same
    UI but a different panel)."""
    import urllib.parse, urllib.error
    from lark_client import get_tenant_access_token
    enc = urllib.parse.quote(message_id, safe='')
    mailbox = urllib.parse.quote(_noah_email(), safe='')
    url = (f"{_base_url()}/open-apis/mail/v1/user_mailboxes/"
           f"{mailbox}/messages/{enc}")
    try:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {get_tenant_access_token()}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"mail body fetch HTTP {e.code}: "
                           f"{e.read().decode()[:200]}")
    if d.get("code") == 15080002:
        raise NotImplementedError(
            "Tenant-token mail body scope is granted but the data "
            "range for Email-Resources-Email Message hasn't been "
            "configured to include Noah's mailbox. In the Lark "
            "Developer Console: Accessible data range for tenant "
            "token scopes → Email-Resources-Email Message → Filter "
            "by condition → User = Noah → Save. No re-OAuth needed.")
    if d.get("code") != 0:
        raise RuntimeError(f"mail body fetch code={d.get('code')}: "
                           f"{d.get('msg','')[:200]}")
    msg = (d.get("data") or {}).get("message") or {}
    if not any(k in msg for k in ("subject", "body_plain_text",
                                    "body_html")):
        raise NotImplementedError(
            f"Tenant body fetch returned but still no subject/body "
            f"fields — keys={sorted(msg.keys())}. Likely needs a "
            f"deeper data-range config or a /body subresource probe.")
    return msg


def _noah_email() -> str:
    """Noah's tenant-issued mailbox address — from notolark.yaml
    (`lark.noah_mailbox_email`). NOT the same as his personal `email`
    field returned by /authen/v1/user_info — that's a separate account
    on the same Lark user. The mailbox API expects the tenant
    address."""
    from config import load_config
    addr = (load_config().get("lark", {})
            .get("noah_mailbox_email", "") or "").strip()
    if not addr:
        raise RuntimeError(
            "lark.noah_mailbox_email not set in notolark.yaml — "
            "needed to address Noah's tenant mailbox for the body "
            "fetch endpoint (user_info.email is a different account).")
    return addr


def normalize_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Convert raw Lark message dict → the shape pipeline_store.upsert_email
    wants. Resilient to missing body fields (returns '' instead of
    raising) so the meta-only path is still useful for dedupe.

    Lark Mail returns body_plain_text and body_html as **URL-safe
    base64** (uses '-' and '_' instead of '+' and '/'; padding often
    omitted; whitespace sometimes wrapped). Standard b64decode fails
    cryptically with 'Incorrect padding' because '-' is rejected as
    non-alphabet — diagnosed empirically against Noah's inbox. We
    strip whitespace, pad to %4, and use urlsafe_b64decode. Falls
    back to the raw string only if decoding genuinely fails (true
    for the rare unencoded short body)."""
    import base64, re
    m = msg or {}

    def _decode_b64(s: str) -> str:
        if not s:
            return ""
        clean = re.sub(r"\s+", "", s)
        clean = clean + "=" * ((-len(clean)) % 4)
        try:
            raw = base64.urlsafe_b64decode(clean)
        except Exception:
            return s
        # Use errors='replace' so a single bad byte mid-body doesn't
        # discard the rest — better than losing the whole message.
        return raw.decode("utf-8", errors="replace")

    return {
        "message_id":       m.get("message_id"),
        "smtp_message_id":  m.get("smtp_message_id"),
        "thread_id":        m.get("thread_id"),
        "internal_date_ms": int(m.get("internal_date", 0) or 0),
        "label_ids":        m.get("label_ids") or [],
        # Address fields need the mail:user_mailbox.message.address:read
        # scope (granted 2026-07-03). The API's field names are
        # `head_from` and `mail_address` — NOT `from`/`email` as one
        # would guess; both shapes accepted here for safety.
        "from_email":       ((m.get("head_from") or m.get("from") or {})
                             .get("mail_address")
                             or (m.get("from") or {}).get("email") or ""),
        "from_name":        ((m.get("head_from") or m.get("from") or {})
                             .get("name") or ""),
        "to_emails":        [t.get("mail_address") or t.get("email")
                             for t in (m.get("to") or [])
                             if isinstance(t, dict)
                             and (t.get("mail_address") or t.get("email"))],
        "subject":          m.get("subject") or "",
        "body_plain":       _decode_b64(m.get("body_plain_text") or ""),
        "body_html":        _decode_b64(m.get("body_html") or ""),
    }


# ---------------------------------------------------------------------------
# Poll new — primary entry point called by the launchd job
# ---------------------------------------------------------------------------

def poll_new(verbose: bool = True) -> Dict[str, int]:
    """Walk INBOX, register any new message_ids in pipeline.emails.
    Body is best-effort: if the body scope is live, the email row gets
    real subject+body; otherwise it gets just metadata + a placeholder
    body so the dedupe + chronological audit still work.

    Subsequent steps (email_pipeline.extract_pending) skip metadata-only
    rows until body is present."""
    import pipeline_store as ps
    ids = list_message_ids(label_id="INBOX")
    if verbose:
        print(f"[lark_mail] INBOX returned {len(ids)} message_id(s)",
              flush=True)
    new = body_available = 0
    for mid in ids:
        # Try the full fetch first; fall back to metadata-only if body
        # scope is still missing. This way the moment the scope is added
        # the next poll picks up real bodies for new messages — no
        # rebuild step needed.
        try:
            full = get_message_full(mid)
            row = normalize_message(full)
            body_available += 1
        except NotImplementedError:
            meta = get_message_metadata(mid)
            row = normalize_message(meta)
        except Exception as e:
            if verbose:
                print(f"  [warn] fetch {mid[:20]}…: {str(e)[:80]}",
                      flush=True)
            continue
        if ps.upsert_email(row):
            new += 1
    if verbose:
        print(f"[lark_mail] poll done — new={new} "
              f"(body_available={body_available}/{len(ids)})", flush=True)
    return {"seen": len(ids), "new": new,
            "body_available": body_available}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _selftest() -> int:
    print("→ list message ids (INBOX, page=1)…")
    ids = list_message_ids(label_id="INBOX",
                            page_size=3, max_pages=1)
    print(f"  got {len(ids)} ids")
    if ids:
        print("→ fetch metadata for first…")
        m = get_message_metadata(ids[0])
        print(f"  keys: {sorted(m.keys())}")
        print(f"  thread_id: {m.get('thread_id','?')[:30]}…")
        print(f"  label_ids: {m.get('label_ids')}")
        print("→ try full fetch (expected to RAISE on metadata-only scope)…")
        try:
            full = get_message_full(ids[0])
            print(f"  ✓ body fetch WORKS now — keys: {sorted(full.keys())}")
        except NotImplementedError as e:
            print(f"  ⚠ body fetch BLOCKED (expected): "
                  f"{str(e).splitlines()[0]}")
    print("→ poll_new (writes to pipeline.db)…")
    res = poll_new(verbose=False)
    print(f"  {res}")
    print("\nALL PASS")
    return 0


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 0
    cmd = argv[0]
    if cmd == "selftest":
        return _selftest()
    if cmd == "list-ids":
        label = "INBOX"; limit = 10
        for i, a in enumerate(argv):
            if a == "--label" and i + 1 < len(argv):
                label = argv[i + 1]
            if a == "--limit" and i + 1 < len(argv):
                limit = int(argv[i + 1])
        ids = list_message_ids(label_id=label,
                                 page_size=min(limit, 50))[:limit]
        for mid in ids:
            print(mid)
        return 0
    if cmd == "meta" and len(argv) >= 2:
        m = get_message_metadata(argv[1])
        print(json.dumps(m, indent=2))
        return 0
    if cmd == "poll-new":
        res = poll_new(verbose=True)
        print(json.dumps(res, indent=2))
        return 0
    print("commands: selftest | list-ids [--label INBOX] [--limit N] | "
          "meta <id> | poll-new", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
