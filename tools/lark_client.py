#!/usr/bin/env python3
"""
Lark client — Noto Lark — company knowledge agent.

Mirrors the structure of tools/email_client.py:
  - credential loading (env-var first, then brain/credentials.yaml `lark:`)
  - a thin, rate-limited wrapper layer over the official `lark-oapi` SDK
  - explicit tenant_access_token verification (the `token` CLI subcommand)

Live API calls require the Lark Custom App to exist & be approved
(see docs/lark-app-setup.md — Phase 1, blocks on the operator). The module
itself is import-clean and self-testable without credentials.

Usage:
    python tools/lark_client.py token        # verify credentials
    python tools/lark_client.py selftest     # offline sanity checks
    python tools/lark_client.py whoami        # app/tenant info (needs creds)
"""

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load_config, get_path  # noqa: E402

# ---------------------------------------------------------------------------
# Credentials (env-var first for cloud portability, then credentials.yaml)
# ---------------------------------------------------------------------------

_ENV_KEYS = {
    "app_id": "LARK_APP_ID",
    "app_secret": "LARK_APP_SECRET",
    "verification_token": "LARK_VERIFICATION_TOKEN",
    "encrypt_key": "LARK_ENCRYPT_KEY",
}


def load_lark_credentials() -> Dict[str, str]:
    """Resolve Lark credentials. Env vars take precedence over the file.

    Returns dict with keys: app_id, app_secret, verification_token,
    encrypt_key (missing values are empty strings).
    """
    creds = {k: "" for k in _ENV_KEYS}

    # File fallback (brain/credentials.yaml -> `lark:` section)
    cred_path = get_path("credentials")
    if os.path.exists(cred_path):
        try:
            import yaml
            with open(cred_path) as f:
                data = yaml.safe_load(f) or {}
            lark_cfg = (data.get("lark") or {}) if isinstance(data, dict) else {}
            for k in creds:
                if lark_cfg.get(k):
                    creds[k] = str(lark_cfg[k])
        except Exception as e:  # pragma: no cover - defensive
            print(f"[lark_client] warning: could not read {cred_path}: {e}",
                  file=sys.stderr)

    # Env override (highest precedence)
    for k, env in _ENV_KEYS.items():
        if os.environ.get(env):
            creds[k] = os.environ[env]

    return creds


def assert_no_lark_delete() -> None:
    """HARD SAFETY INVARIANT: Noto must never delete entire Lark
    objects — documents, files, folders, messages, wiki pages, or
    Bitable records. Scans every Lark tool source for a removal-
    capable API call against those object types and raises if one is
    found, so the bot refuses to start.

    Block-level operations WITHIN a doc (document_block /
    document_block_children) ARE permitted — they preserve the doc's
    native Lark edit history, which is the operator-facing audit
    trail for the singleton-deliverable flow (target lists / workups /
    firm-fits). The allowlist below names the only delete-shape
    patterns the scan is willing to ignore; everything else still
    aborts startup.

    Patterns/comments here are built from fragments and trailing
    comments are stripped before scanning, so this never matches itself.
    """
    import re
    import glob as _glob
    here = os.path.dirname(os.path.abspath(__file__))
    d = "d" + "elete"
    D = "D" + "ELETE"
    Dr = "D" + "elete"

    # Banned patterns — any of these in non-allowlisted code aborts boot.
    pats = [
        re.compile(r"\." + d + r"\s*\("),
        re.compile(Dr + r"\w*Request"),
        re.compile(r"method\s*=\s*['\"]" + D),
        re.compile(r"/open-apis/[^'\"]*" + d),
    ]

    # ALLOWLIST: only block-level edits inside a doc. Anything matching
    # these is OK even though it tripped a banned pattern above.
    # Specifically:
    #   - document_block / document_block_children SDK surfaces
    #   - BatchDeleteDocumentBlockChildrenRequest and friends
    #   - The /docx/v1/documents/.../blocks/.../children/batch_delete
    #     REST endpoint
    # NOT allowed: documents.delete, files.delete, folders.delete,
    # messages.delete, space_node.delete, app_table_record.delete, etc.
    allow = [
        re.compile(r"document_block(?:_children)?\." + d + r"\s*\("),
        re.compile(r"BatchD" + r"eleteDocumentBlock\w*"),
        re.compile(r"/open-apis/docx/v\d+/documents/[^'\"]+/"
                   r"blocks/[^'\"]+/children/batch_" + d),
    ]

    hits = []
    # The mail stack is scanned too. ONE approved exception exists:
    # autodraft_card._delete_draft may DELETE a mail DRAFT — the bot's
    # own pre-send output, dismissed by its owner from the review card.
    # That single line carries the safety-scan-ok marker; any OTHER
    # delete-shape in these files still aborts boot.
    for path in _glob.glob(os.path.join(here, "lark_*.py")) + \
            [os.path.join(here, f) for f in
             ("noto_research.py", "bitable_store.py",
              "email_autodraft.py", "autodraft_card.py",
              "mail_store.py", "mail_retrieval.py", "email_playbook.py")]:
        if not os.path.exists(path):
            continue
        for i, line in enumerate(open(path), 1):
            code = line.split("#", 1)[0]            # drop trailing comment
            if not code.strip() or "safety-scan-ok" in line:
                continue
            tripped = None
            for p in pats:
                if p.search(code):
                    tripped = p
                    break
            if not tripped:
                continue
            # Banned pattern matched — check the allowlist.
            if any(a.search(code) for a in allow):
                continue
            hits.append(f"{os.path.basename(path)}:{i}: "
                        f"{code.strip()[:80]}")
    if hits:
        raise RuntimeError(
            "SAFETY ABORT - top-level Lark removal-capable call found "
            "in Noto's code (Noto may only delete block-level content "
            "inside a doc — never an entire document/file/folder/"
            "message/wiki/Bitable record):\n  "
            + "\n  ".join(hits))


def lark_config() -> Dict[str, Any]:
    """Return the `lark:` block from notolark.yaml (non-secret settings)."""
    return load_config().get("lark", {}) or {}


import re as _re_mod


def _md_inline_segments(text):
    """Split text into (text, bold, italic, url) segments for inline
    markdown: [label](url), **bold**, *italic*. Shared by the block
    builder and the text-block chunker so no writer ever leaves raw
    tokens in a doc."""
    out = []
    link_re = _re_mod.compile(r"\[([^\]]+?)\]\((https?://[^)\s]+)\)")
    pos = 0
    for m in link_re.finditer(text):
        if m.start() > pos:
            out.extend(_md_bold_ital(text[pos:m.start()]))
        out.append((m.group(1), False, False, m.group(2)))
        pos = m.end()
    if pos < len(text):
        out.extend(_md_bold_ital(text[pos:]))
    return [s for s in out if s[0]]


def _md_bold_ital(text):
    out = []
    bold_re = _re_mod.compile(r"\*\*([^*]+?)\*\*")
    pos = 0
    for m in bold_re.finditer(text):
        if m.start() > pos:
            out.extend(_md_ital(text[pos:m.start()]))
        out.append((m.group(1), True, False, None))
        pos = m.end()
    if pos < len(text):
        out.extend(_md_ital(text[pos:]))
    return out


def _md_ital(text):
    out = []
    ital_re = _re_mod.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])|"
                              r"(?<![\w_])_([^_\n]+?)_(?![\w_])")
    pos = 0
    for m in ital_re.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False, False, None))
        out.append((m.group(1) or m.group(2), False, True, None))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False, False, None))
    return out


def _base_url() -> str:
    return lark_config().get("base_url", "https://open.larksuite.com").rstrip("/")

def _tenant_url() -> str:
    from config import load_config
    return (load_config().get("lark", {})
            .get("tenant_url", "")).rstrip("/")


# ---------------------------------------------------------------------------
# Rate limiting (token bucket; conservative defaults per Lark limits)
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple thread-safe token bucket. rate = tokens/sec, burst = capacity."""

    def __init__(self, rate: float, burst: Optional[float] = None):
        self.rate = float(rate)
        self.capacity = float(burst if burst is not None else rate)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, n: float = 1.0) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._last) * self.rate
                )
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                sleep_for = (n - self._tokens) / self.rate
                time.sleep(max(sleep_for, 0.005))


# Lark documented ceilings: ~5 QPS messages; docs/bitable more conservative.
# `card` covers CardKit create/update/stream calls for streaming replies.
_LIMITS = {
    "message": RateLimiter(rate=5, burst=5),
    "card": RateLimiter(rate=5, burst=10),
    "doc": RateLimiter(rate=2, burst=4),
    "bitable": RateLimiter(rate=2, burst=4),
    "default": RateLimiter(rate=3, burst=5),
}

# Lark docx block types used by the doc writer (kind -> (block_type, field)).
_BLOCK_KINDS = {
    "text": (2, "text"),
    "heading1": (3, "heading1"), "heading2": (4, "heading2"),
    "heading3": (5, "heading3"), "heading4": (6, "heading4"),
    "heading5": (7, "heading5"), "heading6": (8, "heading6"),
    "bullet": (12, "bullet"), "ordered": (13, "ordered"),
    "code": (14, "code"), "quote": (15, "quote"),
}


# ---------------------------------------------------------------------------
# tenant_access_token (explicit fetch + in-memory cache w/ refresh @80% TTL)
# ---------------------------------------------------------------------------

class _TokenCache:
    def __init__(self):
        self.token: Optional[str] = None
        self.expires_at: float = 0.0
        self._lock = threading.Lock()

    def get(self, app_id: str, app_secret: str) -> str:
        with self._lock:
            now = time.time()
            if self.token and now < self.expires_at:
                return self.token
            tok, ttl = _fetch_tenant_access_token(app_id, app_secret)
            self.token = tok
            # refresh at 80% of TTL to avoid edge expiry
            self.expires_at = now + ttl * 0.8
            return tok


_token_cache = _TokenCache()


def _fetch_tenant_access_token(app_id: str, app_secret: str) -> tuple[str, int]:
    """POST /open-apis/auth/v3/tenant_access_token/internal (stdlib only)."""
    if not app_id or not app_secret:
        raise RuntimeError(
            "Missing Lark credentials (app_id/app_secret). Set env "
            "LARK_APP_ID/LARK_APP_SECRET or fill brain/credentials.yaml. "
            "See docs/lark-app-setup.md."
        )
    url = f"{_base_url()}/open-apis/auth/v3/tenant_access_token/internal"
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        raise RuntimeError(f"Lark token request failed: {e}") from e
    if payload.get("code") != 0:
        raise RuntimeError(
            f"Lark token error code={payload.get('code')} "
            f"msg={payload.get('msg')}"
        )
    return payload["tenant_access_token"], int(payload.get("expire", 7200))


def get_tenant_access_token() -> str:
    creds = load_lark_credentials()
    return _token_cache.get(creds["app_id"], creds["app_secret"])


def get_bot_open_id() -> str:
    """The bot's own open_id — used to tell whether a group message
    actually @mentions the bot (vs. human-to-human chatter)."""
    url = f"{_base_url()}/open-apis/bot/v3/info"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {get_tenant_access_token()}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
        return ((payload.get("bot") or {}).get("open_id", "")) or ""
    except Exception as e:
        print(f"[lark_client] get_bot_open_id failed: {e}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------------------
# SDK client + typed wrappers
# ---------------------------------------------------------------------------

class LarkClient:
    """Thin wrapper over lark-oapi. Construct once; reuse.

    All read wrappers paginate and are rate-limited. Methods raise
    RuntimeError on API error so callers fail loud.
    """

    def __init__(self, user_token: Optional[str] = None):
        creds = load_lark_credentials()
        if not creds["app_id"] or not creds["app_secret"]:
            raise RuntimeError(
                "LarkClient needs app_id/app_secret (env or "
                "brain/credentials.yaml). See docs/lark-app-setup.md."
            )
        import lark_oapi as lark
        self._lark = lark
        self._user_token = user_token
        self._client = (
            lark.Client.builder()
            .app_id(creds["app_id"])
            .app_secret(creds["app_secret"])
            .build()
        )

    def _opt(self):
        """RequestOption carrying the Noto user token (for wiki/docx
        enumeration, which the app identity cannot do). None -> app token."""
        if not self._user_token:
            return None
        return (self._lark.RequestOption.builder()
                .user_access_token(self._user_token).build())

    # -- helpers ----------------------------------------------------------
    def _check_retry(self, fn, what: str, tries: int = 3):
        """Execute a zero-arg request callable with backoff-retry on
        Lark rate-limit codes. Three processes (bot, nightly resync,
        15-min email poll) share the tenant's QPS ceiling with only
        per-process limiters, so bursts (e.g. the F8 doc-write sweep)
        can get frequency-limited — previously those calls raised
        immediately and the write was dropped, looking like a data bug
        (2026-07 ops review #4). Used on bursty write paths; read
        paths keep the plain _check."""
        import time as _t
        last = None
        for i in range(tries):
            resp = fn()
            if resp.success():
                return getattr(resp, "data", None)
            last = resp
            if str(resp.code) in ("99991400", "99991663", "429") \
                    and i < tries - 1:
                _t.sleep(2 ** i * 3)      # 3s, 6s
                continue
            break
        raise RuntimeError(
            f"Lark API {what} failed: code={last.code} msg={last.msg} "
            f"log_id={getattr(last, 'get_log_id', lambda: '?')()}")

    @staticmethod
    def _check(resp, what: str):
        if not resp.success():
            raise RuntimeError(
                f"Lark API {what} failed: code={resp.code} msg={resp.msg} "
                f"log_id={getattr(resp, 'get_log_id', lambda: '?')()}"
            )
        # Empty-body endpoints (e.g. CardKit update/content) don't expose
        # `.data` — use getattr so callers that ignore the result still work.
        return getattr(resp, "data", None)

    # -- messaging --------------------------------------------------------
    def send_text(self, receive_id: str, text: str,
                   receive_id_type: str = "chat_id") -> str:
        _LIMITS["message"].acquire()
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody,
        )
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        data = self._check(self._client.im.v1.message.create(req), "send_text")
        return data.message_id

    def list_chat_members(self, chat_id: str) -> List[Dict[str, str]]:
        """List a chat's members — [{open_id, name}]. READ-ONLY (GET),
        tenant token (im:chat scope). Primary use: resolving sender
        names the corpus ingest couldn't (only operators.yaml people
        resolve at ingest time)."""
        from lark_oapi.api.im.v1 import GetChatMembersRequest
        out: List[Dict[str, str]] = []
        page_token = None
        while True:
            _LIMITS["message"].acquire()
            b = (GetChatMembersRequest.builder()
                 .chat_id(chat_id)
                 .member_id_type("open_id")
                 .page_size(100))
            if page_token:
                b = b.page_token(page_token)
            data = self._check(
                self._client.im.v1.chat_members.get(b.build()),
                "list_chat_members")
            for it in (getattr(data, "items", None) or []):
                out.append({"open_id": getattr(it, "member_id", "") or "",
                            "name": getattr(it, "name", "") or ""})
            if not getattr(data, "has_more", False):
                break
            page_token = getattr(data, "page_token", None)
        return out

    def list_messages(self, container_id: str,
                       start_time: Optional[str] = None,
                       container_id_type: str = "chat") -> List[Dict[str, Any]]:
        """List messages in a chat (forward-only; see history limitation)."""
        from lark_oapi.api.im.v1 import ListMessageRequest
        out: List[Dict[str, Any]] = []
        page_token = None
        while True:
            _LIMITS["message"].acquire()
            b = (ListMessageRequest.builder()
                 .container_id_type(container_id_type)
                 .container_id(container_id)
                 .page_size(50))
            if start_time:
                b = b.start_time(start_time)
            if page_token:
                b = b.page_token(page_token)
            data = self._check(self._client.im.v1.message.list(b.build()),
                               "list_messages")
            for it in (data.items or []):
                out.append(json.loads(self._lark.JSON.marshal(it)))
            if not getattr(data, "has_more", False):
                break
            page_token = data.page_token
        return out

    # -- streaming cards (CardKit 2.0 — create + update only) ------------
    def create_card(self, card: Dict[str, Any]) -> str:
        """Create a CardKit card entity from a schema-2.0 dict. Returns its
        card_id. CREATE only — a new entity, nothing is altered/removed."""
        from lark_oapi.api.cardkit.v1 import (
            CreateCardRequest, CreateCardRequestBody)
        _LIMITS["card"].acquire()
        req = (CreateCardRequest.builder()
               .request_body(CreateCardRequestBody.builder()
                             .type("card_json")
                             .data(json.dumps(card)).build())
               .build())
        data = self._check(self._client.cardkit.v1.card.create(req),
                            "create_card")
        return data.card_id

    def send_card(self, receive_id: str, card_id: str,
                  receive_id_type: str = "chat_id") -> str:
        """Send an interactive message referencing a CardKit card entity.
        CREATE only — sends a new message."""
        _LIMITS["message"].acquire()
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody)
        content = json.dumps({"type": "card", "data": {"card_id": card_id}})
        req = (CreateMessageRequest.builder()
               .receive_id_type(receive_id_type)
               .request_body(CreateMessageRequestBody.builder()
                             .receive_id(receive_id)
                             .msg_type("interactive")
                             .content(content).build())
               .build())
        data = self._check(self._client.im.v1.message.create(req), "send_card")
        return data.message_id

    def update_card(self, card_id: str, card: Dict[str, Any],
                    sequence: int) -> None:
        """Replace a CardKit card's content. UPDATE only — overwrites the
        card body in place; `sequence` (monotonic) lets the server drop
        out-of-order pushes. Never removes the card or any message."""
        from lark_oapi.api.cardkit.v1 import (
            UpdateCardRequest, UpdateCardRequestBody, Card)
        _LIMITS["card"].acquire()
        req = (UpdateCardRequest.builder()
               .card_id(card_id)
               .request_body(UpdateCardRequestBody.builder()
                             .card(Card.builder().type("card_json")
                                   .data(json.dumps(card)).build())
                             .sequence(sequence).build())
               .build())
        self._check(self._client.cardkit.v1.card.update(req), "update_card")

    def stream_card_element(self, card_id: str, element_id: str,
                            text: str, sequence: int) -> None:
        """Stream text into one card element (the typewriter endpoint).
        UPDATE only — sets the element's content; `sequence` orders pushes."""
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest, ContentCardElementRequestBody)
        _LIMITS["card"].acquire()
        req = (ContentCardElementRequest.builder()
               .card_id(card_id).element_id(element_id)
               .request_body(ContentCardElementRequestBody.builder()
                             .content(text).sequence(sequence).build())
               .build())
        self._check(self._client.cardkit.v1.card_element.content(req),
                    "stream_card_element")

    # -- wiki / docs ------------------------------------------------------
    def list_wiki_spaces(self) -> List[Dict[str, Any]]:
        from lark_oapi.api.wiki.v2 import ListSpaceRequest
        out, page_token = [], None
        while True:
            _LIMITS["doc"].acquire()
            b = ListSpaceRequest.builder().page_size(50)
            if page_token:
                b = b.page_token(page_token)
            data = self._check(
                self._client.wiki.v2.space.list(b.build(), self._opt()),
                "list_wiki_spaces")
            for it in (data.items or []):
                out.append(json.loads(self._lark.JSON.marshal(it)))
            if not getattr(data, "has_more", False):
                break
            page_token = data.page_token
        return out

    def list_wiki_nodes(self, space_id: str,
                        parent_node_token: Optional[str] = None
                        ) -> List[Dict[str, Any]]:
        from lark_oapi.api.wiki.v2 import ListSpaceNodeRequest
        out, page_token = [], None
        while True:
            _LIMITS["doc"].acquire()
            b = ListSpaceNodeRequest.builder().space_id(space_id).page_size(50)
            if parent_node_token:
                b = b.parent_node_token(parent_node_token)
            if page_token:
                b = b.page_token(page_token)
            data = self._check(
                self._client.wiki.v2.space_node.list(b.build(), self._opt()),
                "list_wiki_nodes")
            for it in (data.items or []):
                out.append(json.loads(self._lark.JSON.marshal(it)))
            if not getattr(data, "has_more", False):
                break
            page_token = data.page_token
        return out

    def get_wiki_node(self, token: str) -> Dict[str, Any]:
        """Resolve a wiki node token -> node info (obj_token, obj_type,
        space_id, title). Works for any node the (user) token can see."""
        from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest
        req = GetNodeSpaceRequest.builder().token(token).obj_type("wiki") \
            .build()
        data = self._check(
            self._client.wiki.v2.space.get_node(req, self._opt()),
            "get_wiki_node")
        return json.loads(self._lark.JSON.marshal(data.node))

    def get_doc_meta(self, doc_token: str,
                     doc_type: str = "docx") -> Dict[str, Any]:
        """Doc metadata: owner_id, latest_modify_user, create/update
        times, title (via drive.v1.meta.batch_query — the docx.get
        endpoint doesn't expose owner). Used by indexers to
        attribute authorship for precedential weighting. Read-only."""
        rows = self.get_docs_meta_batch([(doc_token, doc_type)])
        return rows[0] if rows else {}

    def get_docs_meta_batch(
            self, docs: List[tuple]) -> List[Dict[str, Any]]:
        """Batched metadata for many docs in one call. `docs` is a list
        of (doc_token, doc_type) — type is usually 'docx'. Lark limits
        ~200 per call; we chunk."""
        from lark_oapi.api.drive.v1 import (
            BatchQueryMetaRequest, MetaRequest, RequestDoc)
        out: List[Dict[str, Any]] = []
        for i in range(0, len(docs), 200):
            chunk = docs[i:i + 200]
            req_docs = [RequestDoc.builder()
                        .doc_token(t).doc_type(dt).build()
                        for (t, dt) in chunk]
            req = (BatchQueryMetaRequest.builder()
                   .request_body(MetaRequest.builder()
                                 .request_docs(req_docs).build())
                   .build())
            _LIMITS["doc"].acquire()
            data = self._check(
                self._client.drive.v1.meta.batch_query(req, self._opt()),
                "get_docs_meta_batch")
            for m in (data.metas or []):
                out.append(json.loads(self._lark.JSON.marshal(m)))
        return out

    def get_docx_blocks(self, document_id: str) -> List[Dict[str, Any]]:
        from lark_oapi.api.docx.v1 import ListDocumentBlockRequest
        out, page_token = [], None
        while True:
            _LIMITS["doc"].acquire()
            b = (ListDocumentBlockRequest.builder()
                 .document_id(document_id).page_size(500))
            if page_token:
                b = b.page_token(page_token)
            data = self._check(
                self._client.docx.v1.document_block.list(
                    b.build(), self._opt()),
                "get_docx_blocks")
            for it in (data.items or []):
                out.append(json.loads(self._lark.JSON.marshal(it)))
            if not getattr(data, "has_more", False):
                break
            page_token = data.page_token
        return out

    # -- block-level delete (operator-confirmed only — caller wires
    # the confirmation flow). PERMITTED by assert_no_lark_delete's
    # allowlist because it operates on document_block_children only,
    # never on documents/files/folders. Lark records the change in
    # the doc's native edit history; THAT is the audit trail.
    def delete_doc_blocks(self, document_id: str,
                            parent_block_id: str,
                            start_index: int,
                            end_index: int) -> int:
        """Delete a contiguous range of children from a parent block.
        end_index is exclusive (start=2, end=5 deletes positions 2,3,4).

        Lark's API only deletes contiguous ranges in a single call.
        For multiple non-contiguous targets, the caller should issue
        multiple calls — IMPORTANT: from HIGHEST index DOWN so the
        earlier deletes don't shift the later positions.

        Returns the number of children deleted (end_index - start_index).
        Wired via the BatchDeleteDocumentBlockChildrenRequest SDK
        surface — explicitly allowlisted in assert_no_lark_delete."""
        from lark_oapi.api.docx.v1 import (
            BatchDeleteDocumentBlockChildrenRequest,
            BatchDeleteDocumentBlockChildrenRequestBody)
        if end_index <= start_index:
            return 0
        _LIMITS["doc"].acquire()
        body = (BatchDeleteDocumentBlockChildrenRequestBody.builder()
                .start_index(start_index)
                .end_index(end_index)
                .build())
        req = (BatchDeleteDocumentBlockChildrenRequest.builder()
               .document_id(document_id)
               .block_id(parent_block_id)
               .request_body(body)
               .build())
        self._check(
            self._client.docx.v1.document_block_children
            .batch_delete(req, self._opt()),
            "delete_doc_blocks")
        return end_index - start_index

    # -- doc section parser (groups blocks into firm-style sections) ----
    def parse_doc_sections(self, document_id: str
                            ) -> List[Dict[str, Any]]:
        """Group a doc's root-children into heading-bounded sections.

        For target lists / workups, the convention is each firm = an H1
        heading + a tail of paragraphs / bullets / sub-headings until
        the next H1. This walker returns:

            [
              {"heading_text": "Herbert Smith Freehills",
               "heading_block_id": "doxjp...",
               "heading_position": 5,    # index in root-children
               "block_ids": ["doxjp...", "doxjp...", ...],
               "first_index": 5,
               "last_index": 17,         # inclusive
               "full_text": "Herbert Smith Freehills\\nPartners – 6 ...
                             \\n- Daniel Chia ..."},
              ...
            ]

        Blocks BEFORE the first H1 (title, intro, callout, operator
        header) are returned as a synthetic section with
        heading_text='__preamble__' so the caller can skip them
        cleanly. Sections matching the operator-provenance header are
        also flagged via heading_text='__operator_header__' so the
        edit-plan LLM knows not to touch them."""
        blocks = self.get_docx_blocks(document_id)
        # Build {block_id -> position-in-root-children, ...}
        root_children = []
        for b in blocks:
            if b.get("parent_id") == document_id:
                root_children.append(b)
        # Walk root children in order, grouping by H1 heading boundaries.
        sections: List[Dict[str, Any]] = []
        cur: Dict[str, Any] = {
            "heading_text": "__preamble__",
            "heading_block_id": "",
            "heading_position": -1,
            "block_ids": [],
            "first_index": 0,
            "last_index": -1,
            "full_text": "",
        }
        def _text_of(b: Dict[str, Any]) -> str:
            s = ""
            for fk in ("text", "heading1", "heading2", "heading3",
                       "heading4", "bullet", "ordered", "code", "quote",
                       "callout"):
                v = b.get(fk, {}) or {}
                for el in (v.get("elements", []) or []):
                    tr = el.get("text_run", {}) or {}
                    s += tr.get("content", "") or ""
            return s

        for pos, b in enumerate(root_children):
            bt = b.get("block_type")
            t = _text_of(b)
            if bt == 3:    # H1
                if cur["block_ids"] or cur["heading_block_id"]:
                    sections.append(cur)
                cur = {
                    "heading_text": t.strip(),
                    "heading_block_id": b.get("block_id", ""),
                    "heading_position": pos,
                    "block_ids": [b.get("block_id", "")],
                    "first_index": pos,
                    "last_index": pos,
                    "full_text": t,
                }
            else:
                cur["block_ids"].append(b.get("block_id", ""))
                cur["last_index"] = pos
                cur["full_text"] += "\n" + t if cur["full_text"] else t
        if cur["block_ids"] or cur["heading_block_id"]:
            sections.append(cur)
        return sections

    # -- doc creation (create-only — never edits/removes existing docs) ---
    def create_document(self, title: str,
                        folder_token: Optional[str] = None) -> Dict[str, Any]:
        """Create a new Lark doc (optionally inside a folder). Returns the
        document dict (has document_id). CREATE only."""
        from lark_oapi.api.docx.v1 import (
            CreateDocumentRequest, CreateDocumentRequestBody)
        body = CreateDocumentRequestBody.builder().title((title or "")[:800])
        if folder_token:
            body = body.folder_token(folder_token)
        req = (CreateDocumentRequest.builder()
               .request_body(body.build()).build())
        _LIMITS["doc"].acquire()
        data = self._check(
            self._client.docx.v1.document.create(req, self._opt()),
            "create_document")
        return json.loads(self._lark.JSON.marshal(data.document))

    def insert_doc_blocks_at(self, document_id: str,
                              block_dicts: List[Dict[str, Any]],
                              index: int) -> int:
        """Insert content blocks at a SPECIFIC index (0 = before
        everything, 1 = after title, N = anywhere). Same block-render
        path as add_doc_blocks; differs only in the insertion position.
        Used for the operator-provenance header at the top of the doc.
        CREATE only — never edits/removes existing blocks."""
        # Reuse add_doc_blocks's batch logic by monkey-passing the
        # starting index. add_doc_blocks always starts at 0 because
        # the lambda i in its loop uses the batch offset; here we
        # need a configurable insert point.
        return self._add_doc_blocks_at(document_id, block_dicts, index)

    def insert_child_blocks(self, document_id: str,
                            parent_block_id: str,
                            block_dicts: List[Dict[str, Any]],
                            index: int = 0) -> int:
        """Insert blocks as CHILDREN of an arbitrary block (nested
        nested sub-bullets under an existing entry, etc.).
        CREATE only — same block-render path as add_doc_blocks."""
        return self._add_doc_blocks_at(document_id, block_dicts, index,
                                       parent_block_id=parent_block_id)

    def _add_doc_blocks_at(self, document_id: str,
                            block_dicts: List[Dict[str, Any]],
                            start_index: int,
                            parent_block_id: Optional[str] = None) -> int:
        """Shared block-append/insert builder. start_index = 0 → bottom-
        append (default of add_doc_blocks). start_index = N → insert at
        position N. Carries inline [text](url) → Link styling."""
        import re as _re
        import urllib.parse as _urlp
        from lark_oapi.api.docx.v1 import (
            CreateDocumentBlockChildrenRequest,
            CreateDocumentBlockChildrenRequestBody,
            Block, Text, TextElement, TextRun, TextElementStyle, Link)

        _LINK_RE = _re.compile(r"\[([^\]]+?)\]\((https?://[^)\s]+)\)")

        def _segments(content: str):
            pos = 0
            for m in _LINK_RE.finditer(content):
                if m.start() > pos:
                    yield (content[pos:m.start()], None)
                yield (m.group(1), m.group(2))
                pos = m.end()
            if pos < len(content):
                yield (content[pos:], None)

        def _run_for(text: str, link_url: Optional[str],
                     color: Optional[int] = None,
                     bold: bool = False, italic: bool = False):
            rb = TextRun.builder().content(text[:2000])
            if link_url or color or bold or italic:
                sb = TextElementStyle.builder()
                if link_url:
                    sb = sb.link(Link.builder().url(_urlp.quote(
                        link_url, safe=":/?#[]@!$&'()*+,;=%")).build())
                if color:
                    sb = sb.text_color(int(color))
                if bold:
                    sb = sb.bold(True)
                if italic:
                    sb = sb.italic(True)
                rb = rb.text_element_style(sb.build())
            return TextElement.builder().text_run(rb.build()).build()

        def _mk(bd: Dict[str, Any]):
            bt, field = _BLOCK_KINDS.get(bd.get("kind"), (2, "text"))
            content = (bd.get("text") or "").strip() or " "
            color = bd.get("color")    # Lark text_color enum (1 = red)
            # inline markdown -> styled runs (links + **bold** +
            # *italic*), so LLM output never lands as literal tokens
            elements = [
                _run_for(seg, url, color, bold=b, italic=i)
                for seg, b, i, url in _md_inline_segments(content)
                if seg
            ]
            if not elements:
                elements = [_run_for(" ", None)]
            txt = Text.builder().elements(elements).build()
            bb = Block.builder().block_type(bt)
            bb = getattr(bb, field)(txt)
            return bb.build()

        blocks = [_mk(b) for b in block_dicts]
        added = 0
        for i in range(0, len(blocks), 45):
            batch = blocks[i:i + 45]
            insert_at = start_index + i
            _LIMITS["doc"].acquire()
            req = (CreateDocumentBlockChildrenRequest.builder()
                   .document_id(document_id)
                   .block_id(parent_block_id or document_id)
                   .request_body(
                       CreateDocumentBlockChildrenRequestBody.builder()
                       .children(batch).index(insert_at).build())
                   .build())
            self._check(
                self._client.docx.v1.document_block_children.create(
                    req, self._opt()),
                "insert_doc_blocks_at")
            added += len(batch)
        return added

    def _root_child_count(self, document_id: str) -> int:
        """Count blocks that sit directly under the document root.
        Needed for true bottom-append: Lark's `index` field is a
        POSITION (not "end"), so to append we have to pass the current
        child count. Without this, index=0 (the previous default)
        inserts new blocks at the TOP of the doc, pushing the
        original title and existing content down."""
        try:
            children = [b for b in self.get_docx_blocks(document_id)
                        if b.get("parent_id") == document_id]
            return len(children)
        except Exception as e:
            print(f"[lark_client] _root_child_count failed: {e}",
                  file=sys.stderr, flush=True)
            return 0


    def add_doc_blocks(self, document_id: str,
                       block_dicts: List[Dict[str, Any]]) -> int:
        """APPEND content blocks to the bottom of a doc.
        block_dicts: [{kind,text}]. CREATE only — appends children,
        never edits/removes.

        Inline `[label](url)` markdown syntax in the text becomes a real
        Lark hyperlink: the splitter below emits one TextElement per
        plain run plus one TextElement per link run that carries Lark's
        `Link` style on `text_element_style`. So partner names + bio
        URLs make clickable links in the rendered doc."""
        import re as _re
        import urllib.parse as _urlp
        from lark_oapi.api.docx.v1 import (
            CreateDocumentBlockChildrenRequest,
            CreateDocumentBlockChildrenRequestBody,
            Block, Text, TextElement, TextRun, TextElementStyle, Link)

        _LINK_RE = _re.compile(r"\[([^\]]+?)\]\((https?://[^)\s]+)\)")

        def _segments(content: str):
            """Yield (text, link_url_or_None) tuples covering the whole
            string in order. Empty plain segments are skipped."""
            pos = 0
            for m in _LINK_RE.finditer(content):
                if m.start() > pos:
                    yield (content[pos:m.start()], None)
                yield (m.group(1), m.group(2))
                pos = m.end()
            if pos < len(content):
                yield (content[pos:], None)

        def _run_for(text: str, link_url: Optional[str],
                     bold: bool = False, italic: bool = False):
            rb = TextRun.builder().content(text[:2000])
            if link_url or bold or italic:
                sb = TextElementStyle.builder()
                if link_url:
                    # Lark wants the URL percent-encoded; quote leaves
                    # the url-structure chars intact.
                    sb = sb.link(Link.builder().url(_urlp.quote(
                        link_url, safe=":/?#[]@!$&'()*+,;=%")).build())
                if bold:
                    sb = sb.bold(True)
                if italic:
                    sb = sb.italic(True)
                rb = rb.text_element_style(sb.build())
            return TextElement.builder().text_run(rb.build()).build()

        def _mk(bd: Dict[str, Any]):
            bt, field = _BLOCK_KINDS.get(bd.get("kind"), (2, "text"))
            content = (bd.get("text") or "").strip() or " "
            # inline markdown -> styled runs, so **bold** from the LLM
            # never lands as literal asterisks (2026-07-06)
            elements = [
                _run_for(seg, url, bold=b, italic=i)
                for seg, b, i, url in _md_inline_segments(content)
                if seg
            ]
            if not elements:
                elements = [_run_for(" ", None)]
            txt = Text.builder().elements(elements).build()
            bb = Block.builder().block_type(bt)
            bb = getattr(bb, field)(txt)
            return bb.build()

        blocks = [_mk(b) for b in block_dicts]
        # True bottom-append: Lark's `index` field is a position, not a
        # sentinel. To put new blocks AT THE END we have to pass the
        # current root-child count as the starting index. Without
        # this, index=0 (the previous default) inserted at the top of
        # the doc — original title got pushed down and every "addition"
        # actually went to the top, not the bottom as the bot's reply
        # claimed. New behaviour: query the count once, then offset
        # each batch by that count.
        start_index = self._root_child_count(document_id)
        added = 0
        for i in range(0, len(blocks), 45):
            batch = blocks[i:i + 45]
            _LIMITS["doc"].acquire()
            req = (CreateDocumentBlockChildrenRequest.builder()
                   .document_id(document_id)
                   .block_id(document_id)
                   .request_body(
                       CreateDocumentBlockChildrenRequestBody.builder()
                       .children(batch).index(start_index + i).build())
                   .build())
            self._check_retry(
                lambda: self._client.docx.v1.document_block_children
                .create(req, self._opt()),
                "add_doc_blocks")
            added += len(batch)
        return added

    # -- text-doc create + UPDATE ------------------------------------------
    # Submission drafts live as a single TEXT BLOCK so iterative edits
    # are simple, atomic, and don't need block-level surgery. Multiple
    # text_runs inside that one block let us hold a long document
    # (Lark caps each run at 2000 chars). UPDATE-only — never deletes
    # blocks; the assert_no_lark_delete scan still passes.
    @staticmethod
    def _chunk_runs(content: str, chunk: int = 1800) -> List[Any]:
        """Markdown-aware text runs for a single text block.

        Was a raw chunker — every `**`, `###` and `-` the LLM emitted
        landed LITERALLY in the doc (operator report 2026-07-06:
        update_text_doc is used by all v2+ edits, so edited docs showed
        raw markdown). A text block can't hold real heading/bullet
        blocks, so we emulate: heading lines render as bold, list
        markers become bullets/glyphs, and inline **bold** / *italic* /
        [label](url) become styled runs."""
        from lark_oapi.api.docx.v1 import (TextElement, TextRun,
                                           TextElementStyle, Link)
        import urllib.parse as _urlp

        def _mk(text_piece: str, bold=False, italic=False, url=None):
            out = []
            for i in range(0, max(len(text_piece), 1), chunk):
                piece = text_piece[i:i + chunk] or " "
                rb = TextRun.builder().content(piece)
                if bold or italic or url:
                    sb = TextElementStyle.builder()
                    if bold:
                        sb = sb.bold(True)
                    if italic:
                        sb = sb.italic(True)
                    if url:
                        sb = sb.link(Link.builder()
                                     .url(_urlp.quote(url, safe=":/?#&=%.,+-_~"))
                                     .build())
                    rb = rb.text_element_style(sb.build())
                out.append(TextElement.builder()
                           .text_run(rb.build()).build())
            return out

        runs: List[Any] = []
        lines = (content or " ").split("\n")
        for li, raw_line in enumerate(lines):
            line, line_bold = raw_line, False
            m = _re_mod.match(r"^(#{1,6})\s+(.*)$", line)
            if m:
                line, line_bold = m.group(2), True
            elif _re_mod.match(r"^\s*[-*]\s+", line):
                line = _re_mod.sub(r"^(\s*)[-*]\s+", r"\1• ", line)
            if li > 0:
                line = "\n" + line
            for seg_text, seg_bold, seg_ital, seg_url in \
                    _md_inline_segments(line):
                runs.extend(_mk(seg_text, bold=seg_bold or line_bold,
                                italic=seg_ital, url=seg_url))
        return runs or _mk(" ")

    def create_text_doc(self, title: str, content: str,
                        folder_token: Optional[str] = None
                        ) -> Dict[str, Any]:
        """Create a Lark doc whose entire body is ONE text block holding
        `content` (chunked into multiple text_runs). Returns
        {document_id, url, block_id} — block_id is the body block we'll
        update on each edit. CREATE only."""
        from lark_oapi.api.docx.v1 import (
            CreateDocumentBlockChildrenRequest,
            CreateDocumentBlockChildrenRequestBody,
            Block, Text)
        doc = self.create_document(title, folder_token)
        doc_id = doc.get("document_id")
        # build the single body block
        body_block = (Block.builder().block_type(2)
                      .text(Text.builder()
                            .elements(self._chunk_runs(content)).build())
                      .build())
        _LIMITS["doc"].acquire()
        req = (CreateDocumentBlockChildrenRequest.builder()
               .document_id(doc_id).block_id(doc_id)
               .request_body(
                   CreateDocumentBlockChildrenRequestBody.builder()
                   .children([body_block]).index(0).build())
               .build())
        data = self._check(
            self._client.docx.v1.document_block_children.create(
                req, self._opt()),
            "create_text_doc")
        created = json.loads(self._lark.JSON.marshal(data))
        # The response holds the newly-created children — grab the block_id
        block_id = ""
        for ch in (created.get("children") or []):
            if ch.get("block_type") == 2:
                block_id = ch.get("block_id", "")
                break
        return {"document_id": doc_id,
                "url": f"{_tenant_url()}/docx/{doc_id}",
                "block_id": block_id}

    def update_text_doc(self, doc_id: str, content: str,
                        body_block_id: Optional[str] = None) -> None:
        """Replace the body text of a text doc IN PLACE. If
        body_block_id is not given, we list the doc's blocks and pick
        the first text block (block_type==2). If there are multiple
        text blocks (e.g. doc was created elsewhere with rich markdown),
        we overwrite the first with the full new content and BLANK the
        rest — never deleting. UPDATE only."""
        from lark_oapi.api.docx.v1 import (
            BatchUpdateDocumentBlockRequest,
            BatchUpdateDocumentBlockRequestBody,
            UpdateBlockRequest, UpdateTextElementsRequest, Text)
        if not body_block_id:
            text_blocks = [b for b in self.get_docx_blocks(doc_id)
                           if b.get("block_type") == 2]
            if not text_blocks:
                # nothing to update — append a fresh body block
                from lark_oapi.api.docx.v1 import (
                    CreateDocumentBlockChildrenRequest,
                    CreateDocumentBlockChildrenRequestBody, Block)
                body_block = (Block.builder().block_type(2)
                              .text(Text.builder()
                                    .elements(self._chunk_runs(content))
                                    .build()).build())
                _LIMITS["doc"].acquire()
                req = (CreateDocumentBlockChildrenRequest.builder()
                       .document_id(doc_id).block_id(doc_id)
                       .request_body(
                           CreateDocumentBlockChildrenRequestBody.builder()
                           .children([body_block]).index(0).build())
                       .build())
                self._check(
                    self._client.docx.v1.document_block_children.create(
                        req, self._opt()),
                    "update_text_doc (append fallback)")
                return
            target_ids = [b["block_id"] for b in text_blocks]
        else:
            target_ids = [body_block_id]

        # First block gets the full content; any trailing blocks are
        # blanked (never deleted — single space keeps them valid).
        updates = []
        for i, bid in enumerate(target_ids):
            chunk_content = content if i == 0 else " "
            text = Text.builder().elements(
                self._chunk_runs(chunk_content)).build()
            update_text_req = (UpdateTextElementsRequest.builder()
                               .elements(text.elements).build())
            updates.append(UpdateBlockRequest.builder().block_id(bid)
                           .update_text_elements(update_text_req).build())
        _LIMITS["doc"].acquire()
        req = (BatchUpdateDocumentBlockRequest.builder()
               .document_id(doc_id)
               .request_body(
                   BatchUpdateDocumentBlockRequestBody.builder()
                   .requests(updates).build())
               .build())
        self._check(
            self._client.docx.v1.document_block.batch_update(
                req, self._opt()),
            "update_text_doc")

    def parse_doc_template(self, document_id: str,
                            bucket_resolver=None
                            ) -> Dict[str, Any]:
        """Hierarchical parser for template-format target lists. Walks
        root children and groups them by Bucket(H1) → Firm(H2) →
        Office(H3) → body blocks.

        `bucket_resolver(heading_text)` is a callable that returns the
        canonical bucket name (e.g. "Strong Fit") if the H1 heading is
        a recognised bucket, or None otherwise. Caller supplies this
        from lark_bot._canonical_bucket so the parser stays vocab-
        agnostic (and handles legacy + new naming both).

        Return shape:
            {
              "is_template": bool,    # True if the doc looks template-format
                                       # (at least 2 H1s resolved to canonical buckets)
              "preamble_blocks": [block_id, ...],   # everything before the first bucket
              "buckets": [
                  {"name": "Strong Fit",
                   "h1_block_id": "doxjp...",
                   "h1_position": 5,         # index in root-children
                   "last_position": 18,      # index of this bucket's last block
                                              # (inclusive)
                   "firms": [
                       {"name": "Cooley",
                        "h2_block_id": "doxjp...",
                        "h2_position": 6,
                        "last_position": 12,
                        "offices": [
                            {"city": "Singapore",
                             "h3_block_id": "doxjp...",
                             "h3_position": 7,
                             "last_position": 12,
                             "body_block_ids": ["doxjp...", ...]  # text/bullet
                                                                  # /callout between
                                                                  # this H3 and the
                                                                  # next H3/H2/H1}
                        ]}]}, ...]
            }
        """
        blocks = self.get_docx_blocks(document_id)
        root_children = [b for b in blocks
                          if b.get("parent_id") == document_id]
        # parent_id -> [child blocks], so we can read a callout's text
        # (its content lives in CHILD blocks, not the callout itself).
        by_parent: Dict[str, List[Dict[str, Any]]] = {}
        for b in blocks:
            by_parent.setdefault(b.get("parent_id"), []).append(b)

        def _text_of(b: Dict[str, Any]) -> str:
            s = ""
            for fk in ("text", "heading1", "heading2", "heading3",
                       "heading4", "heading5", "heading6",
                       "bullet", "ordered", "code", "quote", "callout"):
                v = b.get(fk, {}) or {}
                for el in (v.get("elements", []) or []):
                    tr = el.get("text_run", {}) or {}
                    s += tr.get("content", "") or ""
            return s

        def _callout_text(callout_id: str) -> str:
            """Full text inside a callout (its child blocks)."""
            parts: List[str] = []
            for ch in by_parent.get(callout_id, []):
                parts.append(_text_of(ch))
            return "\n".join(p for p in parts if p)

        def _parse_callout_firm(callout_id: str, pos: int
                                 ) -> Optional[Dict[str, Any]]:
            """A Not-a-Fit entry is one callout whose first line is
            'Firm — Office' followed by optional removed-prologue and
            the analysis body. Parse it into a firm entry (flagged
            is_callout) so the hierarchy + edit-planner can see + move
            it like any other firm. Returns None if the header doesn't
            split into firm/office."""
            full = _callout_text(callout_id)
            lines = [ln for ln in full.split("\n")]
            header = next((ln.strip() for ln in lines if ln.strip()), "")
            # Split on em-dash (the renderer joins with ' — ').
            if "—" in header:
                firm, _, office = header.partition("—")
            elif " - " in header:
                firm, _, office = header.partition(" - ")
            else:
                firm, office = header, ""
            firm = firm.strip()
            office = office.strip()
            if not firm:
                return None
            # Body = everything after the header + any Removed/Why
            # prologue lines (so a restore drops the removal metadata
            # and keeps just the analysis).
            body_lines: List[str] = []
            seen_blank = False
            for ln in lines:
                s = ln.strip()
                if s == header:
                    continue
                if (s.lower().startswith("removed ")
                        or s.lower().startswith("why removed:")):
                    continue
                if not s and not body_lines:
                    seen_blank = True
                    continue
                body_lines.append(ln)
            body_md = "\n".join(body_lines).strip()
            return {
                "name":            firm,
                "h2_block_id":     callout_id,
                "h2_position":     pos,
                "last_position":   pos,
                "is_callout":      True,
                "callout_block_id": callout_id,
                "offices": [{
                    "city":            office or "(office)",
                    "h3_block_id":     callout_id,
                    "h3_position":     pos,
                    "last_position":   pos,
                    "is_callout":      True,
                    "callout_block_id": callout_id,
                    "callout_body_md": body_md,
                    "body_block_ids":  [],
                }],
            }

        out: Dict[str, Any] = {
            "is_template":      False,
            "preamble_blocks":  [],
            "buckets":          [],
        }
        cur_bucket: Optional[Dict[str, Any]] = None
        cur_firm:   Optional[Dict[str, Any]] = None
        cur_office: Optional[Dict[str, Any]] = None

        for pos, b in enumerate(root_children):
            bt = b.get("block_type")
            t = _text_of(b).strip()
            bid = b.get("block_id", "")

            if bt == 3:    # H1 — bucket boundary
                resolved = bucket_resolver(t) if bucket_resolver else None
                # Close out the previous bucket's last_position.
                if cur_bucket is not None:
                    cur_bucket["last_position"] = pos - 1
                    if cur_firm is not None:
                        cur_firm["last_position"] = pos - 1
                        if cur_office is not None:
                            cur_office["last_position"] = pos - 1
                if resolved:
                    cur_bucket = {
                        "name":          resolved,
                        "h1_block_id":   bid,
                        "h1_position":   pos,
                        "last_position": pos,    # filled in on close
                        "firms":         [],
                    }
                    out["buckets"].append(cur_bucket)
                    cur_firm = None
                    cur_office = None
                else:
                    # Unrecognised H1 — treat as preamble continuation
                    # (rare for template docs; never for legacy docs
                    # since the bucket_resolver wouldn't even be called
                    # there. Either way, log so we can spot drift.)
                    cur_bucket = None
                    cur_firm = None
                    cur_office = None
                    out["preamble_blocks"].append(bid)
                continue

            if bt == 4:    # H2 — firm
                if cur_bucket is None:
                    out["preamble_blocks"].append(bid)
                    continue
                if cur_firm is not None:
                    cur_firm["last_position"] = pos - 1
                    if cur_office is not None:
                        cur_office["last_position"] = pos - 1
                cur_firm = {
                    "name":          t,
                    "h2_block_id":   bid,
                    "h2_position":   pos,
                    "last_position": pos,
                    "offices":       [],
                }
                cur_bucket["firms"].append(cur_firm)
                cur_office = None
                continue

            if bt == 5:    # H3 — office
                if cur_firm is None:
                    if cur_bucket is None:
                        out["preamble_blocks"].append(bid)
                    continue
                if cur_office is not None:
                    cur_office["last_position"] = pos - 1
                cur_office = {
                    "city":           t,
                    "h3_block_id":    bid,
                    "h3_position":    pos,
                    "last_position":  pos,
                    "body_block_ids": [],
                }
                cur_firm["offices"].append(cur_office)
                continue

            # Callout directly under a bucket = a Not-a-Fit-style entry
            # (firm tucked into a collapsed box). Parse it into a firm
            # so the hierarchy + edit-planner can see and move it like
            # any other firm. (Callouts nested under an office are just
            # body content — handled by the generic branch below.)
            if bt == 19 and cur_bucket is not None and cur_office is None:
                cf = _parse_callout_firm(bid, pos)
                if cf:
                    cur_firm = cf
                    cur_bucket["firms"].append(cf)
                    cur_office = None
                    continue

            # Body block (text / bullet / callout / etc.)
            if cur_office is not None:
                cur_office["body_block_ids"].append(bid)
            elif cur_bucket is None:
                out["preamble_blocks"].append(bid)
            # else: under a bucket or firm but no office yet — these
            # are usually placeholder text like "_No firms in this
            # bucket yet._" Leave them attached to nothing; they're
            # part of the bucket span via last_position but no body
            # under a specific office.

        # Close out the LAST open items by walking to the end position.
        end_pos = len(root_children) - 1
        if cur_office is not None:
            cur_office["last_position"] = end_pos
        if cur_firm is not None:
            cur_firm["last_position"] = end_pos
        if cur_bucket is not None:
            cur_bucket["last_position"] = end_pos

        out["is_template"] = len(out["buckets"]) >= 2
        return out

    def add_callout_with_text(self, document_id: str,
                                parent_block_id: str,
                                index: int,
                                text: str,
                                emoji_id: str = "warning",
                                background_color: Optional[int] = 1,
                                border_color: Optional[int] = 1
                                ) -> str:
        """Create a callout block (block_type=19) as a child of
        `parent_block_id` at position `index`, then add a single text
        block CONTAINING `text` as the callout's child. Returns the
        callout block's id so the caller can track / reference it.

        `background_color` / `border_color` are Lark's callout palette
        ints (1 ≈ light red, used here so a moved-to-Not-a-Fit entry is
        VISUALLY DISTINCT from the rest of the doc — the operator's
        white-callout complaint). Pass None to leave default (no fill).

        Used by the move-to-Not-a-Fit flow so a removed firm's full
        entry (name + office + why + prior analysis) is tucked into one
        collapsible, colored callout, keeping the Not-a-Fit bucket
        scannable. No delete capability — document_block_children.create
        only.
        """
        from lark_oapi.api.docx.v1 import (
            CreateDocumentBlockChildrenRequest,
            CreateDocumentBlockChildrenRequestBody,
            Block, Callout, Text)
        # Step 1 — create the callout block at the requested position
        # with a colored fill so it stands out from body text.
        cb = Callout.builder().emoji_id(emoji_id or "warning")
        if background_color is not None:
            cb = cb.background_color(background_color)
        if border_color is not None:
            cb = cb.border_color(border_color)
        callout_obj = cb.build()
        callout_block = (Block.builder()
                          .block_type(19)
                          .callout(callout_obj)
                          .build())
        _LIMITS["doc"].acquire()
        req = (CreateDocumentBlockChildrenRequest.builder()
               .document_id(document_id)
               .block_id(parent_block_id)
               .request_body(
                   CreateDocumentBlockChildrenRequestBody.builder()
                   .children([callout_block])
                   .index(index)
                   .build())
               .build())
        data = self._check(
            self._client.docx.v1.document_block_children.create(
                req, self._opt()),
            "add_callout_with_text (callout)")
        created = data.children or []
        if not created:
            raise RuntimeError("add_callout_with_text: callout "
                               "creation returned no children")
        callout_id = created[0].block_id

        # Step 2 — append the text body under the callout. Newlines in
        # `text` end up as visual line-breaks within the single
        # paragraph block (sufficient for the v1 preserved-analysis
        # tucking; multi-block-inside-callout is a future polish).
        text_block = (Block.builder()
                      .block_type(2)
                      .text(Text.builder()
                            .elements(self._chunk_runs(text))
                            .build())
                      .build())
        _LIMITS["doc"].acquire()
        req2 = (CreateDocumentBlockChildrenRequest.builder()
                .document_id(document_id)
                .block_id(callout_id)
                .request_body(
                    CreateDocumentBlockChildrenRequestBody.builder()
                    .children([text_block])
                    .index(0)
                    .build())
                .build())
        self._check(
            self._client.docx.v1.document_block_children.create(
                req2, self._opt()),
            "add_callout_with_text (text child)")
        return callout_id

    def update_block_text(self, doc_id: str, block_id: str,
                            new_text: str) -> None:
        """Replace the text content of a single block in place. Used
        by the bucket-rename migration (and by stage-2 bucket moves).
        Preserves the block's identity + Lark's edit history; only
        the text elements change. No delete capability — operates on
        document_block.batch_update, which is the UPDATE surface,
        not the delete surface."""
        from lark_oapi.api.docx.v1 import (
            BatchUpdateDocumentBlockRequest,
            BatchUpdateDocumentBlockRequestBody,
            UpdateBlockRequest, UpdateTextElementsRequest, Text)
        text = Text.builder().elements(
            self._chunk_runs(new_text)).build()
        update_text_req = (UpdateTextElementsRequest.builder()
                            .elements(text.elements).build())
        upd = (UpdateBlockRequest.builder().block_id(block_id)
               .update_text_elements(update_text_req).build())
        _LIMITS["doc"].acquire()
        req = (BatchUpdateDocumentBlockRequest.builder()
               .document_id(doc_id)
               .request_body(
                   BatchUpdateDocumentBlockRequestBody.builder()
                   .requests([upd]).build())
               .build())
        self._check(
            self._client.docx.v1.document_block.batch_update(
                req, self._opt()),
            "update_block_text")

    def linkify_block_text(self, doc_id: str, block: Dict[str, Any],
                           links: Dict[str, Any]) -> bool:
        """Turn plain-name substrings in an existing block into
        hyperlinks IN PLACE (e.g. interviewer names → firm bio URLs),
        preserving every run's content, color, bold and existing
        links. links values are either a URL string or a
        {"bio","linkedin"} dict — with both, the name links to the bio
        and a ' (LinkedIn)' link run is inserted after it. UPDATE
        surface only — no delete capability. Returns True if a link
        was applied."""
        from lark_oapi.api.docx.v1 import (
            BatchUpdateDocumentBlockRequest,
            BatchUpdateDocumentBlockRequestBody,
            UpdateBlockRequest, UpdateTextElementsRequest,
            TextElement, TextRun, TextElementStyle, Link)
        body = None
        for key in ("bullet", "text", "ordered", "todo", "quote"):
            if isinstance(block.get(key), dict):
                body = block[key]
                break
        if not body or not links:
            return False

        def _mk_el(content: str, st: Dict[str, Any],
                   url: Optional[str] = None):
            sb = TextElementStyle.builder()
            eff_url = url or (st.get("link") or {}).get("url")
            if eff_url:
                sb = sb.link(Link.builder().url(eff_url).build())
            if st.get("bold"):
                sb = sb.bold(True)
            if st.get("text_color") is not None:
                sb = sb.text_color(int(st["text_color"]))
            rb = (TextRun.builder().content(content[:2000])
                  .text_element_style(sb.build()))
            return TextElement.builder().text_run(rb.build()).build()

        elements, applied = [], False
        for el in (body.get("elements") or []):
            tr = el.get("text_run") or {}
            content = tr.get("content", "")
            if not content:
                continue
            st = tr.get("text_element_style") or {}
            if (st.get("link") or {}).get("url"):
                elements.append(_mk_el(content, st))   # already a link
                continue
            pos = 0
            hits = []
            for name, u in links.items():
                bio = li = None
                if isinstance(u, dict):
                    bio, li = u.get("bio"), u.get("linkedin")
                else:
                    bio = u
                if not (bio or li):
                    continue
                i = content.find(name)
                if i >= 0:
                    hits.append((i, name, bio or li,
                                 li if (bio and li) else None))
            for i, name, url, extra_li in sorted(hits):
                if i < pos:
                    continue          # overlapping earlier hit
                if i > pos:
                    elements.append(_mk_el(content[pos:i], st))
                elements.append(_mk_el(name, st, url=url))
                if extra_li:
                    elements.append(_mk_el(" (", st))
                    elements.append(_mk_el("LinkedIn", st, url=extra_li))
                    elements.append(_mk_el(")", st))
                applied = True
                pos = i + len(name)
            if pos < len(content):
                elements.append(_mk_el(content[pos:], st))
        if not (applied and elements):
            return False
        upd = (UpdateBlockRequest.builder()
               .block_id(block.get("block_id"))
               .update_text_elements(
                   UpdateTextElementsRequest.builder()
                   .elements(elements).build())
               .build())
        _LIMITS["doc"].acquire()
        req = (BatchUpdateDocumentBlockRequest.builder()
               .document_id(doc_id)
               .request_body(BatchUpdateDocumentBlockRequestBody.builder()
                             .requests([upd]).build())
               .build())
        self._check_retry(
            lambda: self._client.docx.v1.document_block.batch_update(
                req, self._opt()),
            "linkify_block_text")
        return True

    def recolor_block_text(self, doc_id: str,
                           block: Dict[str, Any],
                           color: int) -> None:
        """Set the text color of an existing block IN PLACE, preserving
        each run's content and link. Used to mark a firm's summary line
        red when that firm passes (team convention: red = rejected).
        UPDATE surface only — no delete capability."""
        from lark_oapi.api.docx.v1 import (
            BatchUpdateDocumentBlockRequest,
            BatchUpdateDocumentBlockRequestBody,
            UpdateBlockRequest, UpdateTextElementsRequest,
            TextElement, TextRun, TextElementStyle, Link)
        body = None
        for key in ("bullet", "text", "ordered", "todo", "quote",
                    "heading1", "heading2", "heading3", "heading4"):
            if isinstance(block.get(key), dict):
                body = block[key]
                break
        if not body:
            return
        elements = []
        for el in (body.get("elements") or []):
            tr = el.get("text_run") or {}
            content = tr.get("content", "")
            if not content:
                continue
            st = tr.get("text_element_style") or {}
            sb = TextElementStyle.builder().text_color(int(color))
            link = (st.get("link") or {}).get("url")
            if link:
                sb = sb.link(Link.builder().url(link).build())
            if st.get("bold"):
                sb = sb.bold(True)
            rb = (TextRun.builder().content(content)
                  .text_element_style(sb.build()))
            elements.append(TextElement.builder()
                            .text_run(rb.build()).build())
        if not elements:
            return
        upd = (UpdateBlockRequest.builder()
               .block_id(block.get("block_id"))
               .update_text_elements(
                   UpdateTextElementsRequest.builder()
                   .elements(elements).build())
               .build())
        _LIMITS["doc"].acquire()
        req = (BatchUpdateDocumentBlockRequest.builder()
               .document_id(doc_id)
               .request_body(
                   BatchUpdateDocumentBlockRequestBody.builder()
                   .requests([upd]).build())
               .build())
        self._check_retry(
            lambda: self._client.docx.v1.document_block.batch_update(
                req, self._opt()),
            "recolor_block_text")

    # -- corpus discovery (docs search) -----------------------------------
    def search_docs(self, search_key: str, offset: int = 0,
                    count: int = 50) -> Dict[str, Any]:
        """Suite docs search — the working corpus-discovery path. Needs the
        Noto user token. Returns {entities:[{token,type,title,owner}], total,
        has_more}."""
        if not self._user_token:
            raise RuntimeError("search_docs requires the Noto user token")
        url = (f"{_base_url()}/open-apis/suite/docs-api/search/object")
        body = json.dumps({"search_key": search_key, "offset": offset,
                            "count": count}).encode()
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": f"Bearer {self._user_token}",
                     "Content-Type": "application/json"})
        _LIMITS["doc"].acquire()
        with urllib.request.urlopen(req, timeout=25) as r:
            payload = json.loads(r.read().decode())
        if payload.get("code") != 0:
            raise RuntimeError(f"search_docs error: {payload.get('msg')}")
        d = payload.get("data", {})
        return {
            "entities": [
                {"token": e.get("docs_token"), "type": e.get("docs_type"),
                 "title": e.get("title"), "owner": e.get("owner_id")}
                for e in d.get("docs_entities", [])
            ],
            "total": d.get("total", 0),
            "has_more": d.get("has_more", False),
        }

    # -- drive: download file content (for PDF / Word ingestion) ---------
    def download_file(self, file_token: str) -> bytes:
        """Download a Drive file's raw bytes by file_token. Used for
        Drive PDFs / Word docs. Read-only; never modifies."""
        url = f"{_base_url()}/open-apis/drive/v1/files/{file_token}/download"
        auth = self._user_token or get_tenant_access_token()
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {auth}"})
        _LIMITS["doc"].acquire()
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    def download_message_resource(self, message_id: str, file_key: str,
                                  res_type: str = "file") -> bytes:
        """Download a chat-message attachment (im resource) by its
        file_key. res_type: 'file' for attachments, 'image' for image
        keys. im/v1 APIs take the TENANT token (user OAuth has no im:*
        scopes) — needs the im:resource scope granted in the Console;
        until then this raises the API's permission error. Read-only."""
        url = (f"{_base_url()}/open-apis/im/v1/messages/{message_id}"
               f"/resources/{file_key}?type={res_type}")
        req = urllib.request.Request(
            url, headers={"Authorization":
                          f"Bearer {get_tenant_access_token()}"})
        _LIMITS["doc"].acquire()
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    # -- drive ------------------------------------------------------------
    def list_drive_files(self, folder_token: Optional[str] = None
                         ) -> List[Dict[str, Any]]:
        """List files/subfolders in a Drive folder (root if None).
        Use with the Noto user token; recurse on type=='folder'."""
        from lark_oapi.api.drive.v1 import ListFileRequest
        out, page_token = [], None
        while True:
            _LIMITS["doc"].acquire()
            b = ListFileRequest.builder().page_size(200)
            if folder_token:
                b = b.folder_token(folder_token)
            if page_token:
                b = b.page_token(page_token)
            data = self._check(
                self._client.drive.v1.file.list(b.build(), self._opt()),
                "list_drive_files")
            for it in (data.files or []):
                out.append(json.loads(self._lark.JSON.marshal(it)))
            if not getattr(data, "has_more", False):
                break
            page_token = getattr(data, "next_page_token", None)
            if not page_token:
                break
        return out

    # -- bitable ----------------------------------------------------------
    # Lark resources can NEVER be shared with bot/app identities — access
    # to a wiki-mounted Base flows through the Noto USER token. We pass
    # self._opt(); when no user_token is set (provisioning
    # path), self._opt() returns None and the SDK uses the tenant token —
    # which works for bot-owned Bases. So this one wiring covers both.
    def bitable_list_records(self, app_token: str, table_id: str
                             ) -> List[Dict[str, Any]]:
        from lark_oapi.api.bitable.v1 import ListAppTableRecordRequest
        out, page_token = [], None
        while True:
            _LIMITS["bitable"].acquire()
            b = (ListAppTableRecordRequest.builder()
                 .app_token(app_token).table_id(table_id).page_size(100))
            if page_token:
                b = b.page_token(page_token)
            data = self._check(
                self._client.bitable.v1.app_table_record.list(
                    b.build(), self._opt()),
                "bitable_list_records")
            for it in (data.items or []):
                out.append(json.loads(self._lark.JSON.marshal(it)))
            if not getattr(data, "has_more", False):
                break
            page_token = data.page_token
        return out

    def bitable_update_record(self, app_token: str, table_id: str,
                              record_id: str, fields: Dict[str, Any]
                              ) -> Dict[str, Any]:
        from lark_oapi.api.bitable.v1 import (
            UpdateAppTableRecordRequest, AppTableRecord,
        )
        _LIMITS["bitable"].acquire()
        req = (UpdateAppTableRecordRequest.builder()
               .app_token(app_token).table_id(table_id).record_id(record_id)
               .request_body(AppTableRecord.builder().fields(fields).build())
               .build())
        data = self._check(
            self._client.bitable.v1.app_table_record.update(req),
            "bitable_update_record")
        return json.loads(self._lark.JSON.marshal(data))

    # -- bitable (Layer 2 — Base ingestion helpers) ----------------------
    # Wiki-mounted Bases are reached via the Noto user_token (Lark won't
    # let us share resources with the bot identity). self._opt() returns
    # the user-token RequestOption when set; None otherwise → SDK uses
    # tenant token, which works for bot-owned Bases. Same pattern as
    # bitable_list_records above.
    def bitable_list_tables(self, app_token: str) -> List[Dict[str, Any]]:
        """List all tables inside a Bitable Base (paginated)."""
        from lark_oapi.api.bitable.v1 import ListAppTableRequest
        out, page_token = [], None
        while True:
            _LIMITS["bitable"].acquire()
            b = ListAppTableRequest.builder().app_token(app_token).page_size(100)
            if page_token:
                b = b.page_token(page_token)
            data = self._check(
                self._client.bitable.v1.app_table.list(b.build(), self._opt()),
                "bitable_list_tables")
            for it in (data.items or []):
                out.append(json.loads(self._lark.JSON.marshal(it)))
            if not getattr(data, "has_more", False):
                break
            page_token = data.page_token
        return out

    def bitable_list_fields(self, app_token: str, table_id: str
                            ) -> List[Dict[str, Any]]:
        """List the column schema (field defs) of a Bitable table."""
        from lark_oapi.api.bitable.v1 import ListAppTableFieldRequest
        out, page_token = [], None
        while True:
            _LIMITS["bitable"].acquire()
            b = (ListAppTableFieldRequest.builder()
                 .app_token(app_token).table_id(table_id).page_size(100))
            if page_token:
                b = b.page_token(page_token)
            data = self._check(
                self._client.bitable.v1.app_table_field.list(
                    b.build(), self._opt()),
                "bitable_list_fields")
            for it in (data.items or []):
                out.append(json.loads(self._lark.JSON.marshal(it)))
            if not getattr(data, "has_more", False):
                break
            page_token = data.page_token
        return out

    # -- sheets (Layer 2 — read + create, never remove) ------------------
    def sheets_list_sheets(self, spreadsheet_token: str
                           ) -> List[Dict[str, Any]]:
        """List sheet tabs inside a Spreadsheet."""
        from lark_oapi.api.sheets.v3 import QuerySpreadsheetSheetRequest
        _LIMITS["doc"].acquire()
        req = (QuerySpreadsheetSheetRequest.builder()
               .spreadsheet_token(spreadsheet_token).build())
        data = self._check(
            self._client.sheets.v3.spreadsheet_sheet.query(req, self._opt()),
            "sheets_list_sheets")
        return [json.loads(self._lark.JSON.marshal(s))
                for s in (data.sheets or [])]

    def sheets_get_values(self, spreadsheet_token: str, range_spec: str
                          ) -> List[List[Any]]:
        """Read a rectangular block of cell values (Sheets v2 values API
        — not in the typed SDK). `range_spec` is 'sheetId!A1:Z200' style."""
        url = (f"{_base_url()}/open-apis/sheets/v2/spreadsheets/"
               f"{spreadsheet_token}/values/"
               f"{urllib.parse.quote(range_spec, safe='!:')}")
        auth = self._user_token or get_tenant_access_token()
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {auth}"})
        _LIMITS["doc"].acquire()
        with urllib.request.urlopen(req, timeout=25) as r:
            payload = json.loads(r.read().decode())
        if payload.get("code") not in (0, None):
            raise RuntimeError(
                f"sheets_get_values error: {payload.get('msg')} "
                f"(code {payload.get('code')})")
        return (payload.get("data", {}).get("valueRange", {})
                .get("values") or [])

    def sheets_create_spreadsheet(self, title: str,
                                   folder_token: Optional[str] = None
                                   ) -> Dict[str, Any]:
        """CREATE a new Spreadsheet (optionally inside a folder). Returns
        the spreadsheet dict (has spreadsheet_token, url). CREATE only."""
        from lark_oapi.api.sheets.v3 import (
            CreateSpreadsheetRequest, Spreadsheet)
        _LIMITS["doc"].acquire()
        body = Spreadsheet.builder().title((title or "Untitled")[:200])
        if folder_token:
            body = body.folder_token(folder_token)
        req = (CreateSpreadsheetRequest.builder()
               .request_body(body.build()).build())
        data = self._check(
            self._client.sheets.v3.spreadsheet.create(req, self._opt()),
            "sheets_create_spreadsheet")
        return json.loads(self._lark.JSON.marshal(data.spreadsheet))

    def sheets_prepend_values(self, spreadsheet_token: str,
                              range_spec: str,
                              values: List[List[Any]]) -> Dict[str, Any]:
        """Prepend rows to a sheet at `range_spec` (Sheets v2 prepend —
        not in the typed SDK). Insert-at-top semantics; does not modify
        existing data in place beyond shifting it down. Never removes."""
        url = (f"{_base_url()}/open-apis/sheets/v2/spreadsheets/"
               f"{spreadsheet_token}/values_prepend")
        body = json.dumps({"valueRange": {"range": range_spec,
                                            "values": values}}).encode()
        auth = self._user_token or get_tenant_access_token()
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": f"Bearer {auth}",
                     "Content-Type": "application/json"})
        _LIMITS["doc"].acquire()
        with urllib.request.urlopen(req, timeout=25) as r:
            payload = json.loads(r.read().decode())
        if payload.get("code") not in (0, None):
            raise RuntimeError(
                f"sheets_prepend_values error: {payload.get('msg')} "
                f"(code {payload.get('code')})")
        return payload.get("data") or {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_token() -> int:
    tok = get_tenant_access_token()
    print(f"tenant_access_token OK (len={len(tok)}, prefix={tok[:6]}…)")
    return 0


def _cmd_whoami() -> int:
    c = LarkClient()
    spaces = c.list_wiki_spaces()
    print(f"Credentials valid. Visible wiki spaces: {len(spaces)}")
    for s in spaces[:10]:
        print(f"  - {s.get('name')} (space_id={s.get('space_id')})")
    return 0


def _cmd_selftest() -> int:
    """Offline checks — no credentials/network required."""
    ok = True

    rl = RateLimiter(rate=100, burst=2)
    t0 = time.monotonic()
    for _ in range(5):
        rl.acquire()
    if time.monotonic() - t0 < 0.01:
        print("FAIL: rate limiter did not throttle"); ok = False
    else:
        print("PASS: rate limiter throttles")

    cfg = lark_config()
    if cfg.get("platform") == "lark-international":
        print("PASS: lark config block resolves (lark-international)")
    else:
        print(f"FAIL: unexpected lark config: {cfg}"); ok = False

    creds = load_lark_credentials()
    if set(creds) == set(_ENV_KEYS):
        print(f"PASS: credential keys present (filled={sum(1 for v in creds.values() if v)}/4)")
    else:
        print("FAIL: credential shape"); ok = False

    os.environ["LARK_APP_ID"] = "_envtest_"
    try:
        if load_lark_credentials()["app_id"] == "_envtest_":
            print("PASS: env var overrides file")
        else:
            print("FAIL: env override"); ok = False
    finally:
        del os.environ["LARK_APP_ID"]

    try:
        import lark_oapi  # noqa: F401
        print("PASS: lark-oapi importable")
    except Exception as e:
        print(f"FAIL: lark-oapi import: {e}"); ok = False

    try:
        assert_no_lark_delete()
        print("PASS: no Lark delete capability in Noto's code")
    except RuntimeError as e:
        print(f"FAIL: {e}"); ok = False

    print("\nselftest:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Lark client — Noto Lark")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("token", help="verify credentials (fetch tenant_access_token)")
    sub.add_parser("whoami", help="show app/tenant visibility (needs creds)")
    sub.add_parser("selftest", help="offline sanity checks (no creds/network)")
    args = p.parse_args()

    try:
        if args.cmd == "token":
            return _cmd_token()
        if args.cmd == "whoami":
            return _cmd_whoami()
        if args.cmd == "selftest":
            return _cmd_selftest()
        p.print_help()
        return 0
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
